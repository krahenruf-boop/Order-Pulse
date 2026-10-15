"""Feishu custom-bot transport; never logs URLs, secrets, or response bodies.

Official protocol: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
Persistence and deciding whether an event is still relevant belong to the caller.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import inspect
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

MAX_BODY_BYTES = 19_000
REQUEST_INTERVAL = 0.7  # < 100/minute and < 5/second, across all clients.
_rate_lock = threading.Lock()
_next_request_at = 0.0


def validate_webhook(webhook: str) -> None:
    """Raise a credential-free error for unsupported URLs; redirects are forbidden."""
    try:
        parts = urllib.parse.urlsplit(webhook)
        valid = (parts.scheme == "https" and parts.netloc == "open.feishu.cn"
                 and re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]{1,256}", parts.path)
                 and not parts.query and not parts.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Webhook 格式不正确：请填写飞书群自定义机器人的 HTTPS 地址")


def make_sign(timestamp: str | int, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def _event_tag(event_id: str) -> str:
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128 or any(c in event_id for c in "\r\n"):
        raise ValueError("事件编号必须为不含换行的 1–128 字符文本")
    return f"事件编号：{event_id}"


def _decorate(text: str, event_id: str, keyword: str) -> str:
    tag = _event_tag(event_id)
    if keyword and keyword not in text:
        text = f"{keyword}\n{text}"
    if tag not in text:
        text = f"{text}\n\n{tag}"
    return text


def _encode(text: str, timestamp: str = "", sign: str = "") -> bytes:
    payload = {"msg_type": "text", "content": {"text": text}}
    if sign:
        payload.update(timestamp=timestamp, sign=sign)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _budget_size(text: str) -> int:
    # Reserve the full signed payload, including a timestamp longer than needed today.
    return len(_encode(text, "9" * 20, "x" * 44))


def split_messages(blocks: list[str], header: str, event_id: str, keyword: str = "") -> list[str]:
    """Split only between account blocks; every returned signed body fits 19,000 B.

    Return [] when there are no non-empty account blocks. A single overlarge account
    raises ValueError, so no account, amount, or audit metadata is silently cut off.
    """
    _event_tag(event_id)
    blocks = [block.strip() for block in blocks if block.strip()]
    if not blocks:
        return []
    upper = len(blocks)

    def compose(group: list[str], index: int, total: int) -> str:
        prefix = header.strip()
        if total > 1:
            prefix = f"{prefix}\n第 {index}/{total} 条".strip()
        return _decorate(prefix + "\n\n" + "\n\n".join(group), event_id, keyword)

    groups: list[list[str]] = []
    group: list[str] = []
    for block in blocks:
        if _budget_size(compose(group + [block], upper, upper)) > MAX_BODY_BYTES:
            if not group:
                raise ValueError("单账户消息或标题过长，请精简模板；未截断账户内容")
            groups.append(group)
            group = [block]
            if _budget_size(compose(group, upper, upper)) > MAX_BODY_BYTES:
                raise ValueError("单账户消息或标题过长，请精简模板；未截断账户内容")
        else:
            group.append(block)
    if group:
        groups.append(group)
    return [compose(group, i + 1, len(groups)) for i, group in enumerate(groups)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _rate_wait() -> None:
    global _next_request_at
    with _rate_lock:
        remaining = _next_request_at - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        _next_request_at = time.monotonic() + REQUEST_INTERVAL


class FeishuClient:
    def __init__(self, webhook: str, secret: str = "", keyword: str = "", max_retries: int = 2):
        validate_webhook(webhook)
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or not 0 <= max_retries <= 10:
            raise ValueError("重试次数应为 0–10 的整数")
        self._webhook = webhook
        self._secret = secret
        self.keyword = keyword.strip()
        self.max_retries = max_retries
        self.timeout = 15
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "FeishuClient(webhook=<已遮盖>, secret=<已遮盖>)"

    def _post(self, body: bytes) -> tuple[int, bytes, str]:
        request = urllib.request.Request(self._webhook, data=body, method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(65_537), response.headers.get("Retry-After", "")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(65_537), exc.headers.get("Retry-After", "") if exc.headers else ""
            finally:
                exc.close()

    async def _valid(self, still_valid: Callable | None) -> bool:
        if still_valid is None:
            return True
        try:
            result = still_valid()
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    async def send(self, text: str, event_id: str, still_valid: Callable | None = None) -> dict:
        """Return status/reason/attempts; a timeout is uncertain and never retried.

        Caller persists pending before calling and the returned outcome afterwards.
        still_valid() may be sync or async and is checked immediately before every
        HTTP request (including retries and after rate-limit waiting).
        """
        attempts = 0

        def result(status: str, reason: str) -> dict:
            return {"status": status, "reason": reason, "attempts": attempts}

        try:
            if not isinstance(text, str) or not text.strip():
                return result("failed", "消息内容为空")
            prepared = _decorate(text, event_id, self.keyword)
        except ValueError as exc:
            return result("failed", str(exc))
        if _budget_size(prepared) > MAX_BODY_BYTES:
            return result("failed", "消息超过安全字节上限，请按账户拆分或精简模板")

        reason = "飞书发送失败"
        for retry in range(self.max_retries + 1):
            if not await self._valid(still_valid):
                return result("cancelled", "通知已失效或账户状态已改变，已取消发送")
            await asyncio.to_thread(_rate_wait)
            if not await self._valid(still_valid):
                return result("cancelled", "通知已失效或账户状态已改变，已取消发送")
            timestamp = str(int(time.time()))
            body = _encode(prepared, timestamp, make_sign(timestamp, self._secret) if self._secret else "")
            attempts += 1
            retry_after = ""
            try:
                status, raw, retry_after = await asyncio.to_thread(self._post, body)
            except asyncio.CancelledError:
                # A worker thread cannot abort an already-issued request reliably.
                # Return an auditable outcome instead of calling it unsent.
                return result("uncertain", "发送过程中任务被取消，飞书可能已收到；请核对事件编号")
            except (TimeoutError, socket.timeout, http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                return result("uncertain", "请求超时或发送后连接中断，飞书可能已收到；未自动重试")
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout, ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected)) or "timed out" in str(exc.reason).lower():
                    return result("uncertain", "请求超时或发送后连接中断，飞书可能已收到；未自动重试")
                reason = "无法连接飞书，请检查网络和系统证书"
                if not isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror, OSError)):
                    return result("failed", reason)
            except Exception:
                return result("uncertain", "未能确认飞书响应，未自动重试；请核对群消息和事件编号")
            else:
                if 300 <= status < 400:
                    return result("failed", "Webhook 返回重定向，已阻止；请核对机器人地址")
                if status == 429 or 500 <= status <= 599:
                    reason = f"飞书服务暂时不可用（HTTP {status}）"
                elif not 200 <= status <= 299:
                    return result("failed", f"飞书请求被拒绝（HTTP {status}），请检查机器人配置")
                else:
                    try:
                        data = json.loads(raw)
                        code = data.get("code") if isinstance(data, dict) else None
                    except (ValueError, UnicodeDecodeError):
                        code = None
                    if type(code) is not int:
                        return result("uncertain", "飞书响应缺少有效业务结果，未自动重试；请核对群消息")
                    if code == 0:
                        return result("success", "飞书已返回发送成功")
                    failures = {
                        9499: "飞书拒绝消息格式或消息过大，请检查模板",
                        19021: "飞书签名校验失败，请检查签名密钥和 Mac 系统时间",
                        19022: "当前网络 IP 不在机器人白名单内",
                        19024: "消息未包含机器人要求的关键词，请检查关键词设置",
                    }
                    if code != 11232:
                        return result("failed", failures.get(code, f"飞书返回业务错误（代码 {code}），请检查机器人配置"))
                    reason = "飞书触发频率限制，请稍后重试"
            if retry < self.max_retries:
                delay = min(30.0, 2.0 ** (retry + 1))
                try:
                    delay = max(delay, min(60.0, float(retry_after)))
                except (TypeError, ValueError):
                    pass
                await asyncio.sleep(delay)
        return result("failed", f"{reason}；已达本次重试上限")
