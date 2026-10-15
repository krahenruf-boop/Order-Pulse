"""Wise contracts use invented rates and clocks; never access a real service."""
import io
import json
import logging
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from order_pulse.storage import Repository
from order_pulse.wise import (
    API_URL, PUBLIC_URL, MAX_BODY_BYTES, RateService, WiseRateError,
    WiseRateProvider, _NoRedirect, parse_public_converter, parse_rate_api,
)


NOW = datetime(2025, 2, 3, 12, 0, tzinfo=timezone.utc)
INVENTED_RATE = '0.812345'


def quote(at=NOW):
    return dict(fx_provider='wise', fx_pair='CNY/BRL', fx_rate=INVENTED_RATE,
                fx_fetched_at=at.isoformat(), fx_source_type='WISE_PUBLIC_CONVERTER',
                fx_source_reference=PUBLIC_URL, fx_status='LIVE')


def page(rate=INVENTED_RATE, shown='0.8123', source='CNY', target='BRL', timestamp=None):
    if timestamp is None:
        timestamp = int((NOW - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()) * 1000
    data = dict(page='/public/[urlLocale]/currency-converter/route', props=dict(pageProps=dict(model=dict(rate=dict(
        value='DECIMAL_PLACEHOLDER', sourceCurrency=dict(code=source), targetCurrency=dict(code=target),
        providerTimestamp=timestamp)))))
    raw = json.dumps(data).replace('"DECIMAL_PLACEHOLDER"', rate)
    return ('<html><body><div>Mid-market exchange rate</div><span>¥1 CNY = ' + shown + ' BRL</span>'
            '<script id="__NEXT_DATA__" type="application/json">' + raw + '</script></body></html>').encode()


class Response:
    def __init__(self, body, url=PUBLIC_URL, status=200, mime='text/html', headers=None):
        self.body = body
        self.url = url
        self.status = status
        self.headers = dict({'Content-Type': mime}, **(headers or {}))
        self.read_calls = []
    def __enter__(self):return self
    def __exit__(self, *args):return False
    def geturl(self):return self.url
    def read(self, size):
        self.read_calls.append(size)
        return self.body[:size]


class Opener:
    def __init__(self, response):self.response = response; self.calls = []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):raise self.response
        return self.response


class MemoryRepo:
    def __init__(self):self.values = {}; self.writes = 0
    def get(self, key, default=None):return deepcopy(self.values.get(key, default))
    def set(self, key, value):self.writes += 1; self.values[key] = deepcopy(value)


class FakeProvider:
    def __init__(self, value=None):self.value = value or quote(); self.calls = 0
    def fetch(self):
        self.calls += 1
        if isinstance(self.value, Exception):raise self.value
        return deepcopy(self.value)


