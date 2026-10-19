import asyncio
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, Mock, patch

from order_pulse.feishu import (
    FeishuClient, MAX_BODY_BYTES, _NoRedirect, _budget_size,
    make_sign, split_messages, validate_webhook,
)

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"


class FeishuFormatTests(unittest.TestCase):
    def test_signature_vector(self):
        self.assertEqual(make_sign(1599360473, "demo"), "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")

    def test_strict_destination_and_no_secrets_in_error(self):
        validate_webhook(WEBHOOK)
        for bad in [WEBHOOK.replace("https", "http"), WEBHOOK + "?token=private",
                    WEBHOOK.replace("open.feishu.cn", "open.feishu.cn.evil.example"),
                    WEBHOOK.replace("open.feishu.cn", "user:private@open.feishu.cn"),
                    WEBHOOK + "#private", "https://open.feishu.cn/private",
                    "https://open.feishu.cn:8443/open-apis/bot/v2/hook/private"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    validate_webhook(bad)
                self.assertNotIn("private", str(caught.exception))

    def test_redirects_are_disabled(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_unicode_split_preserves_accounts_and_signed_byte_budget(self):
        blocks = [f"账户{i}\n" + "现金余额：BRL 554.31\n" * 90 for i in range(20)]
        parts = split_messages(blocks, "余额汇总", "event-123", "专用关键词")
        self.assertGreater(len(parts), 1)
        for index, part in enumerate(parts, 1):
            self.assertIn(f"第 {index}/{len(parts)} 条", part)
            self.assertIn("事件编号：event-123", part)
            self.assertIn("专用关键词", part)
            self.assertLessEqual(_budget_size(part), MAX_BODY_BYTES)
        for block in blocks:
            self.assertEqual(sum(block.strip() in part for part in parts), 1)

    def test_oversized_account_never_truncated(self):
        with self.assertRaises(ValueError):
            split_messages(["中" * 7000], "标题", "event")
        self.assertEqual(split_messages([], "标题", "event"), [])

    def test_untitled_alert_has_no_leading_blank_lines(self):
        body='低余额预警\n94店 BomPreço\n当前值：BRL 3962.51'
        for header in ('', '  \n  '):
            message=split_messages([body],header,'alert-event')[0]
            self.assertTrue(message.startswith('低余额预警\n'),repr(message))
            self.assertEqual(message,body+'\n\n事件编号：alert-event')
        self.assertTrue(split_messages([body],'余额汇总','alert-event')[0].startswith('余额汇总\n\n低余额预警'))

    def test_client_repr_redacts_credentials(self):
        client = FeishuClient(WEBHOOK, secret="very-private")
        self.assertNotIn("test-token", repr(client))
        self.assertNotIn("very-private", repr(client))


class FeishuSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rate_patch = patch("order_pulse.feishu._rate_wait")
        self.rate_patch.start()
        self.sleep_patch = patch("order_pulse.feishu.asyncio.sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()
        self.client = FeishuClient(WEBHOOK, "private-secret", "关键词")

    async def asyncTearDown(self):
        self.rate_patch.stop()
        self.sleep_patch.stop()

    async def test_http_and_business_success_both_required(self):
        captured = []
        def post(body):
            captured.append(json.loads(body))
            return 200, b'{"code":0}', ""
        self.client._post = post
        result = await self.client.send("消息", "ev-1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 1)
        self.assertIn("关键词", captured[0]["content"]["text"])
        self.assertIn("事件编号：ev-1", captured[0]["content"]["text"])
        self.assertEqual(captured[0]["sign"], make_sign(captured[0]["timestamp"], "private-secret"))

    async def test_legacy_success_field_is_not_enough(self):
        self.client._post = Mock(return_value=(200, b'{"StatusCode":0}', ""))
        self.assertEqual((await self.client.send("消息", "event"))["status"], "uncertain")
        self.assertEqual(self.client._post.call_count, 1)

    async def test_configuration_errors_never_retry_or_leak_response(self):
        for code in [9499, 19021, 19022, 19024, 999999]:
            self.client._post = Mock(return_value=(200, json.dumps({"code": code, "msg": WEBHOOK + " private-secret"}).encode(), ""))
            result = await self.client.send("消息", "event")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["attempts"], 1)
            self.assertNotIn("private-secret", result["reason"])
            self.assertNotIn("test-token", result["reason"])

    async def test_timeout_is_uncertain_and_never_retried(self):
        for exc in [TimeoutError("private-secret"), urllib.error.URLError(socket.timeout("private-secret")),
                    urllib.error.URLError(ConnectionResetError("private-secret"))]:
            self.client._post = Mock(side_effect=exc)
            result = await self.client.send("消息", "event")
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(result["attempts"], 1)
            self.assertNotIn("private-secret", result["reason"])

    async def test_cancellation_during_request_is_uncertain(self):
        self.client._post = Mock(side_effect=asyncio.CancelledError())
        result = await self.client.send("消息", "event")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["attempts"], 1)

    async def test_rate_limit_retries_then_success(self):
        self.client._post = Mock(side_effect=[(200, b'{"code":11232}', ""), (429, b'', "3"), (200, b'{"code":0}', "")])
        result = await self.client.send("消息", "event")
        self.assertEqual(result, {"status": "success", "reason": "飞书已返回发送成功", "attempts": 3})
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [2.0, 4.0])

    async def test_retry_rechecks_validity_and_cancels_stale_event(self):
        self.client._post = Mock(return_value=(503, b'', ""))
        checks = iter([True, True, False])
        result = await self.client.send("消息", "event", lambda: next(checks))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.client._post.call_count, 1)

    async def test_disabled_account_and_async_validation(self):
        self.client._post = Mock()
        result = await self.client.send("消息", "event", AsyncMock(return_value=False))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["attempts"], 0)
        self.client._post.assert_not_called()

    async def test_missing_code_and_redirect_cannot_be_success(self):
        for response, expected in [((200, b'<html>login</html>', ""), "uncertain"),
                                   ((200, b'{"code":false}', ""), "uncertain"),
                                   ((302, b'', ""), "failed"),
                                   ((403, b'{"code":0}', ""), "failed")]:
            self.client._post = Mock(return_value=response)
            result = await self.client.send("消息", "event")
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["attempts"], 1)

    async def test_bounded_retries(self):
        self.client._post = Mock(return_value=(503, b'', ""))
        result = await self.client.send("消息", "event")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
