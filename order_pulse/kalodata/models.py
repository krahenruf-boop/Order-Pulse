"""V1 business contract. No browser requests or secret-bearing payloads live here."""
from __future__ import annotations
import hashlib
import json
import re
import math
from datetime import datetime, timezone, date
from urllib.parse import urlsplit, parse_qs, urlencode

PERIODS = {'1D': 1, '7D': 7, '30D': 30, '90D': 90, '180D': 180, '365D': 365}
ACTIVE = {'created', 'preflighting', 'reading', 'resolving'}
TERMINAL = {'completed', 'completed_with_errors', 'partial', 'cancelled', 'failed', 'blocked', 'interrupted'}
COUNT_KEYS = ('visited_rows','unique_videos','duplicate_occurrences','invalid_rows','resolved_unique','unresolved_unique','new_unique','already_acquired_unique')
SAFE_DETAILS = {'cooldown_until','cooldown_seconds','visible_pages','source_page','source_index','missing_pages','state',
                'expected_range','observed_range','observed_start','observed_end'}
MESSAGES = {
'BROWSER_BINDING_REQUIRED':'素材空间名称或浏览器身份已变化，请到“设置 → 浏览器空间”重新核验绑定。',
'BROWSER_BUSY':'素材与客单价空间正在执行另一项采集，请稍后重试。',
'BROWSER_CONTROL_UNAVAILABLE':'无法取得素材与客单价空间控制权，请检查已绑定的空间是否可用。',
'PLUGIN_RESPONSE_TIMEOUT':'采集插件未及时返回页面结果，请检查 Kalodata 页面后重试。',
'MODULE_DISABLED':'素材模块尚未启用。','PLUGIN_OFFLINE':'素材采集插件未连接，请确认现有插件正在运行。',
'PLUGIN_RELOAD_REQUIRED':'采集插件代码已更新，请重新加载浏览器扩展并刷新 Kalodata 页面。',
'PLUGIN_VERSION_MISMATCH':'素材采集插件版本或日期适配器不兼容。','KALODATA_BUSY':'已有素材任务正在运行。',
'KALODATA_LOGIN_REQUIRED':'请在已安装插件的浏览器中登录 Kalodata。',
'INVALID_PRODUCT_URL':'请输入有效的 Kalodata 商品详情网址。','INVALID_PAGE_RANGE':'页码须为正整数，且起始页不大于结束页。',
'SESSION_MISMATCH':'插件会话已改变，请检查采集服务后重试。','LOGIN_REQUIRED':'请在已安装插件的浏览器中完成 Kalodata 登录或验证，再重试。',
'PERMISSION_DENIED':'当前页面访问权益不足，已停止本次读取。','RATE_LIMITED':'网站已限流；冷却结束后请主动重试。',
'DETAIL_QUOTA_EXHAUSTED':'Kalodata 今日详情查看额度已用完。请恢复额度或登录有可用额度的账号后重试。',
'SOURCE_LOAD_FAILED':'Kalodata 详情页显示加载失败，未取得有效原链接。',
'RANGE_EXCEEDS_VISIBLE':'请求页码超出当前页面已确认的可见范围。','PERIOD_UNAVAILABLE':'当前页面不支持所选日期范围。',
'DATE_CONTROL_NOT_FOUND':'未找到 Kalodata 页面上的日期控件。','DATE_CONTEXT_UNVERIFIED':'无法核实网站实际日期范围。',
'SOURCE_REFRESH_TIMEOUT':'切换统计日期后页面未能及时刷新。','SOURCE_LIST_NOT_READY':'Kalodata 素材列表尚未就绪。','PAGINATION_SCHEMA_CHANGED':'Kalodata 页码信息无法读取。','VIDEO_ITEM_SCHEMA_CHANGED':'视频条目结构无法识别。',
'PRODUCT_NOT_FOUND':'Kalodata 页面未找到此商品。','EXTRACTION_FAILED':'素材提取未能完成。',
'CONTEXT_CHANGED':'商品、市场、日期、排序或筛选已变化；请新建任务。',
'PAGE_MISMATCH':'返回页面与请求页码或已验证页内容不一致。','SCHEMA_CHANGED':'页面结构无法确认，已停止读取。',
'UPSTREAM_BUSINESS_ERROR':'网站返回业务失败，不能作为空列表。','LINK_NOT_FOUND':'页面未提供可验证的 TikTok 原链接。',
'LINK_ID_MISMATCH':'页面外链与当前视频 ID 不一致。','NETWORK_ERROR':'页面读取遇到网络故障。','TASK_BUDGET_EXCEEDED':'本次任务已达到时间预算。',
'STORAGE_ERROR':'素材本地存储暂不可用，余额功能不受影响。','CANCELLED':'任务已取消。','NOT_FOUND':'未找到此任务。',
'CONFLICT':'已有素材任务正在运行。','PREFLIGHT_EXPIRED':'预检已过期，请重新开始。','INVALID_CURSOR':'结果已变化或游标无效，请重新载入。',
'RUN_NOT_FINISHED':'任务结束后才可复制或导出。','EMPTY_RESULT':'没有可导出的有效原链接。','INVALID_REQUEST':'请求字段或格式不正确。',
'INTERRUPTED':'程序中断；请主动新建任务，不会自动续跑。'}

