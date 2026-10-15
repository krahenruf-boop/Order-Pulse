"""Product catalog parsing and deterministic local grouping.

The catalog contains no buyer/order data.  Source spreadsheets are parsed from
the application's private temporary directory and are never retained here.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import xlrd


RULE_VERSION = "product-family-v1"
PRODUCT_FIELDS = {
    "仓库": "warehouse",
    "库存SKU": "inventory_sku",
    "卖家SKU": "seller_sku",
    "产品中文名": "product_name_cn",
    "产品英文名": "product_name_en",
    "状态": "status",
    "虚拟SKU": "virtual_sku",
}
COMBO_BASE = ("组合sku编号", "组合sku名称", "组合sku英文名称", "组合sku图片",
              "虚拟sku", "长", "宽", "高", "关联sku种类数")


class CatalogError(Exception):
    """Stable public error code; raw cells and URLs never enter the message."""


def _book(path):
    path = Path(path)
    data = path.read_bytes()
    if path.suffix.lower() != ".xls" or data[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
        raise CatalogError("CATALOG_FILE_INVALID")
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception:
        raise CatalogError("CATALOG_FILE_INVALID") from None
    if book.nsheets != 1:
        raise CatalogError("CATALOG_SCHEMA_CHANGED")
    return book, data


def _identifier(sheet, row, column, required=False):
    cell = sheet.cell(row, column)
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        value = ""
    elif cell.ctype == xlrd.XL_CELL_TEXT:
        value = cell.value.strip()
    else:
        raise CatalogError("CATALOG_NUMERIC_SKU")
    if required and not value:
        raise CatalogError("CATALOG_SKU_MISSING")
    return value


def _text(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _aliases(value):
    return sorted({part.strip() for part in re.split(r"[;；\n]+", value or "") if part.strip()})


def read_product_catalog_xls(path, expected_rows):
    book, data = _book(path)
    try:
        sheet = book.sheet_by_index(0)
        headers = [_text(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
        if len(headers) != len(set(headers)) or set(headers) != set(PRODUCT_FIELDS):
            raise CatalogError("CATALOG_SCHEMA_CHANGED")
        if sheet.nrows - 1 != expected_rows:
            raise CatalogError("CATALOG_EXPORT_INCOMPLETE")
        index = {name: headers.index(name) for name in headers}
        items = []
        for row in range(1, sheet.nrows):
            sku = _identifier(sheet, row, index["库存SKU"], True)
            seller = _identifier(sheet, row, index["卖家SKU"])
            virtual = _identifier(sheet, row, index["虚拟SKU"])
            item = dict(
                inventory_sku=sku,
                seller_sku=seller,
                seller_aliases=_aliases(seller),
                virtual_sku=virtual,
                virtual_aliases=_aliases(virtual),
                product_name_cn=_text(sheet.cell_value(row, index["产品中文名"])),
                product_name_en=_text(sheet.cell_value(row, index["产品英文名"])),
                status=_text(sheet.cell_value(row, index["状态"])),
                warehouse=_text(sheet.cell_value(row, index["仓库"])),
                source="TMS_PRODUCT_EXPORT",
            )
            if item["status"] != "正常":
                raise CatalogError("CATALOG_STATUS_SCOPE")
            items.append(item)
        return {
            "items": items,
            "evidence": {
                "headers": headers,
                "rows": len(items),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "empty_chinese_names": sum(not i["product_name_cn"] for i in items),
            },
        }
    finally:
        book.release_resources()


def _positive_integer(value):
    if isinstance(value, bool):
        raise CatalogError("CATALOG_COMPONENT_QUANTITY_INVALID")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise CatalogError("CATALOG_COMPONENT_QUANTITY_INVALID") from None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise CatalogError("CATALOG_COMPONENT_QUANTITY_INVALID")
    return int(number)


def read_combo_catalog_xls(path, expected_rows):
    book, data = _book(path)
    try:
        sheet = book.sheet_by_index(0)
        headers = [_text(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
        if headers[:len(COMBO_BASE)] != list(COMBO_BASE) or len(headers) != len(set(headers)):
            raise CatalogError("CATALOG_SCHEMA_CHANGED")
        component_pairs = []
        suffix = 1
        while f"关联sku编号{suffix}" in headers or f"关联sku数量{suffix}" in headers:
            if f"关联sku编号{suffix}" not in headers or f"关联sku数量{suffix}" not in headers:
                raise CatalogError("CATALOG_SCHEMA_CHANGED")
            component_pairs.append((headers.index(f"关联sku编号{suffix}"), headers.index(f"关联sku数量{suffix}")))
            suffix += 1
        expected_headers = set(COMBO_BASE) | {headers[c] for pair in component_pairs for c in pair}
        if set(headers) != expected_headers or not component_pairs:
            raise CatalogError("CATALOG_SCHEMA_CHANGED")
        if sheet.nrows - 1 != expected_rows:
            raise CatalogError("CATALOG_EXPORT_INCOMPLETE")
        idx = {h: headers.index(h) for h in headers}
        combos = []
        for row in range(1, sheet.nrows):
            combo_sku = _identifier(sheet, row, idx["组合sku编号"], True)
            declared = _positive_integer(sheet.cell_value(row, idx["关联sku种类数"]))
            components = Counter()
            for sku_col, qty_col in component_pairs:
                sku = _identifier(sheet, row, sku_col)
                qty_value = sheet.cell_value(row, qty_col)
                if not sku and _text(qty_value):
                    raise CatalogError("CATALOG_COMPONENT_MISSING")
                if sku:
                    components[sku] += _positive_integer(qty_value)
            if declared != len(components) or not components:
                raise CatalogError("CATALOG_COMPONENT_INCOMPLETE")
            raw = ";".join(f"{sku}*{qty}" for sku, qty in sorted(components.items()))
            virtual = _identifier(sheet, row, idx["虚拟sku"])
            combos.append(dict(
                combo_sku=combo_sku,
                product_name_cn=_text(sheet.cell_value(row, idx["组合sku名称"])),
                product_name_en=_text(sheet.cell_value(row, idx["组合sku英文名称"])),
                virtual_sku=virtual,
                virtual_aliases=_aliases(virtual),
                raw_combo_definition=raw,
                components=[{"component_sku": sku, "component_quantity": qty}
                            for sku, qty in sorted(components.items())],
                source="TMS_COMBO_EXPORT",
            ))
        if len({c["combo_sku"] for c in combos}) != len(combos):
            raise CatalogError("CATALOG_COMBO_DUPLICATE")
        return {
            "combos": combos,
            "evidence": {
                "headers": headers,
                "rows": len(combos),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "component_relations": sum(len(c["components"]) for c in combos),
            },
        }
    finally:
        book.release_resources()


def normalize_product_name(value):
    raw = unicodedata.normalize("NFKC", value or "").strip().lower()
    variants = re.findall(r"\d+(?:\.\d+)?\s*(?:寸|cm|厘米|mm|毫米)|(?:黑|白|红|蓝|绿|紫|粉|灰|银|金)色", raw)
    packs = re.findall(r"(?:pack\s*of\s*\d+|\d+\s*(?:个|只|件|包|pcs?)\s*装?|[x×*]\s*\d+)", raw)
    core = raw
    for token in packs + variants:
        core = core.replace(token, " ")
    core = re.sub(r"(?:套装|组合装|多件装|包装)$", " ", core)
    core = re.sub(r"[\s\-_/，,。.;；:：()（）\[\]【】]+", "", core)
    return {"raw": value or "", "core": core, "pack_tokens": packs, "variant_tokens": variants}


def normalize_sku(value):
    raw = unicodedata.normalize("NFKC", value or "").strip().upper()
    root = re.sub(r"(?:[X×*]\d+(?:PCS?)?)$", "", raw)
    return {"raw": value or "", "normalized": re.sub(r"\s+", "", raw), "root": re.sub(r"\s+", "", root)}


def _family_id(core, skus):
    identity = "name:" + core if core else "sku:" + min(skus)
    return "pf_" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def _deduplicate_items(rows):
    grouped = {}
    for row in rows:
        sku = row["inventory_sku"]
        existing = grouped.get(sku)
        if existing is None:
            warehouses = row.get("warehouses") or ([row.get("warehouse")] if row.get("warehouse") else [])
            grouped[sku] = dict(row, warehouses=sorted(set(warehouses)))
            continue
        comparable = ("product_name_cn", "product_name_en", "status")
        if any(existing[k] != row[k] for k in comparable):
            raise CatalogError("CATALOG_SKU_CONFLICT")
        warehouses = row.get("warehouses") or ([row.get("warehouse")] if row.get("warehouse") else [])
        existing["warehouses"] = sorted(set(existing["warehouses"] + warehouses))
        existing["seller_aliases"] = sorted(set(existing["seller_aliases"] + row["seller_aliases"]))
        existing["virtual_aliases"] = sorted(set(existing["virtual_aliases"] + row["virtual_aliases"]))
    result = []
    for sku in sorted(grouped):
        item = grouped[sku]
        # The complete sorted warehouse set is the stable catalog field.  Keeping
        # whichever duplicate row happened to appear first would make identical
        # exports produce different catalog hashes.
        item.pop("warehouse", None)
        result.append(item)
    return result


def default_overrides():
    return {"revision": 1, "members": {}, "families": {}}


def build_families(product_rows, combos, overrides=None):
    overrides = overrides or default_overrides()
    items = _deduplicate_items(product_rows)
    by_sku = {i["inventory_sku"]: i for i in items}
    normalized = {sku: normalize_product_name(i["product_name_cn"]) for sku, i in by_sku.items()}
    groups = []
    used = set()
    exact = defaultdict(list)
    for sku, value in normalized.items():
        if value["core"]:
            exact[value["core"]].append(sku)
    for core in sorted(exact):
        members = sorted(exact[core])
        groups.append((core, members, "CORE_NAME_EXACT", "1.0000"))
        used.update(members)
    for sku in sorted(set(by_sku) - used):
        groups.append((normalized[sku]["core"], [sku], "SEPARATE", "1.0000"))

    families = {}
    sku_to_family = {}
    for core, members, reason, confidence in groups:
        family_id = _family_id(core, members)
        for sku in members:
            family_id = overrides.get("members", {}).get(sku, family_id)
            sku_to_family[sku] = family_id
        # A manual mapping can move members from several automatic groups into one.
    regrouped = defaultdict(list)
    for sku, family_id in sku_to_family.items():
        regrouped[family_id].append(sku)
    manual_families = overrides.get("families", {})
    for family_id, members in sorted(regrouped.items()):
        members = sorted(members)
        names = [by_sku[s]["product_name_cn"] for s in members if by_sku[s]["product_name_cn"]]
        chosen = Counter(names).most_common(1)[0][0] if names else members[0]
        manual = manual_families.get(family_id, {})
        core_values = {normalized[s]["core"] for s in members if normalized[s]["core"]}
        families[family_id] = {
            "product_family_id": family_id,
            "product_name": manual.get("name") or chosen,
            "member_skus": members,
            "manual_locked": bool(manual.get("locked")),
            "merge_source": "MANUAL" if any(overrides.get("members", {}).get(s) for s in members) else
                            ("CORE_NAME_EXACT" if len(members) > 1 and len(core_values) == 1 else "SEPARATE"),
            "confidence": "1.0000" if len(core_values) <= 1 else "0.9300",
            "reasons": ["人工覆盖"] if any(overrides.get("members", {}).get(s) for s in members) else
                       (["规范化中文核心名称完全一致"] if len(members) > 1 else ["证据不足，保持独立产品组"]),
        }

    review = []
    skus = sorted(by_sku)
    for i, left in enumerate(skus):
        if not normalized[left]["core"]:
            continue
        for right in skus[i + 1:]:
            if sku_to_family[left] == sku_to_family[right] or not normalized[right]["core"]:
                continue
            name_score = SequenceMatcher(None, normalized[left]["core"], normalized[right]["core"]).ratio()
            sku_score = SequenceMatcher(None, normalize_sku(left)["root"], normalize_sku(right)["root"]).ratio()
            if name_score >= .85:
                review.append({"left": left, "right": right, "name_similarity": f"{name_score:.4f}",
                               "sku_similarity": f"{sku_score:.4f}", "status": "REVIEW"})
    review.sort(key=lambda value: (-Decimal(value["name_similarity"]),
                                   -Decimal(value["sku_similarity"]), value["left"], value["right"]))
    # Review suggestions are advisory.  Bound the UI/storage payload without
    # changing any automatic family assignment.
    review = review[:500]

    ordinary_alias = {}
    ambiguous = set()
    for item in items:
        aliases = {item["inventory_sku"], *item["seller_aliases"], *item["virtual_aliases"]}
        for alias in filter(None, aliases):
            if alias in ordinary_alias and ordinary_alias[alias] != item["inventory_sku"]:
                ambiguous.add(alias)
            else:
                ordinary_alias[alias] = item["inventory_sku"]
    for alias in ambiguous:
        ordinary_alias.pop(alias, None)

    combo_by_sku = {c["combo_sku"]: c for c in combos}
    combo_alias = {}
    combo_ambiguous = set()
    for combo in combos:
        for alias in {combo["combo_sku"], *combo.get("virtual_aliases", [])}:
            if alias in combo_alias and combo_alias[alias] != combo["combo_sku"]:
                combo_ambiguous.add(alias)
            else:
                combo_alias[alias] = combo["combo_sku"]
    for alias in combo_ambiguous:
        combo_alias.pop(alias, None)

    def expand(combo_sku, stack=()):
        if combo_sku in stack:
            raise CatalogError("CATALOG_COMBO_CYCLE")
        result = Counter()
        for component in combo_by_sku[combo_sku]["components"]:
            sku, quantity = component["component_sku"], component["component_quantity"]
            nested = combo_alias.get(sku)
            if nested:
                for leaf, count in expand(nested, stack + (combo_sku,)).items():
                    result[leaf] += count * quantity
            else:
                result[ordinary_alias.get(sku, sku)] += quantity
        return result

    unresolved_components = set()
    for combo in combos:
        expanded = expand(combo["combo_sku"])
        combo["expanded_components"] = [{"component_sku": sku, "component_quantity": qty,
                                          "resolved": sku in sku_to_family}
                                         for sku, qty in sorted(expanded.items())]
        unresolved_components.update(sku for sku in expanded if sku not in sku_to_family)

    return {
        "items": items,
        "combos": sorted(combos, key=lambda c: c["combo_sku"]),
        "families": families,
        "sku_to_family": sku_to_family,
        "ordinary_aliases": ordinary_alias,
        "combo_aliases": combo_alias,
        "review_candidates": review,
        "ambiguous_aliases": sorted(ambiguous | combo_ambiguous),
        "unresolved_components": sorted(unresolved_components),
        "rule_version": RULE_VERSION,
        "mapping_revision": overrides.get("revision", 1),
    }


def catalog_content_hash(catalog):
    value = {k: catalog[k] for k in ("items", "combos", "rule_version")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
