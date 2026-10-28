"""Late credential results and ambiguous HTTP errors; no real upstream traffic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
import io
import itertools
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

import server


class P1RegressionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(server, "DB_FILE", str(Path(directory) / "orders.db")))
        self.stack.enter_context(patch.object(server, "ORDER_SECRETS", {}))
        self.stack.enter_context(patch.object(server, "upstream_token", "fixture-session"))
        self.stack.enter_context(patch.object(server, "token_expire", time.time() + 3600))
        self.stack.enter_context(patch.object(server, "UPSTREAM_BASE", "https://upstream.invalid/api/v1"))
        self.network = self.stack.enter_context(patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("external request blocked")))
        server.init_db()

    def seed(self, oid=1, ai=False, **extra):
        values = dict(id=oid, order_no="P1-FIXTURE-" + str(oid), order_type="ai_membership" if ai else "gift_card",
                      recharge_id=100 + oid, upstream_order_id=7000 + oid, product_id=9, amount=10,
                      quantity=1, channel_level="level_2", cost_price=10.25, sell_price=11.25,
                      status=1, status_text="待提交账号", created_at=datetime.now().isoformat(),
                      updated_at=datetime.now().isoformat(), fulfillment_state="created", payment_verified=1,
                      credential_state="pending")
        values.update(extra)
        with server.database() as db:
            db.execute("INSERT INTO orders (" + ",".join(values) + ") VALUES (" + ",".join("?" for _ in values) + ")", tuple(values.values()))

    @staticmethod
    def credentials(ai=False):
        return {"ai_token": '{"fixture":true}'} if ai else {"account": "fixture@example.invalid", "password": "fixture-only"}

    def assert_late_result_preserves_newer_state(self, outcome, statuses):
        for oid, (ai, status) in enumerate(itertools.product((False, True), statuses), 1):
            with self.subTest(ai=ai, status=status):
                self.seed(oid, ai)
                entered, release = threading.Event(), threading.Event()
                def delayed(*args):
                    self.assertTrue(server.read_order(oid)["credential_attempt_id"])
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("release timeout")
                    if outcome == "exception":
                        raise TimeoutError("fixture")
                    if outcome == "rejected":
                        return {"code": 1, "msg": "fixture rejection"}
                    return {"code": 0, "data": {"status": 2, "allow_submit_2fa": False}}
                with patch.object(server, "submit_ai_token" if ai else "submit_account", side_effect=delayed), patch.object(server, "get_ai_order_status" if ai else "get_order_status", return_value={"code": 0, "data": {"status": status, "status_text": "newer state", "progress": 99, "allow_submit_2fa": True}}), ThreadPoolExecutor(1) as pool:
                    future = pool.submit(server.deliver_credentials, oid, self.credentials(ai))
                    try:
                        self.assertTrue(entered.wait(3))
                        server.refresh_order(server.read_order(oid))
                        newer = server.read_order(oid)
                    finally:
                        release.set()
                    future.result(5)
                self.assertEqual(server.read_order(oid), newer)

    def test_late_success_does_not_revert_any_terminal_state(self):
        self.assert_late_result_preserves_newer_state("success", (4, 5, 6, 7))

    def test_late_business_rejection_does_not_revert_terminal_state(self):
        self.assert_late_result_preserves_newer_state("rejected", (4, 5, 6, 7))

    def test_late_exception_does_not_revert_terminal_state(self):
        self.assert_late_result_preserves_newer_state("exception", (4, 5, 6, 7))

    def test_late_results_preserve_newer_processing_and_2fa_flags(self):
        self.assert_late_result_preserves_newer_state("success", (2, 3))

    def test_attempt_id_rejects_late_results_from_an_older_attempt(self):
        self.seed(credential_state="submitting", credential_attempt_id="current-attempt")
        before = server.read_order(1)
        self.assertEqual(server.finish_credential_attempt(1, "old-attempt", "pending", "stale error"), 0)
        self.assertEqual(server.read_order(1), before)
        self.assertEqual(server.finish_credential_attempt(1, "current-attempt", "submitted", "processing", 2, True), 1)
        self.assertEqual(server.read_order(1)["need_2fa"], 1)
        self.assertEqual(server.finish_credential_attempt(1, "current-attempt", "review", "stale duplicate"), 0)

    def test_quarantined_attempt_is_not_reopened_by_a_late_success(self):
        self.seed(credential_state="submitting", credential_attempt_id="fixture", updated_at="2000-01-01T00:00:00")
        server.recover_stale_operations()
        before = server.read_order(1)
        self.assertEqual(before["credential_state"], "review")
        self.assertEqual(server.finish_credential_attempt(1, "fixture", "submitted", "stale success", 2, False), 0)
        self.assertEqual(server.read_order(1), before)

    def test_confirmed_business_rejection_allows_a_new_attempt(self):
        self.seed()
        attempts = []
        def rejected(*args):
            attempts.append(server.read_order(1)["credential_attempt_id"])
            return {"code": 1, "msg": "fixture validation rejection"}
        with patch.object(server, "submit_account", side_effect=rejected) as submit:
            for _ in range(2):
                self.assertEqual(server.deliver_credentials(1, self.credentials())["credential_state"], "pending")
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(len(set(attempts)), 2)
        self.assertTrue(all(attempts))

    def test_attempt_column_migrates_and_is_hidden_from_customer_payload(self):
        legacy = str(Path(server.DB_FILE).with_name("legacy.db"))
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, upstream_order_id INTEGER, status INTEGER, status_text TEXT)")
            db.execute("INSERT INTO orders VALUES(1,7001,2,'processing')")
        with patch.object(server, "DB_FILE", legacy):
            server.init_db(); server.init_db()
            row = server.read_order(1)
            self.assertIsNone(row["credential_attempt_id"])
            self.assertEqual(row["status"], 2)
            self.assertNotIn("credential_attempt_id", server.public_order(row))

    def http_error(self, status, body):
        def fail(request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "fixture error", {}, io.BytesIO(body))
        self.network.side_effect = fail

    def test_all_http_5xx_bodies_remain_ambiguous_even_with_success_code(self):
        for status, body in itertools.product((500, 502, 503, 504),
                (b'{"code":502,"ambiguous":false}', b'{"code":0,"data":{}}', b'[]', b'null', b'not-json', b'\xff')):
            with self.subTest(status=status, body=body):
                self.http_error(status, body)
                before = self.network.call_count
                result = server.upstream_api("/fixture-write", "POST", {"fixture": True})
                self.assertEqual(result["code"], status)
                self.assertIs(result["ambiguous"], True)
                self.assertEqual(self.network.call_count, before + 1)

    def test_json_5xx_blocks_repeated_gift_ai_and_2fa_submissions(self):
        for oid, (kind, status) in enumerate(itertools.product(("gift", "ai", "twofa"), (500, 502, 503, 504)), 1):
            with self.subTest(kind=kind, status=status):
                self.seed(oid, kind == "ai", status=2 if kind == "twofa" else 1, need_2fa=int(kind == "twofa"))
                self.http_error(status, b'{"code":502,"ambiguous":false}')
                before = self.network.call_count
                for _ in range(2):
                    if kind == "twofa":
                        with self.assertRaises(ValueError):
                            server.deliver_2fa(server.read_order(oid), "123456")
                    else:
                        server.deliver_credentials(oid, self.credentials(kind == "ai"))
                row = server.read_order(oid)
                self.assertEqual(row["twofa_state" if kind == "twofa" else "credential_state"], "review")
                self.assertEqual(self.network.call_count, before + 1)

    def test_valid_json_4xx_business_error_is_not_changed_to_ambiguous(self):
        self.http_error(400, b'{"code":1001,"msg":"fixture validation error"}')
        result = server.upstream_api("/fixture-write", "POST", {"fixture": True})
        self.assertEqual(result["code"], 1001)
        self.assertNotIn("ambiguous", result)
        self.network.assert_called_once()

    def test_http_error_never_looks_successful_from_its_json_body(self):
        self.http_error(400, b'{"code":0,"data":{"fixture":true}}')
        self.assertEqual(server.upstream_api("/fixture-write", "POST", {"fixture": True})["code"], 400)


if __name__ == "__main__":
    unittest.main()
