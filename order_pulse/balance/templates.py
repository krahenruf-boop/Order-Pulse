"""Strict plain-text template rendering, with no expression evaluation."""
from __future__ import annotations

import re
import string
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VARIABLES = {
    "shop_number": "店铺编号", "shop_name": "店铺名", "display_name": "展示名称",
    "source_name": "原始账户名称", "asset_id": "资产ID", "bc_id": "BC ID",
    "cash_balance": "现金余额", "grant_balance": "赠款余额", "currency": "币种",
    "company": "公司", "advertiser_status": "广告主状态", "customer_type": "新客类型",
    "year_quarter": "所属年份季度", "delivery_country": "投放国家",
    "alert_metric": "预警依据", "threshold": "预警额度", "balance_status": "余额状态",
    "collected_at": "采集时间", "source_updated_at": "来源更新时间",
    "current_balance": "触发指标当前值", "reason": "触发原因", "error": "异常原因",
    "event_id": "事件编号", "data_status": "数据状态",
}

DEFAULT_TEMPLATES = {
    "title": "TikTok OPS 余额汇总",
    "account": "{shop_number} {shop_name}\n{display_name}\n现金余额：{currency} {cash_balance}\n赠款余额：{currency} {grant_balance}",
    "alert": "低余额预警\n{shop_number} {shop_name}\n{display_name}\n预警依据：{alert_metric}\n当前值：{currency} {current_balance}\n预警额度：{currency} {threshold}\n原因：{reason}",
    "recovery": "余额恢复正常\n{shop_number} {shop_name}\n{display_name}\n判断依据：{alert_metric}\n当前值：{currency} {current_balance}\n预警额度：{currency} {threshold}\n原因：{reason}",
}
TEMPLATE_LABELS = {"title": "消息标题", "account": "单账户模板", "alert": "低余额模板",
                   "recovery": "恢复模板"}
LEGACY_FAILURE_TEMPLATE_KEYS = {"failure", "failure_template", "error_template", "failure_message_template"}
IDENTIFIERS = {"shop_number", "shop_name", "display_name", "source_name", "asset_id"}
ALERT_REQUIRED = {"alert_metric", "current_balance", "threshold", "currency"}
_formatter = string.Formatter()


def _enabled(fields) -> set[str]:
    if isinstance(fields, dict):
        return {key for key, value in fields.items() if value}
    return set(fields)


def _references(template: str) -> set[str]:
    if not isinstance(template, str):
        raise ValueError("模板必须是文本")
    try:
        pieces = list(_formatter.parse(template))
    except ValueError:
        raise ValueError("模板花括号不匹配；变量请使用 {变量名}") from None
    refs = set()
    for _, variable, format_spec, conversion in pieces:
        if variable is None:
            continue
        if not re.fullmatch(r"[a-z_]+", variable or "") or variable not in VARIABLES:
            raise ValueError(f"未知变量：{{{variable}}}")
        if format_spec or conversion:
            raise ValueError("模板变量不支持格式表达式或代码，只能使用 {变量名}")
        refs.add(variable)
    return refs


def validate_templates(templates: dict, fields) -> list[str]:
    enabled = _enabled(fields)
    errors = []
    unknown_fields = enabled - VARIABLES.keys()
    if unknown_fields:
        errors.append("字段列表包含未知变量：" + "、".join(sorted(unknown_fields)))
    for name in DEFAULT_TEMPLATES:
        label = TEMPLATE_LABELS[name]
        template = templates.get(name, "")
        if not isinstance(template, str) or not template.strip():
            errors.append(f"{label}不能为空")
            continue
        try:
            refs = _references(template)
        except ValueError as exc:
            errors.append(f"{label}：{exc}")
            continue
        disabled = refs - enabled
        if disabled:
            labels = "、".join(VARIABLES[v] for v in sorted(disabled))
            errors.append(f"{label}仍引用已关闭字段：{labels}；请移除引用或重新勾选")
        if name in {"account", "alert", "recovery"} and not refs.intersection(IDENTIFIERS):
            errors.append(f"{label}至少需要一个账户标识（展示名称、原始名称、资产ID或店铺信息）")
        if name == "alert":
            missing = ALERT_REQUIRED - refs
            if missing:
                errors.append("低余额模板必须保留：" + "、".join(VARIABLES[v] for v in sorted(missing)))
    return errors


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def render_template(template: str, values: dict, fields) -> str:
    """Render whitelisted enabled fields; omit a whole line if any variable is empty.

    Decimal/int zero is a real value. Missing shop mappings therefore do not leave
    empty labels in the message. Literal braces can be written as {{ and }}.
    Values are never interpreted again as templates or markup.
    """
    refs = _references(template)
    disabled = refs - _enabled(fields)
    if disabled:
        raise ValueError("模板引用已关闭字段：" + "、".join(VARIABLES[v] for v in sorted(disabled)))
    identifiers = refs.intersection(IDENTIFIERS)
    if identifiers and all(_empty(values.get(variable)) for variable in identifiers):
        raise ValueError("账户标识为空，请配置展示名称、原始名称、资产ID或店铺信息")
    if ALERT_REQUIRED.issubset(refs) and any(_empty(values.get(variable)) for variable in ALERT_REQUIRED):
        raise ValueError("预警当前值、指标、额度或币种缺失，不能发送预警消息")
    lines = []
    for line in template.splitlines():
        pieces = list(_formatter.parse(line))
        line_refs = [variable for _, variable, _, _ in pieces if variable is not None]
        if any(_empty(values.get(variable)) for variable in line_refs):
            continue
        rendered = "".join(literal + (str(values[variable]) if variable is not None else "")
                           for literal, variable, _, _ in pieces).strip()
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def format_amount(value, currency: str = "BRL", style: str = "decimal") -> str:
    """Return only the numeric amount; caller renders currency separately.

    decimal / standard: 1,234.50 is deliberately NOT grouped => "1234.50".
    local / brazilian: "1.234,50". None/blank => ""; invalid/nonfinite raises.
    The currency argument is accepted for a stable UI-facing signature; this
    function never infers currency from country/account names.
    """
    if _empty(value):
        return ""
    if isinstance(value, (bool, float)):
        raise ValueError("金额必须使用十进制文本、Decimal 或整数，不能使用浮点数")
    try:
        amount = Decimal(value)
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("金额格式不正确") from None
    if style in {"decimal", "standard", "plain", "code"}:
        return f"{amount:.2f}"
    if style in {"local", "brazilian", "br", "symbol"}:
        return f"{amount:,.2f}".translate(str.maketrans({",": ".", ".": ","}))
    raise ValueError("金额显示格式不支持，请选择 decimal 或 local")