class KalodataError(Exception):
    def __init__(self, code, status=400, details=None, retryable=False):
        self.code, self.status, self.retryable = code, status, retryable
        self.message = MESSAGES.get(code, '素材任务无法继续。')
        self.details = {k:v for k,v in (details or {}).items() if k in SAFE_DETAILS}
        super().__init__(self.message)
    def payload(self, request_id):
        return {'error':{'code':self.code,'message':self.message,'retryable':self.retryable,'details':self.details},'request_id':request_id}

def now_iso(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def is_id(value): return isinstance(value,str) and bool(re.fullmatch(r'[0-9]{1,64}',value))
def iso_time(value):
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if not result.tzinfo: raise ValueError()
        return result
    except (ValueError,TypeError,AttributeError): raise KalodataError('SCHEMA_CHANGED')

def strict_url(value, host, path):
    if not isinstance(value,str) or len(value)>4096 or any(ord(c)<33 for c in value): raise KalodataError('INVALID_PRODUCT_URL')
    try:
        u=urlsplit(value)
        if u.scheme!='https' or u.hostname!=host or u.username is not None or u.password is not None or u.port is not None or u.path!=path or u.fragment: raise ValueError()
    except ValueError: raise KalodataError('INVALID_PRODUCT_URL')
    return u

def product_url(value):
    u=strict_url(value,'www.kalodata.com','/product/detail');q=parse_qs(u.query,keep_blank_values=True)
    if len(q.get('id',[]))!=1 or not is_id(q['id'][0]): raise KalodataError('INVALID_PRODUCT_URL')
    safe={'id':q['id'][0]}
    for key in ('region','language','currency'):
        if key in q:
            normalized=q[key][0].strip()
            if len(q[key])!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,24}',normalized): raise KalodataError('INVALID_PRODUCT_URL')
            safe[key]=normalized
    return 'https://www.kalodata.com/product/detail?'+urlencode(safe),safe

