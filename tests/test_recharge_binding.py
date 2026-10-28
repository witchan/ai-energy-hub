"""Single-wallet payment ownership; all upstream calls are isolated fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import server


DETAIL = {"id": 9, "name": "fixture gift", "amounts": [10],
          "brand_levels": {"level_2": {}}, "display_buy_rate": "1.025", "service_fee_rmb": 0}
PLANS = {"code": 0, "data": {"plans": [{"id": 9, "name": "fixture AI", "enabled": True,
          "available_stock": 0, "price_rmb": "10.25"}]}}
BODY = {"product_id": 9, "amount": 10, "plan_id": 9}


def recharge_result(rid=55):
    return {"code": 0, "data": {"id": rid, "amount": 11.25,
            "pay_data": {"qr_code": "https://example.invalid/pay"}}}


def process_create(source, database_file, barrier, queue, ai):
    spec = importlib.util.spec_from_file_location("child_shop", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DB_FILE = database_file
    module.PROFIT = module.AI_PROFIT = 3
    module.get_product_detail = lambda pid: DETAIL
    module.get_ai_plans = lambda: PLANS
    module.payment_qr = lambda payload: "fixture-qr"
    def recharge(amount):
        assert amount == 11.25
        barrier.wait(timeout=10)
        return recharge_result()
    module.create_recharge = recharge
    with patch.object(module.urllib.request, "urlopen", side_effect=AssertionError("external request blocked")):
        try:
            module.create_local_order(BODY, ai)
            queue.put("created")
        except module.RechargeAlreadyBoundError:
            queue.put("rejected")


class RechargeBindingTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(server, "DB_FILE", str(Path(self.temp) / "shop.db")))
        self.stack.enter_context(patch.object(server, "ORDER_SECRETS", {}))
        for name, value in (("PROFIT", 3), ("AI_PROFIT", 3), ("ENABLE_AI", True), ("ENABLE_GIFTCARD", True)):
            self.stack.enter_context(patch.object(server, name, value))
        self.network = self.stack.enter_context(patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("external request blocked")))
        self.stack.enter_context(patch.object(server, "get_product_detail", return_value=DETAIL))
        self.stack.enter_context(patch.object(server, "get_ai_plans", return_value=PLANS))
        self.stack.enter_context(patch.object(server, "payment_qr", return_value="fixture-qr"))
        self.recharge = self.stack.enter_context(patch.object(server, "create_recharge", return_value=recharge_result()))
        server.init_db()

    def tearDown(self):
        self.network.assert_not_called()

    def rows(self):
        with server.database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM orders ORDER BY id")]

    def assert_duplicate_rejected(self, first_ai, second_ai):
        first = server.create_local_order(BODY, first_ai)
        before = self.rows()
        with patch.object(server, "keep_secret") as keep, patch.object(server, "create_order") as gift, patch.object(server, "create_ai_order") as ai:
            with self.assertRaisesRegex(server.RechargeAlreadyBoundError, "付款单已关联"):
                server.create_local_order(BODY, second_ai)
        self.assertEqual(self.rows(), before)
        self.assertEqual(first["sell_price"], 11.25)
        self.assertEqual([call.args for call in self.recharge.call_args_list], [(11.25,), (11.25,)])
        keep.assert_not_called(); gift.assert_not_called(); ai.assert_not_called()

    def test_gift_duplicate_is_rejected_without_repricing_or_retry(self):
        self.assert_duplicate_rejected(False, False)

    def test_zero_stock_ai_duplicate_is_rejected(self):
        self.assert_duplicate_rejected(True, True)

    def test_gift_recharge_cannot_be_reused_by_ai(self):
        self.assert_duplicate_rejected(False, True)

    def test_ai_recharge_cannot_be_reused_by_gift(self):
        self.assert_duplicate_rejected(True, False)

    def test_distinct_recharges_with_same_price_are_allowed(self):
        self.recharge.side_effect = [recharge_result(55), recharge_result(56)]
        first = server.create_local_order(BODY)
        second = server.create_local_order(BODY, True)
        self.assertEqual(first["sell_price"], second["sell_price"])
        self.assertEqual(first["sell_price"], 11.25)
        self.assertNotEqual(first["access_token"], second["access_token"])
        self.assertEqual(len(self.rows()), 2)

    def test_cancelled_failed_completed_orders_keep_the_binding(self):
        for rid, (status, state) in enumerate(((5, "pending"), (6, "review"), (4, "created")), 55):
            with self.subTest(status=status):
                self.recharge.return_value = recharge_result(rid)
                first = server.create_local_order(BODY)
                with server.database() as db:
                    db.execute("UPDATE orders SET status=?,fulfillment_state=? WHERE id=?", (status, state, first["id"]))
                with self.assertRaises(server.RechargeAlreadyBoundError):
                    server.create_local_order(BODY, True)

    def test_concurrent_threads_create_exactly_one_local_order(self):
        barrier = threading.Barrier(12)
        def recharge(amount):
            self.assertEqual(amount, 11.25)
            barrier.wait(timeout=10)
            return recharge_result()
        self.recharge.side_effect = recharge
        def create(index):
            try:
                server.create_local_order(BODY, bool(index % 2))
                return "created"
            except server.RechargeAlreadyBoundError:
                return "rejected"
        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(create, range(12)))
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("rejected"), 11)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_processes_create_exactly_one_local_order(self):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(4), context.Queue()
        children = [context.Process(target=process_create, args=(server.__file__, server.DB_FILE, barrier, queue, bool(i % 2))) for i in range(4)]
        try:
            for child in children:
                child.start()
            for child in children:
                child.join(15)
                self.assertEqual(child.exitcode, 0)
            results = [queue.get(timeout=2) for _ in children]
            self.assertEqual(sorted(results), ["created", "rejected", "rejected", "rejected"])
            self.assertEqual(len(self.rows()), 1)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate(); child.join()
            queue.close()

    def test_database_rejects_duplicate_insert_and_update(self):
        self.recharge.side_effect = [recharge_result(55), recharge_result(56)]
        first = server.create_local_order(BODY)
        second = server.create_local_order(BODY, True)
        with self.assertRaises(sqlite3.IntegrityError), server.database() as db:
            db.execute("INSERT INTO orders(order_no,recharge_id) VALUES('direct-duplicate',55)")
        with self.assertRaises(sqlite3.IntegrityError), server.database() as db:
            db.execute("UPDATE orders SET recharge_id=? WHERE id=?", (first["recharge_id"], second["id"]))
        self.assertEqual([row["recharge_id"] for row in self.rows()], [55, 56])

    def test_restart_migrates_existing_unique_bindings_idempotently(self):
        server.create_local_order(BODY)
        with server.database() as db:
            db.execute("DROP INDEX idx_orders_recharge_unique")
        before = self.rows()
        server.init_db(); server.init_db()
        self.assertEqual(self.rows(), before)
        with self.assertRaises(server.RechargeAlreadyBoundError):
            server.create_local_order(BODY, True)

    def test_legacy_duplicates_stop_startup_without_reassigning_or_mutating(self):
        legacy = str(Path(self.temp) / "legacy.db")
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, recharge_id INTEGER, upstream_order_id INTEGER, status INTEGER, status_text TEXT)")
            db.executemany("INSERT INTO orders VALUES(?,?,0,0,'待付款')", [(1, 55), (2, 55)])
            schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
            rows = db.execute("SELECT * FROM orders ORDER BY id").fetchall()
        with patch.object(server, "DB_FILE", legacy):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "历史订单存在重复充值单绑定"):
                    server.init_db()
                with sqlite3.connect(legacy) as db:
                    self.assertEqual(db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(), schema)
                    self.assertEqual(db.execute("SELECT * FROM orders ORDER BY id").fetchall(), rows)

    def test_legacy_missing_id_placeholders_do_not_block_migration(self):
        with server.database() as db:
            db.execute("DROP INDEX idx_orders_recharge_unique")
            db.executemany("INSERT INTO orders(order_no,recharge_id) VALUES(?,?)", [("old1", None), ("old2", None), ("old3", 0), ("old4", 0)])
        server.init_db()
        self.assertEqual(len(self.rows()), 4)
        self.assert_duplicate_rejected(False, True)

    def test_other_integrity_errors_are_not_misreported_as_payment_conflicts(self):
        self.recharge.side_effect = [recharge_result(55), recharge_result(56)]
        with patch.object(server.uuid, "uuid4") as uuid:
            uuid.return_value.hex = "fixed-order-number"
            server.create_local_order(BODY)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "orders.order_no"):
                server.create_local_order(BODY)
        self.assertEqual(len(self.rows()), 1)

    def test_http_conflict_does_not_disclose_existing_order_or_payment_data(self):
        first = server.create_local_order(BODY)
        with patch.object(server, "RATE_LIMITER", server.RateLimiter()):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            try:
                connection.request("POST", "/api/create-ai-order", json.dumps(BODY), {"Content-Type": "application/json", "Idempotency-Key": "b" * 64})
                response = connection.getresponse()
                data = json.loads(response.read())
                self.assertEqual(response.status, 409)
                self.assertEqual(set(data), {"code", "msg"})
                self.assertEqual(data["code"], 409)
                self.assertIn("付款单已关联", data["msg"])
                for field in ("access_token", "order_no", "qr_code"):
                    self.assertNotIn(str(first[field]), json.dumps(data))
            finally:
                connection.close(); httpd.shutdown(); httpd.server_close(); thread.join()
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
