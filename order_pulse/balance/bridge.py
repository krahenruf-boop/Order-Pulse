from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..integrations.ego_runtime import EgoRuntimeError, execute, validate_target
from .json_parser import parse_json_accounts
from .store import now_iso


class BalanceBrowserError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class BrowserBridge:
    """Read Linkme's verified JSON contract through an existing ego-lite page.

    The adapter may claim only its persisted dedicated Space.  It never reads
    or copies browser secrets. A renumbered Space must match the saved identity uniquely.
    """

    def __init__(self, store, secret_store=None, browser_lock=None, browser_binding=None):
        self.store = store
        self.target = store.get("browser_target")
        self.last_seen = store.get("browser_last_seen")
        self.message = "请在余额监控专用空间中打开并登录 Linkme 资产列表"
        self._busy = False
        self.return_to_workbench = None
        self._browser_lock = browser_lock
        self._browser_binding = browser_binding
        self._script = Path(__file__).resolve().parents[1] / "integrations" / "ego_balance.js"

    def set_target(self, target):
        self.target = validate_target(target)
        self.store.set("browser_target", self.target)
        self.message = "已绑定 ego lite 的 Linkme 页面"
        return self.status()

    def cooldown_remaining(self, api_only=False):
        remaining = 0
        keys = ("api_cooldown_until",) if api_only else ("api_cooldown_until", "export_cooldown_until")
        for key in keys:
            until = self.store.get(key)
            if not until:
                continue
            try:
                stamp = datetime.fromisoformat(until)
                if stamp.tzinfo is not None:
                    remaining = max(remaining, math.ceil((stamp - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                pass
        return max(0, remaining)

    def check_export_cooldown(self):
        remaining = self.cooldown_remaining(api_only=True)
        if remaining:
            raise ValueError(f"充值系统余额采集正在冷却，请等待 {remaining} 秒后重试；未重复请求")

    def _execute(self, action):
        try:
            return self._read_source(action)
        finally:
            if self.return_to_workbench:
                self.return_to_workbench()

    def _read_source(self, action):
        if not self.target:
            raise ValueError("请先绑定余额监控专用空间中的 Linkme 页面")
        target = validate_target(self.target)
        spec = dict(target, job={
            "action": action,
            "collection_mode": "json",
            "currency": self.store.settings()["currency"],
            "max_age_minutes": self.store.settings()["max_age_minutes"],
            "source_timezone": self.store.settings()["source_timezone"],
        })
        try:
            if self._browser_lock is None:
                reply = execute(self._script, spec, "ORDER_PULSE_BALANCE:", timeout=120, max_output=4_000_000)
            else:
                with self._browser_lock:
                    reply = execute(self._script, spec, "ORDER_PULSE_BALANCE:", timeout=120, max_output=4_000_000)
        except EgoRuntimeError as exc:
            if exc.code == "BROWSER_TIMEOUT":
                raise ValueError("ego lite 余额读取超时；已有余额保留") from None
            raise ValueError("ego lite 暂不可连接；已有余额保留") from None
        if reply.get("ok") is not True:
            status = reply.get("api_http_status")
            if status == 429:
                retry = reply.get("api_retry_after_seconds", 60)
                retry = max(1, min(retry, 86400)) if type(retry) is int else 60
                self.store.set("api_cooldown_until", (datetime.now(timezone.utc) + timedelta(seconds=retry)).isoformat())
                self.store.log("json_api", "充值系统限制余额读取频率，已进入冷却", level="warning",
                               http_status=429, retry_after_seconds=retry)
                raise ValueError(f"充值系统限制余额读取频率（HTTP 429），请等待 {retry} 秒后重试；上次成功余额已保留")
            code = reply.get("error_code")
            if code == "WAITING_USER":
                raise ValueError("专用浏览器正在被用户操作；余额任务将等待")
            if code == "WAITING_BROWSER":
                raise BalanceBrowserError(code, "余额监控空间暂不可接管，请检查浏览器连接；已有余额保留")
            if code == "BOUND_SPACE_MISSING":
                raise BalanceBrowserError(code, "未找到与已保存名称和身份一致的余额监控空间，请检查空间绑定；已有余额保留")
            if code == "SPACE_MISMATCH":
                raise ValueError("ego lite Space 身份与余额模块保存的绑定不一致")
            if code == "AUTH_REQUIRED" or status in (401, 403):
                raise BalanceBrowserError("AUTH_REQUIRED", "Linkme 登录或读取权限已失效，请在余额监控空间完成登录和验证码；已有余额保留")
            message = reply.get("error")
            raise ValueError(message[:1000] if isinstance(message, str) and message else "ego lite 余额读取失败")
        self.last_seen = now_iso()
        self.store.set("browser_last_seen", self.last_seen)
        if isinstance(reply.get("browser"),dict):
            if self._browser_binding is not None:
                self.target=self._browser_binding.record_balance_space(target,reply["browser"])
                self.store.set("browser_target",self.target)
            self.store.set("browser_last_identity",reply["browser"])
        return reply

    async def inspect(self):
        if self._busy:
            raise ValueError("浏览器采集正在进行")
        self._busy = True
        try:
            reply = await asyncio.to_thread(self._execute, "inspect")
            metadata = reply.get("metadata", {})
            self.store.set("browser_inspection", metadata)
            self.message = "ego lite 已连接，Linkme 账户范围可读取"
            return metadata
        finally:
            self._busy = False

    async def collect(self, inspect=False, mode="json"):
        if inspect:
            return await self.inspect()
        if mode != "json":
            raise ValueError("余额正式采集只使用已核验 JSON 来源")
        if self._busy:
            raise ValueError("浏览器采集正在进行")
        self.check_export_cooldown()
        self._busy = True
        try:
            reply = await asyncio.to_thread(self._execute, "collect")
            metadata = reply.get("metadata", {})
            if metadata.get("owner_bc") is not True or metadata.get("filters_clear") is not True:
                raise ValueError("无法确认 OWNER_BC 和无筛选的账户范围，已停止采集")
            if metadata.get("data_source") != "json" or metadata.get("refresh_mode") != "snapshot":
                raise ValueError("Linkme JSON 只读快照合同不一致，未更新余额")
            required = {"json_all_pages_verified", "json_every_page_rechecked"}
            if not required.issubset(metadata.get("refresh_evidence", [])):
                raise ValueError("余额接口分页校验不完整，未更新本地余额")
            rows = reply.pop("json_accounts", None)
            try:
                result = parse_json_accounts(rows, metadata.get("source_updated_at_raw"),
                                             self.store.settings()["source_timezone"],
                                             self.store.settings()["max_age_minutes"])
            finally:
                rows = None
                self.store.log("cleanup", "本轮 JSON 在内存校验，仅保留白名单业务字段；未生成下载文件",
                               cleanup="json_memory")
            count = metadata.get("page_count")
            pages = metadata.get("page_pagination_pages")
            if type(count) is not int or count != len(result["accounts"]):
                raise ValueError("页面账户总数与 JSON 账户数量不一致")
            if type(pages) is not int or pages != (count + 19) // 20:
                raise ValueError("JSON 页数与账户总数不一致")
            if metadata.get("currency") != self.store.settings()["currency"]:
                raise ValueError("网页账户币种与余额设置不一致")
            raw_source = metadata.get("source_updated_at_raw")
            try:
                source = datetime.fromisoformat(raw_source.replace("Z", "+00:00"))
                if source.tzinfo is None:
                    source = source.replace(tzinfo=ZoneInfo(self.store.settings()["source_timezone"]))
            except (AttributeError, TypeError, ValueError):
                raise ValueError("来源余额更新时间无法确认") from None
            self.message = "ego lite 已连接，本轮 JSON 全分页读取完成"
            return result, {
                "collected_at": now_iso(),
                "source_updated_at": source.isoformat(),
                "refresh_mode": "snapshot",
                "data_source": "json",
            }, metadata
        finally:
            self._busy = False

    def status(self):
        return {
            "browser_message": self.message,
            "browser_connected": bool(self.target and self.last_seen),
            "browser_last_seen": self.last_seen,
            "browser_target": self.target,
            "browser_provider": "ego_lite",
            "browser_busy": self._busy,
        }
