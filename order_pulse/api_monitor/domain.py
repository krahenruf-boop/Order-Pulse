from decimal import Decimal, InvalidOperation
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
TZ=ZoneInfo('Asia/Shanghai')
PROVIDERS={'deepseek':'DeepSeek','elevenlabs':'ElevenLabs'}
def amount(value):
    if not isinstance(value,str) or len(value)>40:raise ValueError('额度字段缺失或格式不正确')
    try:d=Decimal(value)
    except InvalidOperation:raise ValueError('额度字段格式不正确') from None
    if not d.is_finite() or d<0:raise ValueError('额度字段格式不正确')
    return d

def reading(provider,raw,previous=None):
    if provider not in PROVIDERS or not isinstance(raw,dict):raise ValueError('供应商无效')
    if not isinstance(raw.get('identity'),str) or not raw['identity'].strip() or len(raw['identity'])>300:raise ValueError('账户或工作区身份无法核验')
    if previous and raw['identity']!=previous['identity']:raise ValueError('账户或工作区已变化，请核对后重新绑定')
    unit=raw.get('unit')
    if unit not in (('CNY','USD') if provider=='deepseek' else ('Credits',)):raise ValueError('额度单位无法核验')
    if previous and unit!=previous['unit']:raise ValueError('额度单位已变化，已有值保留')
    current=amount(raw.get('remaining'))
    value=dict(identity=raw['identity'],unit=unit,remaining=str(current),reset_label=raw.get('reset_label'))
    if provider=='elevenlabs':
        total=amount(raw.get('total'));used=amount(raw.get('used'))
        if total<=0 or max(total-used,Decimal(0))!=current:raise ValueError('当期总额度、已用额度和剩余额度不一致')
        if not isinstance(raw.get('reset_label'),str) or not raw['reset_label']:raise ValueError('额度周期未核实')
        value.update(total=str(total),used=str(used))
    return value

def ratio(provider,value,baseline):
    denominator=amount(value['total']) if provider=='elevenlabs' else amount(baseline) if baseline is not None else Decimal(0)
    return str(amount(value['remaining'])/denominator*100) if denominator>0 else None

def next_at(now,time,last_day=None):
    local=now.astimezone(TZ);h,m=map(int,time.split(':'));target=local.replace(hour=h,minute=m,second=0,microsecond=0)
    if target<=local or last_day==local.date().isoformat():target+=timedelta(days=1)
    return target.isoformat()
