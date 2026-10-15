"""One-request Wise rates, with a local, strictly bounded 24-hour fallback.

No page, request headers, API token, or raw exception is persisted. Tokens are
accepted only through an explicit constructor argument; no credentials are
discovered or copied from another application.
"""
import json
import re
import socket
import threading
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser


PUBLIC_URL = 'https://wise.com/gb/currency-converter/cny-to-brl-rate'
API_URL = 'https://api.wise.com/2026Q3/rates?source=CNY&target=BRL'
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_CACHE_AGE = timedelta(hours=24)
MANUAL_REFRESH_INTERVAL = timedelta(minutes=1)
SAFE_CODES = frozenset((
    'WISE_NETWORK_ERROR', 'WISE_TIMEOUT', 'WISE_HTTP_ERROR',
    'WISE_AUTH_FAILED', 'WISE_PERMISSION_DENIED', 'WISE_RATE_LIMITED',
    'WISE_UNAVAILABLE', 'WISE_REDIRECT_REJECTED', 'WISE_RESPONSE_TOO_LARGE',
    'WISE_RESPONSE_INVALID', 'WISE_STRUCTURE_CHANGED', 'WISE_PAIR_MISMATCH',
    'WISE_RATE_INVALID', 'WISE_CACHE_EXPIRED', 'WISE_CACHE_INVALID',
    'WISE_NOT_FETCHED', 'WISE_STORAGE_ERROR',
))


class WiseRateError(ValueError):
    def __init__(self, code):
        self.code = code if code in SAFE_CODES else 'WISE_RESPONSE_INVALID'
        super().__init__(self.code)


def _now(value=None):
    value = value() if callable(value) else value
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WiseRateError('WISE_RESPONSE_INVALID')
    return value.astimezone(timezone.utc)


def _rate(value):
    # JSON floats must have been decoded directly to Decimal, never via float.
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise WiseRateError('WISE_RATE_INVALID')
    try:
        rate = Decimal(value)
        if not rate.is_finite() or rate <= 0 or len(rate.as_tuple().digits) > 30:
            raise WiseRateError('WISE_RATE_INVALID')
        if not -18 <= rate.adjusted() <= 12:
            raise WiseRateError('WISE_RATE_INVALID')
        return rate
    except (InvalidOperation, ValueError):
        raise WiseRateError('WISE_RATE_INVALID') from None


def _json(text):
    def invalid_constant(_):
        raise WiseRateError('WISE_RESPONSE_INVALID')
    return json.loads(text, parse_float=Decimal, parse_constant=invalid_constant)


class _PublicPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.visible = []
        self._script = None
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self._hidden += 1
            if attrs.get('id') == '__NEXT_DATA__':
                if attrs.get('type') != 'application/json':
                    raise WiseRateError('WISE_STRUCTURE_CHANGED')
                self._script = []
                self.scripts.append(self._script)
        elif tag == 'style':
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._hidden = max(0, self._hidden - 1)
            if tag == 'script':
                self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        elif not self._hidden:
            self.visible.append(data)


def _observed_time(value, now, *, milliseconds=False):
    if milliseconds:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WiseRateError('WISE_RESPONSE_INVALID')
        # Integer milliseconds; do not pass rates or identifiers through float.
        observed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
    else:
        if not isinstance(value, str) or not value:
            raise WiseRateError('WISE_RESPONSE_INVALID')
        observed = _now(value)
    age = _now(now) - observed
    if age < timedelta(0) or age > MAX_CACHE_AGE:
        raise WiseRateError('WISE_RESPONSE_INVALID')


def parse_public_converter(body, *, now=None):
    """Reject ambiguous direction, changed schema, or a mismatching shown rate."""
    try:
        page = _PublicPage()
        page.feed(body.decode('utf-8', errors='strict'))
        page.close()
        if len(page.scripts) != 1:
            raise WiseRateError('WISE_STRUCTURE_CHANGED')
        data = _json(''.join(page.scripts[0]))
        if data['page'] != '/public/[urlLocale]/currency-converter/route':
            raise WiseRateError('WISE_STRUCTURE_CHANGED')
        rate_data = data['props']['pageProps']['model']['rate']
        if rate_data['sourceCurrency']['code'] != 'CNY' or rate_data['targetCurrency']['code'] != 'BRL':
            raise WiseRateError('WISE_PAIR_MISMATCH')
        rate = _rate(rate_data['value'])
        _observed_time(rate_data['providerTimestamp'], now, milliseconds=True)
        visible = re.sub(r'\s+', ' ', ' '.join(page.visible))
        label = visible.find('Mid-market exchange rate')
        equations = re.findall(r'¥\s*1\s+CNY\s*=\s*([0-9]+\.[0-9]{2,12})\s+BRL\b', visible)
        if label < 0 or len(equations) != 1:
            raise WiseRateError('WISE_STRUCTURE_CHANGED')
        # The direction equation must be the displayed rate close to its label,
        # not a comparison quote or an unrelated explanatory example.
        following = visible[label:label + 500]
        if not re.search(r'¥\s*1\s+CNY\s*=\s*' + re.escape(equations[0]) + r'\s+BRL\b', following):
            raise WiseRateError('WISE_STRUCTURE_CHANGED')
        displayed = _rate(equations[0])
        quantum = Decimal(1).scaleb(displayed.as_tuple().exponent)
        if rate.quantize(quantum, rounding=ROUND_HALF_UP) != displayed:
            raise WiseRateError('WISE_RATE_INVALID')
        return rate
    except WiseRateError:
        raise
    except Exception:
        raise WiseRateError('WISE_STRUCTURE_CHANGED') from None


