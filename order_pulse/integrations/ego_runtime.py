"""Bounded ego-lite command runner for an explicitly bound dedicated Space.

The browser scripts use the explicitly bound Space when ``auto_takeover`` is
enabled. Balance can resolve a renumbered ID only through its saved unique
name/profile identity; it never creates a Space.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

BALANCE_SPACE_NAMES = ("Order Pulse · 余额 · API监控", "Order Pulse · 余额监控", "Order Pulse · Balance + Kalodata")


class EgoRuntimeError(Exception):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def execute(script: Path, spec: dict, marker: str, *, timeout: int, max_output: int = 2_000_000) -> dict:
    program = "const INPUT=" + json.dumps(spec, ensure_ascii=True, separators=(",", ":")) + ";\n" + script.read_text()
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": "/private/tmp"}
    try:
        proc = subprocess.run(
            [str(Path.home() / ".local/bin/ego-browser"), "nodejs"],
            input=program,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EgoRuntimeError("BROWSER_TIMEOUT") from exc
    except OSError as exc:
        raise EgoRuntimeError("WAITING_CONNECTION") from exc
    output = proc.stdout + "\n" + proc.stderr
    lines = [line.strip()[len(marker):] for line in output.splitlines() if line.strip().startswith(marker)]
    if proc.returncode or len(output.encode()) > max_output or len(lines) != 1:
        raise EgoRuntimeError("WAITING_CONNECTION", {
            "returncode": proc.returncode,
            "marker_count": len(lines),
            "stdout_bytes": len(proc.stdout.encode()),
            "stderr_bytes": len(proc.stderr.encode()),
        })
    try:
        result = json.loads(lines[0])
    except ValueError as exc:
        raise EgoRuntimeError("SOURCE_CHANGED") from exc
    if not isinstance(result, dict):
        raise EgoRuntimeError("SOURCE_CHANGED")
    return result


def validate_target(value: object, *, require_page: bool = True) -> dict:
    if not isinstance(value, dict):
        raise ValueError("浏览器绑定无效")
    required = {"space_id", "space_name", "page_label"}
    allowed = required | {"expected_profile_id", "expected_profile_name", "purpose", "auto_takeover"}
    if not required <= set(value) or set(value) - allowed or type(value.get("space_id")) is not int or value["space_id"] < 0:
        raise ValueError("浏览器绑定无效")
    if not isinstance(value.get("space_name"),str) or not value["space_name"].strip() or len(value["space_name"])>150:
        raise ValueError("只能使用已绑定的 Order Pulse 余额监控专用空间")
    label = value.get("page_label")
    if require_page and (not isinstance(label, str) or not label.startswith("p") or not label[1:].isdigit()):
        raise ValueError("浏览器页面绑定无效")
    profile_id=value.get("expected_profile_id")
    profile_name=value.get("expected_profile_name")
    if profile_id is not None and (not isinstance(profile_id,str) or not profile_id or len(profile_id)>100):
        raise ValueError("浏览器配置身份无效")
    if profile_name is not None and (not isinstance(profile_name,str) or not profile_name or len(profile_name)>100):
        raise ValueError("浏览器账号身份无效")
    if value.get("purpose","balance_kalodata") != "balance_kalodata":
        raise ValueError("浏览器绑定用途无效")
    if type(value.get("auto_takeover",True)) is not bool:
        raise ValueError("浏览器自动接管设置无效")
    return dict(value, purpose="balance_kalodata", auto_takeover=value.get("auto_takeover",True))
