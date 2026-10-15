import copy
import tempfile
import unittest
from types import SimpleNamespace

from order_pulse.balance.module import BalanceModule
from order_pulse.balance.store import now_iso, validate_settings
from order_pulse.balance.templates import LEGACY_FAILURE_TEMPLATE_KEYS


class BalanceSettingsUIContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = BalanceModule(self.tmp.name, SimpleNamespace(namespace=lambda _: {}))
        self.store = self.module.store

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def legacy_settings(self):
        settings = self.store.settings()
        for key in LEGACY_FAILURE_TEMPLATE_KEYS:
            settings[key] = {'legacy': '{unparsed'}
            settings['templates'][key] = {'legacy': '{unparsed'}
        self.store.set('settings', settings)
        return settings

    def test_legacy_values_load_and_active_template_save_preserves_them(self):
        before = self.legacy_settings()
        validate_settings(before)
        active = {key: before['templates'][key] for key in ('title', 'account', 'alert', 'recovery')}
        active['title'] = '余额汇总 · 测试标题'
        self.module.save_settings({'templates': active})
        expected = copy.deepcopy(before)
        expected['templates'].update(active)
        self.assertEqual(self.store.settings(), expected)

    def test_legacy_updates_are_ignored(self):
        before = self.legacy_settings()
        payload = {key: 'replace' for key in LEGACY_FAILURE_TEMPLATE_KEYS}
        payload['templates'] = dict(payload)
        self.module.save_settings(payload)
        self.assertEqual(self.store.settings(), before)

    def test_legacy_fields_are_not_added_to_new_settings(self):
        before = self.store.settings()
        self.module.save_settings({'failure_template': 'ignored', 'templates': {'failure': 'ignored'}})
        self.assertEqual(self.store.settings(), before)

    def test_invalid_active_template_leaves_settings_unchanged(self):
        before = self.legacy_settings()
        with self.assertRaises(ValueError):
            self.module.save_settings({'templates': {'alert': '{invalid_variable}'}})
        self.assertEqual(self.store.settings(), before)

    def account(self):
        stamp = now_iso()
        self.store.ingest({'accounts': [{'asset_id': 'test-account', 'source_name': '中文测试账户',
                          'cash_balance': '100.00', 'grant_balance': '20.00', 'balance_updated_at': stamp}],
                          'warnings': []}, {'collected_at': stamp, 'source_updated_at': stamp,
                                           'data_source': 'json', 'refresh_mode': 'snapshot'})
        return dict(asset_id='test-account', enabled=True, include_summary=True,
                    alert_enabled=False, recovery_enabled=True, shop_number='18', shop_name='测试店铺',
                    display_name='中文账户', alert_metric='grant_balance', threshold='150.25')

    def test_all_existing_account_fields_roundtrip_with_alert_disabled(self):
        values = self.account()
        self.module.save_accounts([values])
        self.module.save_accounts([values])
        saved = self.store.accounts()[0]
        self.assertEqual({key: saved[key] for key in values}, values)
        self.assertEqual(self.store.events(), [])

    def test_invalid_threshold_rejected_without_changing_account(self):
        values = self.account()
        self.module.save_accounts([values])
        before = self.store.accounts()[0]
        with self.assertRaisesRegex(ValueError, '额度'):
            self.module.save_accounts([dict(values, threshold='1.234')])
        self.assertEqual(self.store.accounts()[0], before)


if __name__ == '__main__':
    unittest.main()
