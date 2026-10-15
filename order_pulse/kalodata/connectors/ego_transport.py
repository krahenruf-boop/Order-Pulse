"""Direct ego-lite transport for the migrated Kalodata business service."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from ...integrations.ego_runtime import EgoRuntimeError, execute, validate_target

OPERATIONS = ["bind_product", "apply_period", "verify_context", "read_visible_page", "resolve_observed_link"]


class EgoTransport:
    def __init__(self, settings_provider, browser_lock=None):
        self.settings_provider = settings_provider
        self.lock = threading.RLock()
        self.context = None
        self.snapshot = None
        self.last_seen = 0.0
        self.last_error = None
        self._script = Path(__file__).resolve().parents[2] / "integrations" / "ego_kalodata.js"
        self.browser_lock = browser_lock
        self._job_lease = False

    async def begin_run(self):
        if self.browser_lock is None:return
        # Hold one lease for the whole extraction, including all detail pages.
        # Nonblocking attempts keep cancellation responsive while Balance owns it.
        while not self.browser_lock.acquire(blocking=False):
            await asyncio.sleep(.05)
        self._job_lease = True

    async def end_run(self):
        if self._job_lease:
            self._job_lease = False
            self.browser_lock.release()

    def _target(self):
        value = self.settings_provider().get("browser_target")
        return validate_target(value) if value else None

    def set_target(self, target):
        validate_target(target)
        with self.lock:
            self.context = None
            self.snapshot = None
            self.last_error = None

    def _run(self, operation, payload=None):
        target = self._target()
        if not target:
            return {"success": False, "error_code": "BLOCKED_TRANSPORT", "data": {"error": "browser target not configured"}}
        try:
            spec=dict(target, operation=operation, payload=payload or {})
            if self.browser_lock is None or self._job_lease:
                result = execute(self._script,spec,"ORDER_PULSE_KALODATA:",timeout=45,max_output=2_000_000)
            else:
                with self.browser_lock:
                    result = execute(self._script,spec,"ORDER_PULSE_KALODATA:",timeout=45,max_output=2_000_000)
        except EgoRuntimeError as exc:
            result = {"success": False, "error_code": "NETWORK_ERROR", "data": {"error": exc.code}}
        if result.get("success") is True:
            self.last_seen = time.monotonic()
            self.last_error = None
        else:
            self.last_error = result.get("error_code") or "NETWORK_ERROR"
        return result

    async def probe_session(self):
        result = await asyncio.to_thread(self._run, "probe", {})
        target = self._target()
        ready = bool(target and result.get("success") is True)
        return {
            "deployable": ready,
            "authorized": ready,
            "browser": "ego_lite",
            "session_label": "Order Pulse · Balance + Kalodata",
            "session_ref": f"ego-space-{target['space_id']}-{target['page_label']}" if target else None,
            "operations": OPERATIONS,
            "state": "READY" if ready else result.get("error_code", "BLOCKED_TRANSPORT"),
        }

    async def observe(self, operation, payload):
        if operation not in OPERATIONS + ["inspect_ui"]:
            raise ValueError("unsupported operation")
        with self.lock:
            augmented = dict(payload)
            if self.context:
                augmented["_page_size"] = self.context["page_size"]
            if operation == "verify_context" and self.snapshot:
                augmented.update(_expected_page=self.snapshot["page"], _row_ids=self.snapshot["ids"])
            if operation == "resolve_observed_link":
                if not self.context or not self.snapshot or self.snapshot["run"] != payload.get("run_id") or self.snapshot["page"] != payload.get("source_page"):
                    result = {"success": False, "error_code": "CONTEXT_CHANGED", "data": {"error": "context changed"}}
                else:
                    augmented.update(_expected_page=self.snapshot["page"], _row_ids=self.snapshot["ids"])
                    result = await asyncio.to_thread(self._run, operation, augmented)
            else:
                result = await asyncio.to_thread(self._run, operation, augmented)
            if result.get("success") and operation == "apply_period":
                self.context = result.get("data")
                self.snapshot = None
            if result.get("success") and operation == "read_visible_page":
                data = result.get("data") or {}
                self.snapshot = {"run": payload.get("run_id"), "page": payload.get("source_page"),
                                 "ids": [row.get("video_id") for row in data.get("rows", [])]}
            session = self._target()
            result.update(
                session_ref=f"ego-space-{session['space_id']}-{session['page_label']}" if session else None,
                operation_id=payload.get("operation_id"),
                run_id=payload.get("run_id"),
                generation=payload.get("generation"),
            )
            return result

    async def cancel(self, run_id, generation):
        with self.lock:
            if self.snapshot and self.snapshot.get("run") == run_id:
                self.snapshot = None

    def info(self):
        target = self._target()
        return {
            "connected": bool(target and time.monotonic() - self.last_seen < 60 and not self.last_error),
            "provider": "ego_lite",
            "space_name": target.get("space_name") if target else None,
            "page_label": target.get("page_label") if target else None,
            "space_id": target.get("space_id") if target else None,
            "auto_takeover": target.get("auto_takeover") if target else False,
            "state": self.last_error or ("READY" if target else "BINDING_REQUIRED"),
        }
