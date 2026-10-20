from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from order_pulse.balance.engine import Engine, due_slots
from order_pulse.balance.bridge import BrowserBridge
from order_pulse.balance.json_parser import parse_json_accounts
from order_pulse.balance.store import SOURCE, Store, now_iso
from order_pulse.kalodata.exports import make_export
from order_pulse.kalodata.repository import Repository as KalodataRepository
from order_pulse.kalodata.connectors.ego_transport import EgoTransport
from order_pulse.integrations.ego_runtime import validate_target
from order_pulse.server import create_app
from order_pulse.service import Service
from order_pulse.storage import Credentials, Repository


ASSET_ID = "7641475860268400660"


def balance_source_row(cash="12.34", grant="5.67"):
    stamp = datetime.now(timezone.utc).isoformat()
    return {
        "AccountID": ASSET_ID,
        "AccountName": "回归测试账户",
        "BCID": "7641475860268400661",
        "RelationType": "OWNER_BC",
        "AdvertiserShowStatus": "STATUS_ENABLE",
        "CashBalance": cash,
        "GrantBalance": grant,
        "BalanceUpdatedAt": stamp,
        "Company": "回归测试公司",
        "NewCustomerType": "NONE",
        "NewCustomerYearQuarter": "-",
        "DeliveryCountries": ["BR"],
    }, stamp








class BalanceModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ego_balance_uses_bound_space_and_verified_json_contract(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            target = {"space_id": 6, "space_name": "Order Pulse · Balance + Kalodata", "page_label": "p5"}
            store.set("browser_target", target)
            raw, stamp = balance_source_row()
            reply = {"ok": True, "json_accounts": [raw], "metadata": {
                "owner_bc": True, "filters_clear": True, "data_source": "json",
                "refresh_mode": "snapshot", "refresh_evidence": ["json_all_pages_verified", "json_every_page_rechecked"],
                "page_count": 1, "page_pagination_pages": 1, "source_updated_at_raw": stamp,
                "currency": store.settings()["currency"],
            }}
            bridge = BrowserBridge(store)
            with patch("order_pulse.balance.bridge.execute", return_value=reply) as run:
                result, meta, evidence = await bridge.collect()
            self.assertEqual(result["accounts"][0]["asset_id"], ASSET_ID)
            self.assertEqual(result["accounts"][0]["cash_balance"], "12.34")
            self.assertEqual(result["accounts"][0]["grant_balance"], "5.67")
            self.assertEqual(meta["data_source"], "json")
            self.assertEqual(run.call_args.args[1]["space_name"], "Order Pulse · Balance + Kalodata")
            store.close()

    async def test_json_contract_cash_grant_decimal_and_failure_retains_values(self):
        raw, stamp = balance_source_row("1234567890.12", "0.34")
        parsed = parse_json_accounts([raw], stamp, "Asia/Shanghai", 30)
        self.assertEqual(parsed["cash_total"], "1234567890.12")
        self.assertEqual(parsed["grant_total"], "0.34")
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            meta = {"collected_at": now_iso(), "source_updated_at": stamp, "refresh_mode": "snapshot", "data_source": "json"}
            store.ingest(parsed, meta); store.set("collection", dict(meta, status="success", parse_count=1))
            before = store.accounts()[0]
            class FailedBridge:
                async def collect(self): raise ValueError("本轮读取失败")
            secrets = SimpleNamespace(status=lambda: {"webhook_configured": False}, get=lambda: {})
            engine = Engine(store, FailedBridge(), secrets)
            await engine._run_cycle({"alerts": False, "summary": False, "slots": [], "triggers": {"query"}})
            after = store.accounts()[0]
            self.assertEqual(after["cash_balance"], before["cash_balance"])
            self.assertEqual(after["grant_balance"], before["grant_balance"])
            self.assertEqual(after["data_status"], "failed")
            store.close()

    async def test_shared_scheduler_uses_balance_namespace_and_restart_marks_send_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            data = {"source_name": "账户", "cash_balance": "12.00", "grant_balance": "3.00", "balance_updated_at": now_iso()}
            meta = {"collected_at": now_iso(), "source_updated_at": now_iso(), "refresh_mode": "snapshot", "data_source": "json"}
            store.ingest({"accounts": [dict(data, asset_id=ASSET_ID)], "warnings": []}, meta)
            store.update_accounts([{"asset_id": ASSET_ID, "enabled": True, "include_summary": True}])
            settings = store.settings(); settings.update(paused=False, setup_complete=True, currency_confirmed=True, source_timezone_confirmed=True, account_scope_confirmed=True,
                schedules=[{"id": "am", "time": "09:30", "enabled": True}]); store.set("settings", settings)
            secrets = SimpleNamespace(status=lambda: {"webhook_configured": False}, get=lambda: {})
            engine = Engine(store, None, secrets)
            slot = {"key": "2026-09-24|09:30|Asia/Shanghai", "at": now_iso()}
            engine.queue_summary([slot], manual=True)
            self.assertEqual(store.db.execute("SELECT count(*) FROM balance_schedules").fetchone()[0], 1)
            event = store.events()[0]; store.update_event(event["id"], "sending", "发送中")
            store.recover_sending(); self.assertEqual(store.events()[0]["status"], "uncertain")
            self.assertIsNone(engine.scheduler_task, "Balance must not own a second long-running scheduler")
            slots = due_slots(settings, datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc), datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc))
            self.assertEqual(slots[0]["key"], "2026-09-24|09:30|Asia/Shanghai")
            store.close()


