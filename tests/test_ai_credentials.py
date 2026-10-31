"""Session length/type regressions; synthetic credentials and a temporary DB only."""
from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server


LIMIT = 256 * 1024


def session(size=70000):
    return json.dumps({"accessToken": "fixture-" + "x" * size, "user": {"name": "测试账号"}}, ensure_ascii=False)


class CredentialValidationTest(unittest.TestCase):
    def test_long_session_preserves_the_complete_string(self):
        value = session()
        self.assertEqual(server.validate_ai_credential(value, "chatgpt"), value)

    def test_exact_utf8_byte_limit_is_accepted(self):
        prefix, suffix = '{"fixture":"', '"}'
        room = LIMIT - len((prefix + suffix).encode())
        value = prefix + "测" * (room // 3) + "x" * (room % 3) + suffix
        self.assertEqual(len(value.encode()), LIMIT)
        self.assertEqual(server.validate_ai_credential(value, "chatgpt"), value)

    def test_excess_length_has_a_size_error_not_a_type_error(self):
        # Fewer than 256K characters can still exceed the UTF-8 byte limit.
        value = json.dumps({"fixture": "测" * (LIMIT // 3)}, ensure_ascii=False)
        self.assertLess(len(value), LIMIT)
        with self.assertRaisesRegex(ValueError, "账号信息过长"):
            server.validate_ai_credential(value, "chatgpt")

    def test_only_non_strings_have_a_type_error(self):
        for value in (None, {}, [], True, 123):
            with self.subTest(type=type(value).__name__), self.assertRaisesRegex(ValueError, "必须为字符串"):
                server.validate_ai_credential(value, "chatgpt")

    def test_empty_content_has_an_actionable_error(self):
        for value in ("", " \t\r\n"):
            with self.subTest(value=repr(value)), self.assertRaisesRegex(ValueError, "请粘贴账号信息"):
                server.validate_ai_credential(value, "chatgpt")

    def test_invalid_or_non_object_json_remains_rejected(self):
        for value in ('{"fixture":', "[]", "null", '"fixture"', "123"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "完整 JSON 对象"):
                server.validate_ai_credential(value, "chatgpt")

    def test_extremely_nested_json_is_a_validation_error(self):
        value = '{"fixture":' + '[' * 3000 + '0' + ']' * 3000 + '}'
        with self.assertRaisesRegex(ValueError, "完整 JSON 对象"):
            server.validate_ai_credential(value, "chatgpt")

    def test_invalid_unicode_has_no_credential_in_the_error(self):
        with self.assertRaisesRegex(ValueError, "账号信息包含无效字符") as error:
            server.validate_ai_credential('{"fixture":"private-fixture-\ud800"}', "chatgpt")
        self.assertNotIn("private-fixture", str(error.exception))

    def test_claude_id_validation_is_unchanged(self):
        org = "12345678-1234-1234-1234-123456789abc"
        value = json.dumps({"organization_id": org.upper(), "organization_id_confirm": org})
        result = server.validate_ai_credential(value, "claude")
        self.assertIsInstance(result, str)
        self.assertEqual(json.loads(result), {"organization_id": org, "organization_id_confirm": org})
        for invalid in ('{}', json.dumps({"organization_id": org, "organization_id_confirm": org[:-1] + "d"})):
            with self.assertRaisesRegex(ValueError, "Organization ID"):
                server.validate_ai_credential(invalid, "claude")


class CredentialHTTPTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(server, "DB_FILE", str(Path(directory) / "shop.db")))
        self.stack.enter_context(patch.object(server, "ORDER_SECRETS", {}))
        self.stack.enter_context(patch.object(server, "RATE_LIMITER", server.RateLimiter()))
        self.stack.enter_context(patch.object(server.urllib.request, "urlopen", side_effect=AssertionError("external network blocked")))
        server.init_db()
        self.access = "fixture-order-access-token"
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def seed(self, provider="chatgpt"):
        with server.database() as db:
            db.execute("""INSERT INTO orders
                (order_no,order_type,recharge_id,upstream_order_id,payment_verified,
                 fulfillment_state,status,credential_state,access_token_hash,ai_provider_code)
                VALUES ('fixture','ai_membership',55,7001,1,'created',1,'pending',?,?)""",
                (hashlib.sha256(self.access.encode()).hexdigest(), provider))

    def post(self, path, fields, ensure_ascii=False):
        body = {"order_id": 1, "access_token": self.access, **fields}
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            conn.request("POST", path, json.dumps(body, ensure_ascii=ensure_ascii).encode(),
                         {"Content-Type": "application/json", "Idempotency-Key": "a" * 64})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_paid_order_forwards_long_session_as_a_string_exactly_once(self):
        self.seed()
        value = session()
        with patch.object(server, "upstream_api", return_value={"code": 0, "data": {"status": 2}}) as upstream:
            status, response = self.post("/api/submit-ai-token", {"token": value})
            self.assertEqual(status, 200)
            self.assertEqual(response["code"], 0)
            self.assertEqual(self.post("/api/submit-ai-token", {"token": value})[0], 200)
        upstream.assert_called_once_with("/orders/7001/submit-ai-token", "POST", {"token": value})
        row = server.read_order(1)
        self.assertEqual(row["credential_state"], "submitted")
        self.assertNotIn("accessToken", json.dumps(row))
        self.assertNotIn("accessToken", json.dumps(response))
        self.assertNotIn(1, server.ORDER_SECRETS)

    def test_escaped_json_envelope_fits_without_truncation(self):
        self.seed()
        # Escaping control characters expands a valid inner JSON string sixfold.
        # Whitespace outside the object is valid JSON, and must remain intact.
        value = '{"fixture":true}' + '\t' * (LIMIT - len('{"fixture":true}'))
        body = {"order_id": 1, "access_token": self.access, "token": value}
        escaped = json.dumps(body).replace('\\t', '\\u0009')
        self.assertGreater(len(escaped), 1024 * 1024)
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            with patch.object(server, "submit_ai_token", return_value={"code": 0, "data": {"status": 2}}) as submit:
                conn.request("POST", "/api/submit-ai-token", escaped, {"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["code"], 0)
                submit.assert_called_once_with(7001, value)
        finally:
            conn.close()

    def test_large_session_at_checkout_is_only_kept_in_memory(self):
        value = session()
        plans = {"code": 0, "data": {"plans": [{"id": 9, "enabled": True, "price_rmb": "10", "provider_code": "chatgpt", "name": "fixture"}]}}
        recharge = {"code": 0, "data": {"id": 55, "pay_data": {"qr_code": "fixture-qr"}}}
        with patch.object(server, "get_ai_plans", return_value=plans), \
                patch.object(server, "create_recharge", return_value=recharge) as create, \
                patch.object(server, "payment_qr", return_value="fixture-qr"):
            status, response = self.post("/api/create-ai-order", {"plan_id": 9, "ai_token": value})
        self.assertEqual(status, 200)
        self.assertEqual(response["code"], 0)
        create.assert_called_once()
        oid = response["data"]["id"]
        self.assertEqual(server.ORDER_SECRETS[oid]["ai_token"], value)
        self.assertNotIn("accessToken", json.dumps(server.read_order(oid)))

    def test_oversize_credential_is_rejected_before_claim_and_can_be_corrected(self):
        self.seed()
        with patch.object(server, "submit_ai_token", return_value={"code": 0, "data": {"status": 2}}) as submit:
            status, response = self.post("/api/submit-ai-token", {"token": session(LIMIT)})
            self.assertEqual(status, 400)
            self.assertIn("账号信息过长", response["msg"])
            submit.assert_not_called()
            self.assertEqual(server.read_order(1)["credential_state"], "pending")
            self.assertEqual(self.post("/api/submit-ai-token", {"token": session(5000)})[0], 200)
            submit.assert_called_once()

    def test_request_size_limits_are_route_specific(self):
        for path, limit in (("/api/create-ai-order", 2 * 1024 * 1024),
                            ("/api/submit-ai-token", 2 * 1024 * 1024),
                            ("/api/create-order", 65536), ("/api/submit-account", 65536)):
            conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Length", str(limit + 1))
                conn.putheader("Content-Type", "application/json")
                conn.endheaders()  # Rejection must occur without reading the body.
                response = conn.getresponse()
                self.assertEqual(response.status, 413)
                self.assertEqual(json.loads(response.read())["msg"], "请求内容过长")
            finally:
                conn.close()

    def test_object_or_missing_token_is_not_coerced_or_forwarded(self):
        self.seed()
        with patch.object(server, "submit_ai_token") as submit:
            for fields in ({}, {"token": {"accessToken": "fixture"}}, {"token": " "}):
                status, _ = self.post("/api/submit-ai-token", fields)
                self.assertEqual(status, 400)
            submit.assert_not_called()
        self.assertEqual(server.read_order(1)["credential_state"], "pending")

    def test_claude_submission_still_forwards_json_as_a_string(self):
        self.seed("claude")
        org = "12345678-1234-1234-1234-123456789abc"
        value = json.dumps({"organization_id": org, "organization_id_confirm": org})
        with patch.object(server, "upstream_api", return_value={"code": 0, "data": {"status": 2}}) as upstream:
            self.assertEqual(self.post("/api/submit-ai-token", {"token": value})[0], 200)
        upstream.assert_called_once_with("/orders/7001/submit-ai-token", "POST", {"token": value})


if __name__ == "__main__":
    unittest.main()
