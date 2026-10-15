"""Validate the extension's allowlisted Linkme account snapshot.

The extension must preserve JSON number lexemes before JavaScript can round
them. Only the mapped fields below are returned; raw envelopes and credentials
must never be passed to this parser or persisted by its callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .xlsx_parser import _amount, _decimal_string, _DASHES


_REQUIRED = ("AccountID", "AccountName", "BCID", "RelationType", "AdvertiserShowStatus",
             "CashBalance", "GrantBalance", "BalanceUpdatedAt")
_OPTIONAL = ("Company", "NewCustomerType", "NewCustomerYearQuarter", "DeliveryCountries")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}"
                    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z")


def _timestamp(raw, source_zone, label):
    if not isinstance(raw, str) or not _STAMP.fullmatch(raw.strip()):
        raise ValueError(f"{label}缺失或时间格式无法确认")
    try:
        stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            first, second = stamp.replace(tzinfo=source_zone, fold=0), stamp.replace(tzinfo=source_zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError("来源时区的时间不存在或有歧义")
            if first.astimezone(timezone.utc).astimezone(source_zone).replace(tzinfo=None) != stamp:
                raise ValueError("来源时区的时间不存在")
            stamp = first
        return stamp
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label}或来源时区无法确认") from exc


def _fresh(stamp, now, max_age_seconds, label):
    age = (now - stamp).total_seconds()
    if age < -120:
        raise ValueError(f"{label}晚于当前时间，请核对来源时区和 Mac 时间")
    if age > max_age_seconds:
        raise ValueError(f"{label}已超过最长有效期，不能作为当前余额")


def _identifier(raw, field, row):
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,128}", raw):
        raise ValueError(f"第 {row} 条账户“{field}”必须为完整数字字符串，禁止数值型 ID")
    return raw


def _text(raw, field, row, *, required=False):
    if raw is None and not required:
        return ""
    if not isinstance(raw, str) or len(raw) > 10000 or (required and not raw.strip()):
        raise ValueError(f"第 {row} 条账户“{field}”为空或文本格式无效")
    return raw.strip()


def _balance(raw, field, row):
    if raw is None:
        return None, "blank"
    if not isinstance(raw, str) or not _NUMBER.fullmatch(raw):
        raise ValueError(f"第 {row} 条账户“{field}”必须为未丢失精度的 JSON 数字文本或空值")
    return _amount((raw, "str", False), field, row)


def parse_json_accounts(accounts, source_updated_at, source_timezone, max_age_minutes):
    """Return the existing XLSX parser result contract from allowlisted rows.

    Missing required keys fail the whole snapshot. Explicit null amounts remain
    missing balances, never zero. Every timestamp must be fresh independently;
    this prevents the store's common snapshot timestamp hiding stale row data.
    """
    if not isinstance(accounts, list) or not accounts or len(accounts) > 100000:
        raise ValueError("JSON 资产列表为空、格式无效或超过 100000 条账户")
    if isinstance(max_age_minutes, bool) or not isinstance(max_age_minutes, (int, float)) or not math.isfinite(max_age_minutes) or max_age_minutes <= 0:
        raise ValueError("来源余额最长有效期必须为正数")
    try:
        source_zone = ZoneInfo(source_timezone)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("来源时区无法确认") from exc
    now = datetime.now(timezone.utc)
    source = _timestamp(source_updated_at, source_zone, "列表来源更新时间")
    _fresh(source, now, max_age_minutes * 60, "列表来源更新时间")
    warnings = []
    missing_optional = set()
    missing_bc = False
    parsed, seen_ids = [], set()
    for row, raw in enumerate(accounts, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {row} 条账户不是有效 JSON 对象")
        missing = [field for field in _REQUIRED if field not in raw]
        if missing:
            raise ValueError(f"第 {row} 条账户缺少必需字段：" + "、".join(missing))
        asset_id = _identifier(raw["AccountID"], "AccountID", row)
        if asset_id in seen_ids:
            raise ValueError("JSON 资产ID重复，不能提交不完整的分页结果")
        seen_ids.add(asset_id)
        bc_id = raw["BCID"]
        if bc_id == "" and isinstance(bc_id, str):
            missing_bc = True
        else:
            bc_id = _identifier(bc_id, "BCID", row)
        if raw["RelationType"] != "OWNER_BC":
            raise ValueError(f"第 {row} 条账户不属于代理商资产 OWNER_BC")
        updated = _timestamp(raw["BalanceUpdatedAt"], source_zone, f"第 {row} 条账户余额更新时间")
        _fresh(updated, now, max_age_minutes * 60, f"第 {row} 条账户余额更新时间")
        if (updated - source).total_seconds() > 120:
            raise ValueError(f"第 {row} 条账户余额更新时间晚于列表来源时间，分页快照可能不一致")
        cash, cash_status = _balance(raw["CashBalance"], "CashBalance", row)
        grant, grant_status = _balance(raw["GrantBalance"], "GrantBalance", row)
        missing_optional.update(field for field in _OPTIONAL if field not in raw or raw[field] is None)
        advertiser_status = _text(raw["AdvertiserShowStatus"], "AdvertiserShowStatus", row, required=True)
        customer_type = _text(raw.get("NewCustomerType"), "NewCustomerType", row)
        year_quarter = _text(raw.get("NewCustomerYearQuarter"), "NewCustomerYearQuarter", row)
        countries = raw.get("DeliveryCountries")
        if countries is None:
            countries = []
        if not isinstance(countries, list) or len(countries) > 1000 or any(not isinstance(country, str) for country in countries):
            raise ValueError(f"第 {row} 条账户“DeliveryCountries”必须为字符串数组")
        delivery_country = ", ".join(_text(country, "DeliveryCountries", row) for country in countries)
        parsed.append({
            "asset_id": asset_id,
            "source_name": _text(raw["AccountName"], "AccountName", row, required=True),
            "bc_id": bc_id, "bc_id_status": "valid" if bc_id else "missing",
            "advertiser_status": {"STATUS_ENABLE": "审核通过"}.get(advertiser_status, advertiser_status),
            "advertiser_status_raw": raw["AdvertiserShowStatus"],
            "cash_balance": cash, "cash_balance_status": cash_status,
            "grant_balance": grant, "grant_balance_status": grant_status,
            "company": _text(raw.get("Company"), "Company", row),
            "customer_type": {"NONE": "非新客"}.get(customer_type, customer_type),
            "customer_type_raw": raw.get("NewCustomerType"),
            "year_quarter": "" if year_quarter in _DASHES else year_quarter,
            "year_quarter_raw": raw.get("NewCustomerYearQuarter"),
            "delivery_country": delivery_country,
            "delivery_country_raw": list(countries) if raw.get("DeliveryCountries") is not None else None,
            "relation_type": "OWNER_BC",
            "balance_updated_at": updated.isoformat(),
        })
    if missing_bc:
        warnings.append("本次 JSON 接口未提供 BC ID")
    warnings.extend("辅助字段未提供：" + field for field in _OPTIONAL if field in missing_optional)
    with localcontext() as context:
        context.prec = 64
        totals = {metric: _decimal_string(sum((Decimal(account[metric]) for account in parsed
                                               if account[metric] is not None), Decimal(0)))
                  for metric in ("cash_balance", "grant_balance")}
    return {"accounts": parsed, "columns": list(_REQUIRED) + list(_OPTIONAL), "warnings": warnings,
            "cash_total": totals["cash_balance"], "grant_total": totals["grant_balance"]}
