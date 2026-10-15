"""Direct Wise reference pairs with an independent cache."""
from datetime import datetime, timezone, timedelta
import urllib.request
from ..wise import (WiseRateProvider, _PublicPage, _json, _rate, _observed_time,
                    _NoRedirect, WiseRateError, MAX_BODY_BYTES)
from .domain import instant

CURRENCIES=('BRL','MXN','THB','VND','USD','GBP','EUR','MYR','SGD','PHP','IDR','JPY','CNY')


def parse_pair(body, currency, at):
    page=_PublicPage();page.feed(body.decode('utf-8',errors='strict'));page.close()
    if len(page.scripts)!=1:raise WiseRateError('WISE_STRUCTURE_CHANGED')
    data=_json(''.join(page.scripts[0]))
    if data.get('page')!='/public/[urlLocale]/currency-converter/route':raise WiseRateError('WISE_STRUCTURE_CHANGED')
    rate=data['props']['pageProps']['model']['rate']
    if rate['sourceCurrency']['code']!='CNY' or rate['targetCurrency']['code']!=currency:raise WiseRateError('WISE_PAIR_MISMATCH')
    _observed_time(rate['providerTimestamp'],at,milliseconds=True)
    return format(_rate(rate['value']),'f')


class Rates:
    def __init__(self,store,fetcher=None):self.store=store;self.fetcher=fetcher or self.fetch
    def fetch(self,currency,at):
        if currency=='BRL':return WiseRateProvider(clock=lambda:at).fetch()
        url=f'https://wise.com/gb/currency-converter/cny-to-{currency.lower()}-rate'
        request=urllib.request.Request(url,headers={'User-Agent':'OrderPulse/1.0 (+currency-reference)','Accept':'text/html'})
        with urllib.request.build_opener(_NoRedirect()).open(request,timeout=8) as r:
            if r.status!=200 or r.geturl()!=url or r.headers.get('Content-Type','').split(';')[0]!='text/html':raise WiseRateError('WISE_RESPONSE_INVALID')
            body=r.read(MAX_BODY_BYTES+1)
        if len(body)>MAX_BODY_BYTES:raise WiseRateError('WISE_RESPONSE_TOO_LARGE')
        return dict(fx_pair='CNY/'+currency,fx_rate=parse_pair(body,currency,at),fx_fetched_at=at.isoformat(),
                    fx_source_reference=url,fx_provider='wise',fx_status='LIVE')
    def get(self,currency,at):
        if currency not in CURRENCIES:raise ValueError('币种无效')
        if currency=='CNY':return dict(fx_pair='CNY/CNY',fx_rate='1',fx_fetched_at=at.isoformat(),fx_status='IDENTITY')
        try:
            value=self.fetcher(currency,at)
            if value.get('fx_pair')!='CNY/'+currency:raise ValueError('pair')
            _rate(value['fx_rate'])
            if not timedelta(0)<=at-instant(value['fx_fetched_at'])<=timedelta(hours=24):raise ValueError('age')
            self.store.fx(currency,value);return value
        except Exception:
            old=self.store.fx(currency)
            try:
                if old and old.get('fx_pair')=='CNY/'+currency and timedelta(0)<=at-instant(old['fx_fetched_at'])<=timedelta(hours=24):
                    _rate(old['fx_rate']);return dict(old,fx_status='CACHED')
            except (ValueError,KeyError):pass
            return dict(fx_pair='CNY/'+currency,fx_rate=None,fx_fetched_at=None,fx_status='UNAVAILABLE')
