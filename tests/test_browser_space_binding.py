from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from order_pulse.balance.bridge import BrowserBridge
from order_pulse.balance.store import Store
from order_pulse.integrations.browser_binding import BrowserSpaceBinding
from order_pulse.kalodata.connectors.ego_transport import EgoTransport
from order_pulse.storage import Repository


NAME = "Order Pulse · Balance + Kalodata"


def binding(space_id=6):
    return {
        "space_id": space_id,
        "expected_name": NAME,
        "profile_id": "Default",
        "profile_name": "昊",
        "purpose": "balance_kalodata",
        "auto_takeover": True,
        "tabs": {"balance_tab": "p1", "kalodata_tab": "p2"},
    }


class DedicatedBrowserBindingTests(unittest.TestCase):
    def test_entire_kalodata_run_keeps_balance_waiting_and_releases_on_exit(self):
        async def scenario():
            mutex=threading.Lock()
            transport=EgoTransport(lambda:{},browser_lock=mutex)
            await transport.begin_run()
            entered=threading.Event()
            def balance():
                with mutex:entered.set()
            worker=asyncio.create_task(asyncio.to_thread(balance))
            await asyncio.sleep(.02)
            self.assertFalse(entered.is_set())
            self.assertTrue(transport._job_lease)
            await transport.end_run()
            await asyncio.wait_for(worker,1)
            self.assertTrue(entered.is_set())
            self.assertFalse(mutex.locked())
        asyncio.run(scenario())

    def test_binding_persists_and_restores_both_tabs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = BrowserSpaceBinding(Repository(root))
            saved = first.save(binding(), tms_space_id=4)
            restored = BrowserSpaceBinding(Repository(root))
            self.assertEqual(restored.get(), saved)
            self.assertEqual(restored.target("balance")["page_label"], "p1")
            self.assertEqual(restored.target("kalodata")["page_label"], "p2")
            self.assertTrue(restored.target("balance")["auto_takeover"])

    def test_empty_name_and_tms_space_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = BrowserSpaceBinding(Repository(directory))
            wrong = binding(); wrong["expected_name"] = ""
            with self.assertRaises(ValueError):
                manager.save(wrong, tms_space_id=4)
            with self.assertRaises(ValueError):
                manager.save(binding(4), tms_space_id=4)
            self.assertIsNone(manager.get())

    def test_connectors_use_exact_id_and_one_bounded_takeover(self):
        root = Path(__file__).parents[1] / "order_pulse" / "integrations"
        for filename in ("ego_balance.js", "ego_kalodata.js"):
            source = (root / filename).read_text()
            self.assertIn("resolveBoundBalanceSpace(spaces,spec)" if filename=="ego_balance.js" else "find(s=>s.id===spec.space_id)", source)
            self.assertEqual(source.count("claimTaskSpace(entry.id)"), 1)
            self.assertEqual(source.count("takeOverTaskSpace(entry.id)"), 1)
            self.assertNotIn("taskSpace(spec.space_name)", source)
            self.assertNotIn("taskSpace(\"", source)
            self.assertIn("BOUND_SPACE_MISSING", source)
            if filename == "ego_balance.js":
                self.assertIn("u.origin==='https://tt.linkme.live'", source)
                self.assertIn("task.adopt(tab.page,options)", source)

    def test_balance_and_kalodata_share_one_browser_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            mutex = threading.Lock()
            store = Store(directory)
            target = {
                "space_id": 6, "space_name": NAME, "page_label": "p1",
                "expected_profile_id": "Default", "expected_profile_name": "昊",
                "purpose": "balance_kalodata", "auto_takeover": True,
            }
            store.set("browser_target", target)
            bridge = BrowserBridge(store, browser_lock=mutex)
            kd_target = dict(target, page_label="p2")
            transport = EgoTransport(lambda: {"browser_target": kd_target}, browser_lock=mutex)
            active = 0
            maximum = 0
            gate = threading.Lock()

            def fake_execute(script, spec, marker, **kwargs):
                nonlocal active, maximum
                with gate:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(.05)
                with gate:
                    active -= 1
                if marker == "ORDER_PULSE_BALANCE:":
                    return {"ok": True, "metadata": {"page_count": 27}}
                return {"success": True, "data": {"url": "https://www.kalodata.com/product/detail?id=1"}}

            async def scenario():
                with patch("order_pulse.balance.bridge.execute", side_effect=fake_execute), patch(
                    "order_pulse.kalodata.connectors.ego_transport.execute", side_effect=fake_execute
                ):
                    return await asyncio.gather(bridge.inspect(), transport.probe_session())

            balance_result, kalodata_result = asyncio.run(scenario())
            self.assertEqual(maximum, 1)
            self.assertEqual(balance_result["page_count"], 27)
            self.assertTrue(kalodata_result["deployable"])
            self.assertFalse(mutex.locked())
            store.close()


if __name__ == "__main__":
    unittest.main()
