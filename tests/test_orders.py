import hashlib
import http.client
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import server


def process_fulfill(source, database_file, barrier, queue):
    spec = importlib.util.spec_from_file_location("child_shop", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DB_FILE = database_file
    def wallet(*args, **kwargs):
        barrier.wait(timeout=8)
        return {"code": 0, "data": {"balance": 100}}
    def create(*args):
        queue.put("create")
        return {"code": 0, "data": {"id": 7001}}
    module.upstream_api = wallet
    module.create_ai_order = create
    module.fulfill_order(1)


class OrderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = server.DB_FILE
        server.DB_FILE = str(Path(self.temp.name) / "shop.db")
        server.init_db()
        server.ORDER_SECRETS.clear()
        server.RATE_LIMITER = server.RateLimiter()
        self.token = "fixture-order-access-token"
        self.network = patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("external request blocked"))
        self.network.start()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.network.stop()
        server.DB_FILE = self.old_db
        self.temp.cleanup()

    def seed(self, ai=False, **extra):
        values = dict(order_no="fixture", order_type="ai_membership" if ai else "gift_card",
                      recharge_id=55, upstream_order_id=0, product_id=9, amount=10,
                      quantity=1, channel_level="level_2", cost_price=10, sell_price=13,
                      status=0, status_text="pending", created_at="2020-01-01T00:00:00",
                      updated_at="2020-01-01T00:00:00", access_token_hash=hashlib.sha256(self.token.encode()).hexdigest())
        values.update(extra)
        with server.database() as db:
            db.execute("INSERT INTO orders (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")", tuple(values.values()))

    def post(self, path, fields=None, headers=None, raw=None):
        body = {"order_id": 1, "access_token": self.token}
        body.update(fields or {})
        data = json.dumps(body) if raw is None else raw
        head = {"Content-Type": "application/json"}
        if path in ("/api/create-order", "/api/create-ai-order"):
            head["Idempotency-Key"] = os.urandom(32).hex()
        head.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=15)
        try:
            conn.request("POST", path, data, head)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def mock_payment(self, **data):
        payload = {"id": 55, "status": 1, "amount": 13}
        payload.update(data)
        return patch.object(server, "get_recharge_status", return_value={"code": 0, "data": payload})

    def test_recharge_id_mismatch_rejected_before_upstream(self):
        self.seed()
        with patch.object(server, "get_recharge_status") as payment, patch.object(server, "create_order") as create:
            status, _ = self.post("/api/check-recharge", {"recharge_id": 999})
        self.assertEqual(status, 400)
        payment.assert_not_called(); create.assert_not_called()

    def test_order_type_mismatch_rejected(self):
        self.seed(ai=True)
        self.assertEqual(self.post("/api/check-recharge")[0], 400)

    def test_bad_token_rejected(self):
        self.seed()
        self.assertEqual(self.post("/api/check-recharge", {"access_token": "wrong"})[0], 404)

    def test_payment_amount_and_identifier_must_match(self):
        self.seed()
        for data in ({"amount": 1}, {"id": 999}, {"amount": None}):
            with self.mock_payment(**data), patch.object(server, "create_order") as create:
                server.fulfill_order(1)
                create.assert_not_called()
                self.assertEqual(server.read_order(1)["payment_verified"], 0)

    def test_unpaid_has_no_side_effect(self):
        self.seed()
        with self.mock_payment(status=0), patch.object(server, "create_order") as create:
            result = self.post("/api/check-recharge")[1]
        self.assertFalse(result["data"]["paid"])
        create.assert_not_called()

    def test_cancelled_ai_uses_shared_wallet_and_recovers(self):
        self.seed(ai=True, status=5)
        with self.mock_payment() as payment, patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 100}}), patch.object(server, "create_ai_order", return_value={"code": 0, "data": {"id": 7001}}) as create:
            result = self.post("/api/check-ai-recharge")[1]
        payment.assert_called_once_with(55)
        create.assert_called_once()
        self.assertTrue(result["data"]["need_token"])

    def test_ambiguous_create_is_not_retried_after_restart(self):
        self.seed(ai=True, payment_verified=1)
        with patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 100}}), patch.object(server, "create_ai_order", side_effect=TimeoutError()) as create:
            server.fulfill_order(1)
            server.init_db()
            server.fulfill_order(1)
        create.assert_called_once()
        self.assertEqual(server.read_order(1)["fulfillment_state"], "review")

    def test_dispatching_claim_is_never_reset(self):
        self.seed(ai=True, payment_verified=1, fulfillment_state="dispatching", dispatch_started_at=time.time()-600)
        server.recover_stale_operations()
        with patch.object(server, "create_ai_order") as create:
            server.fulfill_order(1)
        create.assert_not_called()
        self.assertEqual(server.read_order(1)["fulfillment_state"], "review")

    def test_restarted_gift_order_can_resubmit_credentials(self):
        self.seed()
        with self.mock_payment(), patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 100}}), patch.object(server, "create_order", return_value={"code": 0, "data": {"id": 7001}}) as create:
            self.assertTrue(self.post("/api/check-recharge")[1]["data"]["need_account"])
            server.init_db()
            with patch.object(server, "submit_account", return_value={"code": 0, "data": {"status": 2}}):
                result = self.post("/api/submit-account", {"account": "a@example.invalid", "password": "fixture-password"})
            self.post("/api/check-recharge")
        self.assertEqual(result[0], 200)
        self.assertEqual(server.read_order(1)["upstream_order_id"], 7001)
        create.assert_called_once()

    def test_upstream_id_persisted_before_credential_submit(self):
        self.seed(payment_verified=1)
        server.keep_secret(1, {"account": "a@example.invalid", "password": "fixture-password"})
        def submit(oid, *args):
            self.assertEqual(server.read_order(1)["upstream_order_id"], oid)
            raise TimeoutError()
        with patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 100}}), patch.object(server, "create_order", return_value={"code": 0, "data": {"id": 7001}}), patch.object(server, "submit_account", side_effect=submit):
            server.fulfill_order(1)
        self.assertEqual(server.read_order(1)["credential_state"], "review")
        self.assertNotIn(1, server.ORDER_SECRETS)

    def test_two_processes_only_dispatch_once(self):
        self.seed(ai=True, payment_verified=1)
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        children = [context.Process(target=process_fulfill, args=(server.__file__, server.DB_FILE, barrier, queue)) for _ in range(2)]
        for child in children: child.start()
        for child in children:
            child.join(12)
            if child.is_alive(): child.terminate(); child.join()
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(queue.get(timeout=2), "create")
        self.assertTrue(queue.empty())
        queue.close()
        self.assertEqual(server.read_order(1)["upstream_order_id"], 7001)

    def test_concurrent_credential_submission_only_once(self):
        self.seed(ai=True, payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=1)
        entered = threading.Event()
        release = threading.Event()
        def submit(*args):
            entered.set(); release.wait(4)
            return {"code": 0, "data": {"status": 2}}
        with patch.object(server, "submit_ai_token", side_effect=submit) as mocked, ThreadPoolExecutor(2) as pool:
            first = pool.submit(server.deliver_credentials, 1, {"ai_token": '{"fixture":true}'})
            self.assertTrue(entered.wait(2))
            second = pool.submit(server.deliver_credentials, 1, {"ai_token": '{"fixture":true}'})
            self.assertEqual(second.result(2)["credential_state"], "submitting")
            release.set(); first.result(3)
        mocked.assert_called_once()

    def test_credential_route_requires_paid_created_order(self):
        self.seed()
        with patch.object(server, "submit_account") as submit:
            self.assertEqual(self.post("/api/submit-account", {"account": "a@example.invalid", "password": "fixture-only"})[0], 400)
        submit.assert_not_called()

    def test_ai_credential_not_truncated(self):
        token = json.dumps({"accessToken": "a"*5000})
        self.assertEqual(server.validate_ai_credential(token, "chatgpt"), token)
        with self.assertRaises(ValueError): server.validate_ai_credential("not-json", "chatgpt")

    def test_2fa_clears_flag_and_blocks_repeat(self):
        self.seed(payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=2, credential_state="submitted", need_2fa=1)
        with patch.object(server, "submit_2fa", return_value={"code": 0, "data": {}}) as submit:
            self.assertEqual(self.post("/api/submit-2fa", {"code": "123456"})[0], 200)
            self.assertEqual(self.post("/api/submit-2fa", {"code": "123456"})[0], 400)
        submit.assert_called_once()
        self.assertEqual(server.read_order(1)["need_2fa"], 0)

    def test_maintenance_recovers_payment_without_browser(self):
        self.seed(ai=True)
        with self.mock_payment() as payment, patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 100}}), patch.object(server, "create_ai_order", return_value={"code": 0, "data": {"id": 7001}}):
            server.maintenance_tick()
        payment.assert_called_once_with(55)
        self.assertEqual(server.read_order(1)["fulfillment_state"], "created")

    def test_pending_upstream_order_resumes_poll_after_restart(self):
        self.seed(ai=True, payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=2, credential_state="submitted")
        server.init_db()
        with patch.object(server, "get_ai_order_status", return_value={"code": 0, "data": {"status": 4, "status_text": "完成", "progress": 100}}):
            server.maintenance_tick()
        self.assertEqual(server.read_order(1)["status"], 4)

    def test_insufficient_balance_does_not_consume_claim(self):
        self.seed(ai=True, payment_verified=1)
        with patch.object(server, "upstream_api", return_value={"code": 0, "data": {"balance": 0}}), patch.object(server, "create_ai_order") as create:
            server.fulfill_order(1)
        create.assert_not_called()
        self.assertEqual(server.read_order(1)["fulfillment_state"], "pending")

    def test_secret_ttl_cleanup(self):
        server.ORDER_SECRETS[1] = {"password": "fixture", "expires_at": time.time()-1}
        server.cleanup_secrets()
        self.assertNotIn(1, server.ORDER_SECRETS)

    def test_malformed_json_and_request_limits(self):
        self.assertEqual(self.post("/api/check-recharge", raw="[]")[0], 400)
        self.assertEqual(self.post("/api/check-recharge", raw="{")[0], 400)
        self.assertEqual(self.post("/api/check-recharge", raw="x"*65537)[0], 413)
        self.assertEqual(self.post("/api/check-recharge", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("/api/check-recharge", headers={"Origin": "https://unrelated.invalid"})[0], 403)
        self.assertEqual(self.post("/api/check-recharge", headers={"Content-Length": "-1"})[0], 400)

    def test_limiter_is_bounded_and_enforced(self):
        limiter = server.RateLimiter()
        self.assertTrue(limiter.allow(("ip", "create"), 1))
        self.assertFalse(limiter.allow(("ip", "create"), 1))
        for i in range(10002): limiter.allow((str(i), "get"), 1)
        self.assertLessEqual(len(limiter.entries), 10000)

    def test_payment_qr_generated_locally(self):
        result = server.payment_qr("https://example.invalid/pay?id=1&amount=13")
        self.assertTrue(result.startswith("data:image/svg+xml;"))
        self.assertNotIn("qrserver", result)

    def test_unique_ai_order_numbers_and_consistent_price(self):
        plans = {"code": 0, "data": {"plans": [{"id": 9, "enabled": True, "available_stock": 2, "name": "fixture", "price_rmb": 10}]}}
        recharge = {"code": 0, "data": {"id": 55, "pay_data": {"qr_code": "https://example.invalid/pay"}}}
        second_recharge = {"code": 0, "data": dict(recharge["data"], id=56)}
        with patch.object(server, "get_ai_plans", return_value=plans), patch.object(server, "create_recharge", side_effect=[recharge, second_recharge]), patch.object(server, "AI_PROFIT", 3):
            first = server.create_local_order({"plan_id": 9}, True)
            second = server.create_local_order({"plan_id": 9}, True)
        self.assertNotEqual(first["order_no"], second["order_no"])
        self.assertEqual(first["sell_price"], 11)

    def test_invalid_denomination_and_disabled_ai_plan(self):
        detail = {"id": 9, "amounts": [10], "is_custom": False}
        with patch.object(server, "get_product_detail", return_value=detail), patch.object(server, "create_recharge") as recharge:
            with self.assertRaises(ValueError): server.create_local_order({"product_id": 9, "amount": 1})
        recharge.assert_not_called()
        plans = {"code": 0, "data": {"plans": [{"id": 9, "enabled": False, "available_stock": 0}]}}
        with patch.object(server, "get_ai_plans", return_value=plans), patch.object(server, "create_recharge") as recharge:
            with self.assertRaises(ValueError): server.create_local_order({"plan_id": 9}, True)
        recharge.assert_not_called()

    def test_public_order_redacts_payment_and_credentials(self):
        self.seed(qr_payload="secret-link")
        public = server.public_order(server.read_order(1))
        self.assertTrue({"qr_payload", "cost_price", "recharge_id", "access_token_hash"}.isdisjoint(public))

    def test_stale_poll_does_not_undo_concurrent_submission(self):
        self.seed(ai=True, payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=1)
        snapshot = server.read_order(1)
        with server.database() as db:
            db.execute("UPDATE orders SET status=2, credential_state='submitted', updated_at='2030-01-01T00:00:00' WHERE id=1")
        with patch.object(server, "get_ai_order_status", return_value={"code": 0, "data": {"status": 1}}):
            server.refresh_order(snapshot)
        self.assertEqual(server.read_order(1)["status"], 2)

    def test_poll_updates_only_the_local_order(self):
        self.seed(ai=True, payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=2)
        self.seed(order_no="gift-fixture", recharge_id=56, payment_verified=1, fulfillment_state="created", upstream_order_id=7001, status=2)
        with patch.object(server, "get_ai_order_status", return_value={"code": 0, "data": {"status": 4}}):
            server.refresh_order(server.read_order(1))
        self.assertEqual(server.read_order(1)["status"], 4)
        self.assertEqual(server.read_order(2)["status"], 2)

    def test_config_allows_disabling_ai(self):
        with patch.object(server, "ENABLE_GIFTCARD", True), patch.object(server, "ENABLE_AI", False), patch.dict(os.environ, {"UPSTREAM_USER":"fixture", "UPSTREAM_PASS":"fixture", "UPSTREAM_PAY_PASS":"fixture"}, clear=True):
            server.validate_config()

    def test_credential_material_not_stored_in_new_database(self):
        detail = {"id": 9, "name": "fixture", "amounts": [10], "brand_levels": {"level_2": {}}, "display_buy_rate": 1, "service_fee_rmb": 0}
        recharge = {"code": 0, "data": {"id": 55, "pay_data": {"qr_code": "https://example.invalid/pay"}}}
        with patch.object(server, "get_product_detail", return_value=detail), patch.object(server, "create_recharge", return_value=recharge):
            result = server.create_local_order({"product_id": 9, "amount": 10, "account": "a@example.invalid", "password": "fixture-private-password"})
        self.assertEqual(result["sell_price"], 11)
        order = server.read_order(result["id"])
        self.assertNotIn("password", order)
        self.assertNotIn("account", order)
        self.assertNotIn("fixture-private-password", json.dumps(order))

    def test_negative_profit_discounts_without_opt_in(self):
        self.assertEqual(server.sale_price(100, -3), 97)

    def test_legacy_migration_preserves_identity_and_marks_ambiguous(self):
        old = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, upstream_order_id INTEGER, status INTEGER, status_text TEXT, account TEXT, password TEXT)")
            db.execute("INSERT INTO orders VALUES(1,7001,2,'处理中','account','password')")
            db.execute("INSERT INTO orders VALUES(2,0,6,'失败','account','password')")
        current = server.DB_FILE
        try:
            server.DB_FILE = str(old)
            server.init_db()
            with server.database() as db:
                rows = db.execute("SELECT fulfillment_state,account,password FROM orders ORDER BY id").fetchall()
            self.assertEqual(tuple(rows[0]), ("created", "", ""))
            self.assertEqual(tuple(rows[1]), ("review", "", ""))
        finally: server.DB_FILE = current


if __name__ == "__main__":
    unittest.main()