def parse_rate_api(body, *, now=None):
    try:
        data = _json(body.decode('utf-8', errors='strict'))
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise WiseRateError('WISE_STRUCTURE_CHANGED')
        row = data[0]
        if row.get('source') != 'CNY' or row.get('target') != 'BRL':
            raise WiseRateError('WISE_PAIR_MISMATCH')
        _observed_time(row.get('time'), now)
        return _rate(row['rate'])
    except WiseRateError:
        raise
    except Exception:
        raise WiseRateError('WISE_STRUCTURE_CHANGED') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WiseRateError('WISE_REDIRECT_REJECTED')


class WiseRateProvider:
    """Fetch direct CNY→BRL only. No retries or public/API fallback requests."""
    def __init__(self, *, token=None, opener=None, clock=None, timeout=8):
        if token is not None and (not isinstance(token, str) or not token.strip() or '\n' in token or '\r' in token):
            raise WiseRateError('WISE_AUTH_FAILED')
        self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._clock = clock
        self.timeout = timeout

    def fetch(self):
        url = API_URL if self._token else PUBLIC_URL
        headers = {'User-Agent': 'OrderPulse/1.0 (+currency-reference)',
                   'Accept': 'application/json' if self._token else 'text/html'}
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        try:
            request = urllib.request.Request(url, headers=headers, method='GET')
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != url:
                    raise WiseRateError('WISE_REDIRECT_REJECTED')
                status = response.status
                if 300 <= status < 400:
                    raise WiseRateError('WISE_REDIRECT_REJECTED')
                if status != 200:
                    raise WiseRateError(_http_code(status))
                mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                expected = 'application/json' if self._token else 'text/html'
                if mime != expected:
                    raise WiseRateError('WISE_RESPONSE_INVALID')
                length = response.headers.get('Content-Length')
                if length is not None and int(length) > MAX_BODY_BYTES:
                    raise WiseRateError('WISE_RESPONSE_TOO_LARGE')
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise WiseRateError('WISE_RESPONSE_TOO_LARGE')
            now = _now(self._clock)
            rate = parse_rate_api(body, now=now) if self._token else parse_public_converter(body, now=now)
            return dict(fx_provider='wise', fx_pair='CNY/BRL', fx_rate=format(rate, 'f'),
                        fx_fetched_at=_now(self._clock).isoformat(),
                        fx_source_type='WISE_API' if self._token else 'WISE_PUBLIC_CONVERTER',
                        fx_source_reference=url, fx_status='LIVE')
        except WiseRateError:
            raise
        except urllib.error.HTTPError as exc:
            raise WiseRateError(_http_code(exc.code)) from None
        except (TimeoutError, socket.timeout):
            raise WiseRateError('WISE_TIMEOUT') from None
        except urllib.error.URLError as exc:
            code = 'WISE_TIMEOUT' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'WISE_NETWORK_ERROR'
            raise WiseRateError(code) from None
        except Exception:
            raise WiseRateError('WISE_RESPONSE_INVALID') from None


def _http_code(status):
    if 300 <= status < 400:
        return 'WISE_REDIRECT_REJECTED'
    return {401: 'WISE_AUTH_FAILED', 403: 'WISE_PERMISSION_DENIED',
            429: 'WISE_RATE_LIMITED'}.get(status, 'WISE_UNAVAILABLE' if status >= 500 else 'WISE_HTTP_ERROR')