class KalodataModuleTests(unittest.TestCase):
    def test_binding_names_can_change_but_empty_identity_and_wrong_purpose_fail(self):
        for name in ('Order Pulse · 余额 · API监控','已重新命名的财务空间'):
            self.assertEqual(validate_target({'space_id':6,'space_name':name,'page_label':'p5'})['space_name'],name)
        for target in ({'space_id':2,'space_name':'','page_label':'p1'},{'space_id':3,'space_name':'财务','page_label':'p1','purpose':'material_aov'}):
            with self.assertRaises(ValueError):validate_target(target)

    def test_ego_kalodata_transport_keeps_page_context_and_correlation(self):
        target = {"space_id": 6, "space_name": "Order Pulse · Balance + Kalodata", "page_label": "p6"}
        transport = EgoTransport(lambda: {"browser_target": target})
        context = {"page_size": 20}
        responses = [
            {"success": True, "data": {"url": "https://www.kalodata.com/product/detail?id=1"}},
            {"success": True, "data": context},
            {"success": True, "data": {"source_page": 1, "rows": [{"video_id": "1"}]}},
        ]
        async def scenario():
            with patch("order_pulse.kalodata.connectors.ego_transport.execute", side_effect=responses) as run:
                cap = await transport.probe_session()
                applied = await transport.observe("apply_period", {"operation_id": "1", "run_id": None, "generation": 0})
                page = await transport.observe("read_visible_page", {"operation_id": "2", "run_id": "run", "generation": 1, "source_page": 1})
            return cap, applied, page, run
        cap, applied, page, run = asyncio.run(scenario())
        self.assertTrue(cap["deployable"])
        self.assertEqual(cap["browser"], "ego_lite")
        self.assertEqual(applied["operation_id"], "1")
        self.assertEqual(page["run_id"], "run")
        self.assertEqual(transport.snapshot, {"run": "run", "page": 1, "ids": ["1"]})
        self.assertEqual(run.call_args_list[-1].args[1]["space_name"], "Order Pulse · Balance + Kalodata")

    def test_rank_order_dedupe_and_txt_csv_are_identical(self):
        with tempfile.TemporaryDirectory() as d:
            Repository(d)
            repo = KalodataRepository(Path(d) / "order_pulse.sqlite3"); repo.open(True)
            request = {"start_page": 1, "end_page": 2, "period": "7D"}
            context = {"visible_pages": 2, "product_id": "1", "region": "US", "period": "7D", "actual_start_date": "2026-09-18", "actual_end_date": "2026-09-24"}
            run = repo.create("run", "client", request, context); generation = run["generation"]
            repo.page("run", generation, 1, 20, 2, "page-1", now_iso())
            repo.row("run", generation, 1, 1, "111", "detail-111")
            repo.resolve("run", generation, 1, 1, "https://www.tiktok.com/@x/video/111", {"ok": True}, "detail-111")
            repo.row("run", generation, 1, 2, "222", "detail-222")
            repo.resolve("run", generation, 1, 2, "https://www.tiktok.com/@x/video/222", {"ok": True}, "detail-222")
            repo.verify_page("run", generation, 1)
            repo.page("run", generation, 2, 20, 1, "page-2", now_iso())
            repo.row("run", generation, 2, 1, "111", "detail-111")
            repo.verify_page("run", generation, 2); repo.finish("run", "completed")
            run = repo.get_run("run")
            self.assertEqual([x["video_id"] for x in run["items"]], ["111", "222"])
            self.assertEqual(run["counts"]["duplicate_occurrences"], 1)
            txt, _, _ = make_export(run, "txt"); csv_bytes, _, _ = make_export(run, "csv")
            txt_urls = txt.decode().splitlines()
            rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            self.assertEqual(txt_urls, [r["tiktok_url"] for r in rows])
            self.assertEqual([r["export_index"] for r in rows], ["1", "2"])
            repo.close()


class SeparatedAppTests(unittest.TestCase):
    def test_routes_settings_security_and_page_reads_do_not_collect(self):
        for role,port in [('finance',8786),('materials',8797)]:
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);service=Service(root,role=role)
                client=TestClient(create_app(root,service=service,role=role,port=port),base_url=f'http://127.0.0.1:{port}')
                state=client.get('/api/state').json();headers={'Origin':f'http://127.0.0.1:{port}','X-CSRF-Token':state['csrf']}
                page=client.get('/').text
                self.assertIn('余额监控' if role=='finance' else '素材情报',page)
                self.assertNotIn('素材情报' if role=='finance' else '余额监控',page)
                self.assertEqual(client.get('/api/state',headers={'Host':'evil.invalid'}).status_code,403)
                self.assertEqual(client.get('/api/aov-monitor/state' if role=='finance' else '/api/balance/state').status_code,404)
                if role=='finance':
                    current=client.get('/api/balance/state').json()['settings']
                    self.assertEqual(client.post('/api/balance/settings',json=current,headers=headers).status_code,200)
                else:
                    current=client.get('/api/kalodata/settings').json();current.update(default_period='7D')
                    self.assertEqual(client.post('/api/kalodata/settings',json=current,headers=headers).status_code,200)

if __name__=='__main__':unittest.main()
