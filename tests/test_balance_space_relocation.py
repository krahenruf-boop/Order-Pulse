import tempfile
import unittest
from unittest.mock import patch
from order_pulse.storage import Repository
from order_pulse.balance.store import Store
from order_pulse.balance.bridge import BrowserBridge
from order_pulse.integrations.browser_binding import BrowserSpaceBinding
from order_pulse.integrations.ego_runtime import validate_target
from tests.test_browser_space_binding import binding

class SpaceRelocationTests(unittest.TestCase):
    def test_zero_is_valid_but_negative_and_boolean_ids_are_not(self):
        with tempfile.TemporaryDirectory() as d:
            manager=BrowserSpaceBinding(Repository(d));manager.save(binding(0))
            self.assertEqual(manager.target('balance')['space_id'],0)
            for value in (-1,True,'0'):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):manager.save(binding(value))
                    with self.assertRaises(ValueError):validate_target(dict(manager.target('balance'),space_id=value))

    def test_verified_relocation_persists_across_restart_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Repository(d);manager=BrowserSpaceBinding(repo);manager.save(binding(1))
            store=Store(d);store.set('browser_target',manager.target('balance'));settings=store.settings()
            repo.set('target',{'space_id':4,'page_label':'p7'})
            bridge=BrowserBridge(store,browser_binding=manager)
            browser=dict(space_id=0,space_name=binding()['expected_name'],profile_id='Default',profile_name='昊',page_label='p3')
            with patch('order_pulse.balance.bridge.execute',return_value={'ok':True,'browser':browser}):bridge._execute('inspect')
            self.assertEqual(bridge.target['space_id'],0)
            self.assertEqual(store.get('browser_target')['space_id'],0)
            self.assertEqual(BrowserSpaceBinding(Repository(d)).target('balance')['space_id'],0)
            self.assertEqual(manager.get()['tabs'],binding()['tabs'])
            self.assertEqual(repo.get('target'),{'space_id':4,'page_label':'p7'})
            self.assertEqual(store.settings(),settings)
            store.close()

    def test_wrong_or_stale_identity_cannot_overwrite_binding(self):
        with tempfile.TemporaryDirectory() as d:
            manager=BrowserSpaceBinding(Repository(d));manager.save(binding(1));expected=manager.target('balance')
            browser=dict(space_id=0,space_name=binding()['expected_name'],profile_id='Default',profile_name='昊')
            for change in ({'profile_name':'another user'},{'space_name':'Space-GitHub'},{'space_id':True}):
                with self.assertRaises(ValueError):manager.record_balance_space(expected,dict(browser,**change))
            manager.save(binding(3))
            with self.assertRaises(ValueError):manager.record_balance_space(expected,browser)
            self.assertEqual(manager.get()['space_id'],3)

if __name__=='__main__':unittest.main()