class PublicParser(unittest.TestCase):
    def test_exact_direction_and_decimal_from_next_data(self):
        actual = parse_public_converter(page(), now=NOW)
        self.assertIsInstance(actual, Decimal)
        self.assertEqual(actual, Decimal(INVENTED_RATE))

    def test_declared_direction_cannot_be_inverted(self):
        with self.assertRaisesRegex(WiseRateError, 'WISE_PAIR_MISMATCH'):
            parse_public_converter(page(source='BRL', target='CNY'), now=NOW)

    def test_visible_rate_must_agree_with_direct_rate_rounding(self):
        with self.assertRaisesRegex(WiseRateError, 'WISE_RATE_INVALID'):
            parse_public_converter(page(shown='0.8129'), now=NOW)

    def test_script_only_midmarket_text_does_not_count(self):
        body = page().replace(b'<div>Mid-market exchange rate</div>', b'<script>"Mid-market exchange rate"</script>')
        with self.assertRaisesRegex(WiseRateError, 'WISE_STRUCTURE_CHANGED'):
            parse_public_converter(body, now=NOW)

    def test_changed_schema_and_duplicate_payload_fail_closed(self):
        for body in (page().replace(b'"pageProps"', b'"renamed"'), page() + page(),
                     page().replace(b'application/json', b'text/plain'),
                     page().replace(b'public/[urlLocale]', b'wrong/route'),
                     page().replace(b'CNY = ', b'BRL = ')):
            with self.subTest(body_length=len(body)), self.assertRaises(WiseRateError):
                parse_public_converter(body, now=NOW)

    def test_comparison_quote_cannot_replace_missing_primary_rate(self):
        body = page().replace(b'"rate":', b'"comparisonRate":')
        with self.assertRaisesRegex(WiseRateError, 'WISE_STRUCTURE_CHANGED'):
            parse_public_converter(body, now=NOW)

    def test_invalid_and_unsafe_rates_rejected(self):
        for value in ('null', 'true', 'NaN', 'Infinity', '-0.3', '0', '1e999999'):
            with self.subTest(value=value), self.assertRaises(WiseRateError):
                parse_public_converter(page(rate=value), now=NOW)

    def test_source_timestamp_not_future_or_more_than_24_hours_old(self):
        millis = int(NOW.timestamp()) * 1000
        for delta in (1, -(24 * 60 * 60 * 1000 + 1)):
            with self.subTest(delta=delta), self.assertRaises(WiseRateError):
                parse_public_converter(page(timestamp=millis + delta), now=NOW)
        self.assertEqual(parse_public_converter(page(timestamp=millis - 24 * 60 * 60 * 1000), now=NOW), Decimal(INVENTED_RATE))


class ProviderRequests(unittest.TestCase):
    def provider(self, response, **kwargs):
        opener = Opener(response)
        return WiseRateProvider(opener=opener, clock=lambda: NOW.isoformat(), **kwargs), opener

    def test_one_public_https_request_no_cookie_or_auth(self):
        provider, opener = self.provider(Response(page()))
        value = provider.fetch()
        self.assertEqual(value['fx_rate'], INVENTED_RATE)
        self.assertEqual(value['fx_source_reference'], PUBLIC_URL)
        self.assertEqual(value['fx_source_type'], 'WISE_PUBLIC_CONVERTER')
        self.assertEqual(value['fx_fetched_at'], NOW.isoformat())
        self.assertEqual(value['fx_status'], 'LIVE')
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, PUBLIC_URL)
        self.assertEqual(request.get_method(), 'GET')
        self.assertNotIn('Authorization', dict(request.header_items()))
        self.assertNotIn('Cookie', dict(request.header_items()))
        self.assertEqual(timeout, 8)

    def test_explicit_token_api_is_direct_cny_brl_and_not_stored(self):
        token = 'private-fake-key-must-not-leak'
        body = ('[{"source":"CNY","target":"BRL","rate":' + INVENTED_RATE + ',"time":"' + NOW.isoformat() + '"}]').encode()
        provider, opener = self.provider(Response(body, url=API_URL, mime='application/json'), token=token)
        value = provider.fetch()
        self.assertEqual(value['fx_source_type'], 'WISE_API')
        self.assertEqual(value['fx_rate'], INVENTED_RATE)
        self.assertEqual(opener.calls[0][0].full_url, API_URL)
        self.assertEqual(opener.calls[0][0].get_header('Authorization'), 'Bearer ' + token)
        self.assertNotIn(token, json.dumps(value))
        self.assertNotIn(token, repr(provider))

    def test_api_wrong_pair_or_response_shape_rejected(self):
        for data in ({'source': 'CNY'}, [], [{'source': 'BRL', 'target': 'CNY', 'rate': '0.8'}],
                     [{'source': 'CNY', 'target': 'BRL', 'rate': '0.8', 'time': 'future'}]):
            with self.subTest(data=data), self.assertRaises(WiseRateError):
                parse_rate_api(json.dumps(data).encode(), now=NOW)

    def test_failure_codes_do_not_leak_token_headers_or_response(self):
        token = 'SECRET-WISE-TOKEN-for-testing'
        stream = io.StringIO();handler = logging.StreamHandler(stream);logging.getLogger().addHandler(handler)
        try:
            for status, expected in ((401, 'WISE_AUTH_FAILED'), (403, 'WISE_PERMISSION_DENIED'),
                                     (429, 'WISE_RATE_LIMITED'), (500, 'WISE_UNAVAILABLE')):
                error = urllib.error.HTTPError(API_URL, status, token, {'Authorization': token}, None)
                provider, opener = self.provider(error, token=token)
                with self.subTest(status=status), self.assertRaisesRegex(WiseRateError, '^' + expected + '$') as caught:
                    provider.fetch()
                self.assertNotIn(token, str(caught.exception))
                self.assertEqual(len(opener.calls), 1)
            self.assertNotIn(token, stream.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)

    def test_timeouts_no_retry_or_second_public_request(self):
        for error in (TimeoutError('secret'), urllib.error.URLError(TimeoutError('secret'))):
            provider, opener = self.provider(error, token='private-token')
            with self.assertRaisesRegex(WiseRateError, '^WISE_TIMEOUT$'):
                provider.fetch()
            self.assertEqual(len(opener.calls), 1)

    def test_redirects_rejected_and_not_followed(self):
        with self.assertRaisesRegex(WiseRateError, 'WISE_REDIRECT_REJECTED'):
            _NoRedirect().redirect_request(None, None, 302, 'go', {}, 'https://other.invalid')
        for response in (Response(page(), status=302), Response(page(), url='https://other.invalid'),
                         urllib.error.HTTPError(PUBLIC_URL, 302, 'go', {'Location': '/else'}, None)):
            provider, opener = self.provider(response)
            with self.assertRaisesRegex(WiseRateError, '^WISE_REDIRECT_REJECTED$'):
                provider.fetch()
            self.assertEqual(len(opener.calls), 1)

    def test_body_limit_and_content_type(self):
        for response, code in ((Response(b'x' * (MAX_BODY_BYTES + 1)), 'WISE_RESPONSE_TOO_LARGE'),
                               (Response(page(), headers={'Content-Length': str(MAX_BODY_BYTES + 1)}), 'WISE_RESPONSE_TOO_LARGE'),
                               (Response(page(), mime='application/octet-stream'), 'WISE_RESPONSE_INVALID')):
            provider, opener = self.provider(response)
            with self.assertRaisesRegex(WiseRateError, code):provider.fetch()
            self.assertEqual(len(opener.calls), 1)


