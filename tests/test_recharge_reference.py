"""Optional upstream recharge reference for internal wallet reconciliation."""
from contextlib import ExitStack
from itertools import count
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import server


BODY = {"product_id": 9, "amount": 10, "plan_id": 9}
REFERENCE = "C-fixture-CaseSensitive123"


def recharge_result(rid=55, reference=REFERENCE):
    return {"code": 0, "data": {"id": rid, "recharge_no": reference, "amount": 11.25,
            "pay_data": {"qr_code": "https://example.invalid/pay"}}}


class RechargeReferenceTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(server, "DB_FILE", str(Path(directory) / "shop.db")))
        self.stack.enter_context(patch.object(server, "ORDER_SECRETS", {}))
        self.network = self.stack.enter_context(patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("external request blocked")))
        for name in ("PROFIT", "AI_PROFIT"):
            self.stack.enter_context(patch.object(server, name, 3))
        detail = {"id": 9, "name": "fixture", "amounts": [10], "brand_levels": {"level_2": {}},
                  "display_buy_rate": "1.025", "service_fee_rmb": 0}
        plans = {"code": 0, "data": {"plans": [{"id": 9, "name": "fixture AI", "enabled": True,
                 "available_stock": 0, "price_rmb": "10.25"}]}}
        self.stack.enter_context(patch.object(server, "get_product_detail", return_value=detail))
        self.stack.enter_context(patch.object(server, "get_ai_plans", return_value=plans))
        self.stack.enter_context(patch.object(server, "payment_qr", return_value="fixture-qr"))
        self.recharge = self.stack.enter_context(patch.object(server, "create_recharge", return_value=recharge_result()))
        server.init_db()

    def tearDown(self):
        self.network.assert_not_called()

    def test_gift_and_ai_persist_upstream_reference_without_repricing(self):
        for ai in (False, True):
            with self.subTest(ai=ai):
                self.recharge.return_value = recharge_result(55 + int(ai))
                order = server.create_local_order(BODY, ai)
                saved = server.read_order(order["id"])
                self.assertEqual(saved["recharge_no"], REFERENCE)
                self.assertEqual(saved["sell_price"], 11.25)
                self.assertEqual(saved["recharge_id"], 55 + int(ai))
        self.assertEqual([call.args for call in self.recharge.call_args_list], [(11.25,), (11.25,)])

    def test_reference_survives_restart_and_supports_lookup(self):
        order = server.create_local_order(BODY)
        server.init_db(); server.init_db()
        with server.database() as db:
            row = db.execute("SELECT order_no,recharge_id,recharge_no FROM orders WHERE recharge_no=?", (REFERENCE,)).fetchone()
        self.assertEqual(tuple(row), (order["order_no"], 55, REFERENCE))

    def test_missing_or_non_string_reference_does_not_block_order(self):
        missing = object()
        for rid, value in enumerate((missing, None, False, 123, {}, [], "", "  "), 55):
            with self.subTest(value=value):
                result = recharge_result(rid, value)
                if value is missing:
                    result["data"].pop("recharge_no")
                self.recharge.return_value = result
                order = server.create_local_order(BODY, bool(rid % 2))
                self.assertIsNone(server.read_order(order["id"])["recharge_no"])
                self.assertEqual(order["sell_price"], 11.25)

    def test_browser_cannot_override_or_supply_reference(self):
        self.recharge.side_effect = [recharge_result(55), recharge_result(56, None)]
        body = dict(BODY, recharge_no="browser-forged-reference")
        first = server.create_local_order(body)
        second = server.create_local_order(body, True)
        self.assertEqual(server.read_order(first["id"])["recharge_no"], REFERENCE)
        self.assertIsNone(server.read_order(second["id"])["recharge_no"])

    def test_customer_payloads_do_not_expose_reference(self):
        order = server.create_local_order(BODY)
        saved = server.read_order(order["id"])
        for payload in (order, server.public_order(saved), server.order_flags(saved)):
            self.assertNotIn("recharge_no", payload)
            self.assertNotIn(REFERENCE, str(payload))
        self.assertEqual(saved["recharge_no"], REFERENCE)

    def test_reference_is_not_a_second_unique_binding(self):
        ids = count(55)
        self.recharge.side_effect = lambda amount: recharge_result(next(ids))
        server.create_local_order(BODY)
        server.create_local_order(BODY, True)
        with server.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM orders WHERE recharge_no=?", (REFERENCE,)).fetchone()[0], 2)
            indexes = {row[1]: row[2] for row in db.execute("PRAGMA index_list(orders)")}
        self.assertEqual(indexes["idx_orders_recharge_no"], 0)
        self.assertEqual(indexes["idx_orders_recharge_unique"], 1)

    def test_same_id_is_rejected_even_if_reference_changes(self):
        self.recharge.side_effect = [recharge_result(55), recharge_result(55, "C-different")]
        first = server.create_local_order(BODY)
        with self.assertRaises(server.RechargeAlreadyBoundError):
            server.create_local_order(BODY, True)
        self.assertEqual(server.read_order(first["id"])["recharge_no"], REFERENCE)

    def test_reference_does_not_replace_payment_verification(self):
        order = server.create_local_order(BODY)
        saved = server.read_order(order["id"])
        data = {"id": 55, "status": 1, "amount": 11.25, "recharge_no": REFERENCE}
        for wrong in ({"id": 99}, {"status": 0}, {"amount": 1.23}):
            with self.subTest(wrong=wrong), patch.object(server, "get_recharge_status", return_value={"code": 0, "data": dict(data, **wrong)}) as payment:
                self.assertFalse(server.verify_payment(saved))
                payment.assert_called_once_with(55)
                self.assertEqual(server.read_order(order["id"])["payment_verified"], 0)
        # The auxiliary reference is neither payment proof nor a new payment gate.
        with patch.object(server, "get_recharge_status", return_value={"code": 0, "data": dict(data, recharge_no="different-reference")}) as payment:
            self.assertTrue(server.verify_payment(saved))
            payment.assert_called_once_with(55)
        self.assertEqual(server.read_order(order["id"])["recharge_no"], REFERENCE)

    def test_legacy_migration_preserves_rows_and_leaves_reference_empty(self):
        legacy = str(Path(server.DB_FILE).with_name("legacy.db"))
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, order_no TEXT, recharge_id INTEGER, upstream_order_id INTEGER, status INTEGER, status_text TEXT, sell_price REAL)")
            db.execute("INSERT INTO orders VALUES(1,'old-order',55,0,0,'待付款',11.25)")
            before = db.execute("SELECT * FROM orders").fetchall()
        with patch.object(server, "DB_FILE", legacy):
            server.init_db(); server.init_db()
            with server.database() as db:
                rows = db.execute("SELECT id,order_no,recharge_id,upstream_order_id,status,status_text,sell_price FROM orders").fetchall()
                self.assertEqual([tuple(row) for row in rows], before)
                self.assertIsNone(db.execute("SELECT recharge_no FROM orders").fetchone()[0])

    def test_parameterized_lookup_preserves_opaque_reference_verbatim(self):
        reference = "C_MixedCase-' OR 1=1 -- "
        self.recharge.side_effect = [recharge_result(55, reference), recharge_result(56, "another-reference")]
        first = server.create_local_order(BODY)
        server.create_local_order(BODY, True)
        with server.database() as db:
            rows = db.execute("SELECT id,recharge_no FROM orders WHERE recharge_no=?", (reference,)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(first["id"], reference)])


if __name__ == "__main__":
    unittest.main()
