"""Read the recharge export from bytes without trusting XLSX dimensions.

No spreadsheet content is executed and no archive member is extracted to disk.
Identifiers never pass through floating point. Amount strings retain exact
decimal precision, with at least two fractional digits for whole/cents values.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from io import BytesIO
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


FIELDS = {
    "资产ID": "asset_id", "名称": "source_name", "BC ID": "bc_id",
    "广告主状态": "advertiser_status", "现金余额": "cash_balance",
    "赠款余额": "grant_balance", "Company": "company", "新客类型": "customer_type",
    "所属年份季度": "year_quarter", "投放国家": "delivery_country",
}
REQUIRED = tuple(FIELDS)[:6]
_DASHES = {"-", "—", "–", "－", "'-"}
_CELL_REF = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")
_MAX_ARCHIVE = 20 * 1024 * 1024
_MAX_UNPACKED = 64 * 1024 * 1024


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(archive: ZipFile, path: str) -> ET.Element:
    content = archive.read(path)
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("XLSX 含不支持的 XML 实体声明")
    return ET.fromstring(content)


def _text(element: ET.Element) -> str:
    # Rich strings use several <r><t> fragments. Phonetic annotations are
    # metadata and must not become part of an identifier or a header.
    result = []
    for child in element:
        if _local(child.tag) == "t":
            result.append(child.text or "")
        elif _local(child.tag) == "r":
            result.extend(part.text or "" for part in child if _local(part.tag) == "t")
    return "".join(result)


def _cell_value(cell: ET.Element, strings: list[str]) -> tuple[str, str, bool]:
    kind = cell.get("t", "n")
    children = {_local(child.tag): child for child in cell}
    formula = "f" in children
    value = children.get("v")
    raw = (value.text or "") if value is not None else ""
    if kind == "inlineStr":
        inline = children.get("is")
        return (_text(inline) if inline is not None else "", kind, formula)
    if kind == "s":
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) >= len(strings):
            raise ValueError("XLSX 共享文本索引无效")
        raw = strings[int(raw)]
    elif kind not in {"n", "str", "e", "b", "d"}:
        raise ValueError(f"XLSX 单元格类型不受支持：{kind}")
    return raw, kind, formula


def _decimal_string(amount: Decimal) -> str:
    if amount == 0:
        return "0.00"
    raw = format(amount, "f")
    if "." not in raw:
        return raw + ".00"
    whole, fractional = raw.split(".", 1)
    return whole + "." + fractional.rstrip("0").ljust(2, "0")


def _amount(value: tuple[str, str, bool], field: str, row: int) -> tuple[str | None, str]:
    raw, kind, formula = value
    raw = raw.strip()
    if formula or kind in {"b", "e", "d"}:
        raise ValueError(f"第 {row} 行“{field}”不是可验证的金额，不能使用公式或错误单元格")
    if not raw:
        return None, "blank"
    if raw in _DASHES:
        return None, "dash"
    if len(raw) > 64 or not _NUMBER.fullmatch(raw):
        raise ValueError(f"第 {row} 行“{field}”金额格式无效")
    try:
        number = Decimal(raw)
        if not number.is_finite() or abs(number.as_tuple().exponent) > 12 or len(number.as_tuple().digits) > 36:
            raise ValueError(f"第 {row} 行“{field}”金额超出可解析范围")
        return _decimal_string(number), "valid"
    except InvalidOperation as exc:
        raise ValueError(f"第 {row} 行“{field}”金额格式无效") from exc


def _identifier(value: tuple[str, str, bool], field: str, row: int) -> str:
    raw, kind, formula = value
    raw = raw.strip()
    if formula or kind in {"b", "e", "d"} or not raw:
        raise ValueError(f"第 {row} 行“{field}”为空或格式无效")
    if kind == "n":
        # Excel numeric cells only preserve 15 significant digits reliably.
        # Even an apparently exact 19-digit XML value is unsafe to accept.
        if not re.fullmatch(r"[0-9]{1,15}", raw):
            raise ValueError(f"第 {row} 行“{field}”为数值型长 ID，可能已丢失精度；请导出文本型 ID")
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"第 {row} 行“{field}”必须为完整数字文本")
    return raw


def parse_xlsx(data: bytes) -> dict:
    """Parse Sheet1; raise ValueError on any invalid core record.

    Missing balances are returned as None with status ``blank`` or ``dash``;
    they are never coerced to zero. Optional missing source fields are empty
    strings, and their absent columns are reported in warnings.
    """
    if not isinstance(data, bytes) or not data or len(data) > _MAX_ARCHIVE:
        raise ValueError("导出内容为空、不是字节内容或超过 20 MB")
    if data.lstrip().startswith((b"<", b"<!")):
        raise ValueError("下载内容是网页而非 XLSX；请检查充值系统登录是否失效")
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2000 or sum(info.file_size for info in infos) > _MAX_UNPACKED:
                raise ValueError("XLSX 解压内容超出安全读取范围")
            if len({info.filename for info in infos}) != len(infos):
                raise ValueError("XLSX 包含重复文件项")
            workbook = _xml(archive, "xl/workbook.xml")
            sheet = next((e for e in workbook.iter() if _local(e.tag) == "sheet" and e.get("name") == "Sheet1"), None)
            if sheet is None:
                raise ValueError("XLSX 缺少 Sheet1 工作表")
            relation_id = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
            rels = _xml(archive, "xl/_rels/workbook.xml.rels")
            relation = next((e for e in rels if e.get("Id") == relation_id), None)
            if relation is None or relation.get("TargetMode") == "External":
                raise ValueError("Sheet1 工作表关联无效")
            target = relation.get("Target", "")
            path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
            if not path.startswith("xl/") or "\\" in path:
                raise ValueError("Sheet1 工作表路径无效")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = [_text(e) for e in _xml(archive, "xl/sharedStrings.xml") if _local(e.tag) == "si"]
            worksheet = _xml(archive, path)
            rows = []
            seen_rows = set()
            for row in worksheet.iter():
                if _local(row.tag) != "row":
                    continue
                row_number = int(row.get("r", "0"))
                if row_number < 1 or row_number > 1_048_576 or row_number in seen_rows:
                    raise ValueError("XLSX 行号无效或重复")
                seen_rows.add(row_number)
                values = {}
                for cell in row:
                    if _local(cell.tag) != "c":
                        continue
                    ref = _CELL_REF.fullmatch(cell.get("r", ""))
                    if not ref or int(ref[2]) != row_number:
                        raise ValueError("XLSX 单元格坐标无效")
                    column = 0
                    for letter in ref[1]:
                        column = column * 26 + ord(letter) - ord("A") + 1
                    if column > 16_384 or column in values:
                        raise ValueError("XLSX 列号无效或重复")
                    values[column] = _cell_value(cell, strings)
                if any(value[0].strip() or value[2] for value in values.values()):
                    rows.append((row_number, values))
                if len(rows) > 100_001:
                    raise ValueError("XLSX 超过 100000 条账户记录")
    except (BadZipFile, KeyError, ET.ParseError, OSError, OverflowError, RuntimeError) as exc:
        raise ValueError("XLSX 损坏或结构无效，请重新刷新并导出") from exc
    if not rows:
        raise ValueError("Sheet1 没有表头和账户记录")
    rows.sort(key=lambda item: item[0])
    _, raw_headers = rows[0]
    headers = {}
    for column, value in sorted(raw_headers.items()):
        raw, kind, formula = value
        header = raw.strip()
        if formula or kind in {"e", "b", "d"}:
            raise ValueError("XLSX 表头格式无效")
        if not header:
            continue
        if header in headers.values():
            raise ValueError(f"XLSX 表头重复：{header}")
        headers[column] = header
    missing = [field for field in REQUIRED if field not in headers.values()]
    if missing:
        raise ValueError("XLSX 缺少必需字段：" + "、".join(missing))
    warnings = [f"辅助字段未提供：{field}" for field in FIELDS if field not in headers.values()]
    warnings.extend(f"发现新增字段，已忽略：{field}" for field in headers.values() if field not in FIELDS)
    dimension = next((e.get("ref", "") for e in worksheet if _local(e.tag) == "dimension"), "")
    if dimension == "A1" and (len(rows) > 1 or len(headers) > 1):
        warnings.append("工作表声明范围为 A1，已按实际单元格读取完整数据")
    accounts = []
    seen_ids = set()
    for row_number, cells in rows[1:]:
        source = {header: cells.get(column, ("", "n", False)) for column, header in headers.items()}
        # Ignore formatting-only rows and entirely empty declared fields.
        if not any(value[0].strip() or value[2] for key, value in source.items() if key in FIELDS):
            continue
        account = {internal: "" for internal in FIELDS.values()}
        for header, internal in FIELDS.items():
            value = source.get(header, ("", "n", False))
            if internal in {"asset_id", "bc_id"}:
                account[internal] = _identifier(value, header, row_number)
            elif internal in {"cash_balance", "grant_balance"}:
                account[internal], account[internal + "_status"] = _amount(value, header, row_number)
            else:
                if value[2] or value[1] == "e":
                    raise ValueError(f"第 {row_number} 行“{header}”含公式或错误，无法核验来源文本")
                account[internal] = value[0].strip()
        asset_id = account["asset_id"]
        if asset_id in seen_ids:
            raise ValueError(f"XLSX 资产ID重复：{asset_id}")
        seen_ids.add(asset_id)
        account["year_quarter_raw"] = account["year_quarter"]
        if account["year_quarter"] in _DASHES:
            account["year_quarter"] = ""
        accounts.append(account)
    if not accounts:
        raise ValueError("XLSX 未包含账户记录；请确认资产分类及筛选条件")
    with localcontext() as context:
        context.prec = 64
        totals = {
            metric: _decimal_string(sum((Decimal(a[metric]) for a in accounts if a[metric] is not None), Decimal(0)))
            for metric in ("cash_balance", "grant_balance")
        }
    return {"accounts": accounts, "columns": list(headers.values()), "warnings": warnings,
            "cash_total": totals["cash_balance"], "grant_total": totals["grant_balance"]}