class CachedRateService(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepo();self.now = NOW
        self.provider = FakeProvider()
        self.service = RateService(self.repo, self.provider, lambda: self.now.isoformat())

    def test_success_only_metadata_persisted_and_state_reads_never_fetch(self):
        self.provider.value.update(Authorization='do-not-save', html='<personal>')
        value = self.service.refresh(True)
        self.assertEqual(value['fx_status'], 'LIVE')
        for _ in range(10):self.assertEqual(self.service.state(), value)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.repo.writes, 2)
        serialized = json.dumps(self.repo.values)
        self.assertNotIn('do-not-save', serialized)
        self.assertNotIn('personal', serialized)

    def test_cache_used_on_network_failure_original_fetch_time_preserved(self):
        old = self.service.refresh(True)
        self.now += timedelta(hours=3)
        self.provider.value = WiseRateError('WISE_NETWORK_ERROR')
        actual = self.service.refresh(True)
        self.assertEqual(actual['fx_status'], 'CACHED')
        self.assertEqual(actual['fx_rate'], old['fx_rate'])
        self.assertEqual(actual['fx_fetched_at'], old['fx_fetched_at'])
        self.assertEqual(actual['fx_error_code'], 'WISE_NETWORK_ERROR')

    def test_exactly_24_hours_allowed_then_expired(self):
        frozen = self.service.refresh(True)
        self.provider.value = RuntimeError('sensitive response')
        self.now += timedelta(hours=24)
        self.assertEqual(self.service.refresh(True)['fx_status'], 'CACHED')
        self.now += timedelta(microseconds=1)
        failed = self.service.refresh(True)
        self.assertEqual(failed['fx_status'], 'UNAVAILABLE')
        self.assertIsNone(failed['fx_rate'])
        self.assertEqual(frozen['fx_status'], 'LIVE')
        self.assertEqual(frozen['fx_rate'], INVENTED_RATE)
        self.assertNotIn('sensitive', json.dumps(failed))

    def test_never_success_no_guess_or_zero(self):
        self.provider.value = RuntimeError('token: do-not-save')
        value = self.service.refresh(True)
        self.assertEqual(value['fx_status'], 'UNAVAILABLE')
        self.assertIsNone(value['fx_rate'])
        self.assertIsNone(value['fx_fetched_at'])
        self.assertNotIn('do-not-save', json.dumps(self.repo.values))

    def test_future_missing_or_wrong_pair_cache_rejected(self):
        self.provider.value = RuntimeError('failed')
        for bad in (dict(quote(), fx_pair='BRL/CNY'), dict(quote(), fx_fetched_at=None),
                    quote(NOW + timedelta(seconds=1)), dict(quote(), fx_provider='other'),
                    dict(quote(), fx_rate='NaN')):
            self.repo.set('wise_last_success', bad)
            with self.subTest(bad=bad):
                self.assertIsNone(self.service.refresh(True)['fx_rate'])

    def test_manual_refresh_minute_debounce_but_query_forces_one_attempt(self):
        self.service.refresh()
        for _ in range(8):self.service.refresh()
        self.assertEqual(self.provider.calls, 1)
        self.service.refresh(True)
        self.assertEqual(self.provider.calls, 2)
        self.now += timedelta(minutes=1)
        self.provider.value = quote(self.now)
        self.service.refresh()
        self.assertEqual(self.provider.calls, 3)

    def test_parallel_manual_refreshes_share_one_request(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.service.refresh(), range(12)))
        self.assertEqual(self.provider.calls, 1)
        self.assertTrue(all(x['fx_status'] == 'LIVE' for x in results))

    def test_state_detects_age_without_writing_or_requesting(self):
        frozen = self.service.refresh(True)
        writes = self.repo.writes
        self.now += timedelta(hours=25)
        state = self.service.state()
        self.assertEqual(state['fx_status'], 'UNAVAILABLE')
        self.assertEqual(state['fx_error_code'], 'WISE_CACHE_EXPIRED')
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.repo.writes, writes)
        self.assertEqual(frozen['fx_status'], 'LIVE')

    def test_repository_failures_do_not_escape(self):
        self.repo.get = lambda *args: (_ for _ in ()).throw(RuntimeError('secret'))
        self.repo.set = lambda *args: (_ for _ in ()).throw(RuntimeError('secret'))
        self.assertIsNone(self.service.state()['fx_rate'])
        live = self.service.refresh(True)
        self.assertEqual(live['fx_status'], 'LIVE')
        self.assertEqual(live['fx_error_code'], 'WISE_STORAGE_ERROR')

    def test_sqlite_restart_cache_and_previous_snapshot_frozen(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Repository(root)
            first = RateService(repo, self.provider, lambda: NOW).refresh(True)
            repo.set('saved_snapshot_example', {'fx': first})
            restarted = RateService(Repository(root), FakeProvider(RuntimeError('offline')), lambda: NOW + timedelta(hours=1))
            self.assertEqual(restarted.refresh(True)['fx_status'], 'CACHED')
            self.assertEqual(repo.get('saved_snapshot_example')['fx'], first)

    def test_corrupt_storage_state_does_not_break_application(self):
        for value in ('invalid', {'fx_status': 'LIVE'}, {'fx_status': 'CACHED', 'fx_fetched_at': 'bad'}):
            self.repo.set('wise_rate', value)
            with self.subTest(value=value):self.assertEqual(self.service.state()['fx_status'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