def extract_request(payload):
    fields={'product_url','period','start_page','end_page','session_ref'}
    if not isinstance(payload,dict) or set(payload)!=fields: raise KalodataError('INVALID_REQUEST',422)
    clean,params=product_url(payload['product_url'])
    if not isinstance(payload['period'],str) or payload['period'] not in PERIODS: raise KalodataError('PERIOD_UNAVAILABLE',422)
    start,end=payload['start_page'],payload['end_page']
    if type(start)!=int or type(end)!=int or start<1 or end<start or end>100000: raise KalodataError('INVALID_PAGE_RANGE',422)
    if not isinstance(payload['session_ref'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',payload['session_ref']): raise KalodataError('SESSION_MISMATCH',422)
    return dict(payload,product_url=clean)

def validate_context(raw,request):
    keys={'product_id','region','period','actual_start_date','actual_end_date','site_date_label','site_timezone','date_basis','page_size','visible_total','visible_pages','observed_sort','filters','source_mode','captured_at'}
    if not isinstance(raw,dict) or not keys.issubset(raw): raise KalodataError('SCHEMA_CHANGED')
    c={k:raw[k] for k in keys};_,p=product_url(request['product_url'])
    if not is_id(c['product_id']) or c['product_id']!=p['id'] or not isinstance(c['region'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,24}',c['region']): raise KalodataError('CONTEXT_CHANGED',409)
    if p.get('region') and p['region']!=c['region']: raise KalodataError('CONTEXT_CHANGED',409)
    # Kalodata's date selection is a best-effort filter, never an extraction gate.
    # The visible list and its product identity remain the authoritative source.
    start=end=None;span=None
    try:
        if c['actual_start_date'] and c['actual_end_date']:
            start=date.fromisoformat(c['actual_start_date']);end=date.fromisoformat(c['actual_end_date']);span=(end-start).days+1
            if span<1:span=None
    except (ValueError,TypeError):start=end=span=None
    if span is None:c['actual_start_date']=c['actual_end_date']=None
    else:c['actual_start_date']=start.isoformat();c['actual_end_date']=end.isoformat()
    exact=(c['period']==request['period'] and c['date_basis']=='page_visible_range' and span==PERIODS[request['period']])
    tolerances={'1D':(1,3),'7D':(5,10),'30D':(25,35),'90D':(80,100),'180D':(165,195),'365D':(330,400)}
    lo,hi=tolerances[request['period']]
    c['requested_range']=request['period'];c['observed_range']=c['period'] if c['period'] in PERIODS else None
    c['date_status']='MATCHED' if exact else 'APPROXIMATE' if span is not None and lo<=span<=hi else 'UNKNOWN'
    c['date_warning']=None if c['date_status']=='MATCHED' else '页面日期范围可能与所选范围略有差异' if c['date_status']=='APPROXIMATE' else '未确认页面日期范围，结果按当前 Kalodata 页面实际显示内容提取'
    if not isinstance(c['site_date_label'],str):c['site_date_label']=''
    if c['site_timezone'] is not None and not isinstance(c['site_timezone'],str):c['site_timezone']=None
    if type(c['page_size'])!=int or not 1<=c['page_size']<=10000: raise KalodataError('SCHEMA_CHANGED')
    for key in ('visible_pages','visible_total'):
        if c[key] is not None and (type(c[key])!=int or c[key]<0): raise KalodataError('SCHEMA_CHANGED')
    if not isinstance(c['observed_sort'],list): raise KalodataError('SCHEMA_CHANGED')
    for s in c['observed_sort']:
        if not isinstance(s,dict) or set(s)!={'field','direction'} or not isinstance(s['field'],str) or len(s['field'])>100 or s['direction'] not in ('ASC','DESC'): raise KalodataError('SCHEMA_CHANGED')
    if not isinstance(c['filters'],dict) or len(c['filters'])>30: raise KalodataError('SCHEMA_CHANGED')
    for key,value in c['filters'].items():
        if not isinstance(key,str) or len(key)>100 or not (value is None or type(value) in (str,int,float,bool)) or len(str(value))>300: raise KalodataError('SCHEMA_CHANGED')
        if type(value)==float and not math.isfinite(value):raise KalodataError('SCHEMA_CHANGED')
        if any(word in key.lower() for word in ('token','cookie','secret','password','header','session')): raise KalodataError('SCHEMA_CHANGED')
    if c['source_mode'] not in ('ui_json_observed','ui_dom'): raise KalodataError('SCHEMA_CHANGED')
    iso_time(c['captured_at'])
    c['context_fingerprint']=digest({k:v for k,v in c.items() if k not in ('captured_at','visible_total','visible_pages','site_date_label','date_warning','period','requested_range','observed_range','date_status','actual_start_date','actual_end_date','date_basis')})
    if raw.get('context_fingerprint') and raw['context_fingerprint']!=c['context_fingerprint']: raise KalodataError('CONTEXT_CHANGED',409)
    cap=c['visible_pages']
    if (cap==0 and (request['start_page'],request['end_page'])!=(1,1)) or (cap is not None and cap>0 and request['end_page']>cap): raise KalodataError('RANGE_EXCEEDS_VISIBLE',400,{'visible_pages':cap})
    return c