def _metadata(value):
    if not isinstance(value, dict) or value.get('fx_provider') != 'wise' or value.get('fx_pair') != 'CNY/BRL':
        raise WiseRateError('WISE_CACHE_INVALID')
    source = value.get('fx_source_type')
    reference = {'WISE_API': API_URL, 'WISE_PUBLIC_CONVERTER': PUBLIC_URL}.get(source)
    if not reference or value.get('fx_source_reference') != reference:
        raise WiseRateError('WISE_CACHE_INVALID')
    if not isinstance(value.get('fx_fetched_at'), str) or not value['fx_fetched_at']:
        raise WiseRateError('WISE_CACHE_INVALID')
    return dict(fx_provider='wise', fx_pair='CNY/BRL', fx_rate=format(_rate(value.get('fx_rate')), 'f'),
                fx_fetched_at=_now(value.get('fx_fetched_at')).isoformat(),
                fx_source_type=source, fx_source_reference=reference)


def _unavailable(code, checked_at=None):
    return dict(fx_provider='wise', fx_pair='CNY/BRL', fx_rate=None,
                fx_fetched_at=None, fx_source_type=None, fx_source_reference=PUBLIC_URL,
                fx_status='UNAVAILABLE', fx_error_code=code if code in SAFE_CODES else 'WISE_UNAVAILABLE',
                fx_checked_at=checked_at)


class RateService:
    """Persist only safe metadata. Snapshot readers never call the network."""
    def __init__(self, repo, provider=None, clock=None):
        self.repo = repo
        self.clock = clock
        self.provider = provider or WiseRateProvider(clock=clock)
        self._lock = threading.Lock()

    def _cached(self, now, code, checked_at=None):
        try:
            cached = _metadata(self.repo.get('wise_last_success'))
            age = now - _now(cached['fx_fetched_at'])
            if age < timedelta(0):
                return _unavailable('WISE_CACHE_INVALID', checked_at)
            if age > MAX_CACHE_AGE:
                return _unavailable(code or 'WISE_CACHE_EXPIRED', checked_at)
            cached.update(fx_status='CACHED', fx_error_code=code, fx_checked_at=checked_at)
            return cached
        except Exception:
            return _unavailable(code or 'WISE_NOT_FETCHED', checked_at)

    def state(self):
        """Pure read, including an age check; never changes historic snapshots."""
        now = _now(self.clock)
        try:
            stored = self.repo.get('wise_rate')
            if not stored:
                return self._cached(now, None)
            code = stored.get('fx_error_code')
            code = code if code in SAFE_CODES else None
            checked_at = stored.get('fx_checked_at')
            if stored.get('fx_status') in ('LIVE', 'CACHED'):
                result = _metadata(stored)
                age = now - _now(result['fx_fetched_at'])
                if age < timedelta(0):
                    return _unavailable('WISE_CACHE_INVALID', checked_at)
                if age > MAX_CACHE_AGE:
                    return _unavailable('WISE_CACHE_EXPIRED', checked_at)
                result.update(fx_status=stored['fx_status'], fx_error_code=code, fx_checked_at=checked_at)
                return result
            return self._cached(now, code, checked_at)
        except Exception:
            return _unavailable('WISE_STORAGE_ERROR')

    def refresh(self, force=False):
        """Manual refresh is throttled; successful query runs use force=True.

        The query service already prevents overlapping runs. This lock also
        serializes a settings refresh racing with a query; each invocation makes
        at most one provider request and never retries a failed fetch.
        """
        with self._lock:
            now = _now(self.clock)
            if not force:
                prior = self.state()
                try:
                    age = now - _now(prior.get('fx_checked_at'))
                    if prior.get('fx_checked_at') and timedelta(0) <= age < MANUAL_REFRESH_INTERVAL:
                        return deepcopy(prior)
                except Exception:
                    pass
            try:
                fetched = self.provider.fetch()
                result = _metadata(fetched)
                now = _now(self.clock)
                age = now - _now(result['fx_fetched_at'])
                if age < timedelta(0) or age > MAX_CACHE_AGE or fetched.get('fx_status') != 'LIVE':
                    raise WiseRateError('WISE_RESPONSE_INVALID')
                result.update(fx_status='LIVE', fx_error_code=None, fx_checked_at=now.isoformat())
            except Exception as exc:
                code = exc.code if isinstance(exc, WiseRateError) else 'WISE_UNAVAILABLE'
                now = _now(self.clock)
                result = self._cached(now, code, now.isoformat())
            else:
                try:
                    self.repo.set('wise_last_success', result)
                except Exception:
                    # The live quote can still be frozen in the order snapshot.
                    result = dict(result, fx_error_code='WISE_STORAGE_ERROR')
            try:
                self.repo.set('wise_rate', result)
            except Exception:
                # A settings persistence error must not discard valid RMB orders.
                result = dict(result, fx_error_code='WISE_STORAGE_ERROR')
            return deepcopy(result)
