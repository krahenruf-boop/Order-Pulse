"""Shop directory identities and actual-order products; no fuzzy joins."""
from collections import Counter
from copy import deepcopy
import re
import unicodedata
from .domain import WINDOWS, rule, ruleset, digest, calculate

# Only these staff/internal suffix tokens have been observed and checked in both
# directories. The identity before 店, including country and 本土, stays intact.
SUFFIX = re.compile(r'^(?:\d+|(?:侨巴|乔巴|BOLA|MCDI|FAFB)\d*|舒|邵|凯)$', re.I)
def normalized_name(name):
    value=unicodedata.normalize('NFKC',name).strip().casefold()
    value=re.sub(r'[\s_—–－]+','-',value).strip('-')
    value=re.sub(r'-+','-',value)
    match=re.search(r'\d+店',value)
    if match:
        head=value[:match.end()];tail=value[match.end():].strip('-').split('-')
        if tail and all(SUFFIX.fullmatch(x) for x in tail):value=head
    return value

def identity_conflict(a,b):
    # Never erase locality or a differing known country to obtain a match.
    x,y=normalized_name(a),normalized_name(b)
    countries=('巴西','墨西哥','泰国','越南','美国','英国','马来西亚','菲律宾','新加坡','印尼','日本')
    return ('本土' in x)!=('本土' in y) or any((c in x)!=(c in y) for c in countries)

def match_shops(tms,miaoshou,manual=None):
    valid=[s for s in miaoshou if s.get('status')=='normal' and s.get('auth_status')=='valid']
    byid={s['id']:s for s in valid};manual=manual or {};result={};chosen={}
    tn=Counter(normalized_name(s['name']) for s in tms)
    te=Counter(s['name'].strip().casefold() for s in tms)
    for shop in tms:
        sid=shop['id']; candidates=[];method=None;target=None
        pid=shop.get('platform_shop_id')
        if pid:
            candidates=[s for s in valid if s.get('platform_shop_id')==pid];method='PLATFORM_ID'
        if not candidates:
            name=shop['name'].strip().casefold()
            candidates=[s for s in valid if s['name'].strip().casefold()==name]
            if candidates and te[name]!=1:candidates=[]
            method='EXACT_NAME'
        if not candidates:
            name=normalized_name(shop['name']);candidates=[s for s in valid if normalized_name(s['name'])==name]
            method='NORMALIZED_NAME'
            if candidates and tn[name]!=1:
                result[sid]=dict(status='CONFLICT',candidates=[s['id'] for s in candidates]);continue
        if sid in manual:
            candidate=byid.get(manual[sid]);method='CONFIRMED'
            if candidate and not identity_conflict(shop['name'],candidate['name']) and (not pid or not candidate.get('platform_shop_id') or pid==candidate['platform_shop_id']):target=candidate
        elif len(candidates)==1:
            candidate=candidates[0]
            if not identity_conflict(shop['name'],candidate['name']) and (not pid or not candidate.get('platform_shop_id') or pid==candidate['platform_shop_id']):target=candidate
        if target:
            result[sid]=dict(status='MATCHED',method=method,miaoshou_shop_id=target['id'],miaoshou_shop_name=target['name'],platform_shop_id=target.get('platform_shop_id'),candidates=[s['id'] for s in candidates]);chosen.setdefault(target['id'],[]).append(sid)
        else:result[sid]=dict(status='CONFLICT' if candidates or sid in manual else 'UNMATCHED',candidates=[s['id'] for s in candidates])
    for ids in chosen.values():
        if len(ids)>1:
            for sid in ids:result[sid]=dict(status='CONFLICT',candidates=[result[sid]['miaoshou_shop_id']])
    return result

def collection_mapping(directory,manual=None):
    """Only a complete directory can establish that Miaoshou has no such shop."""
    result=match_shops(directory.get('tms',[]),directory.get('miaoshou',[]),manual)
    if directory.get('status')!='COMPLETE':return result
    for shop in directory.get('tms',[]):
        m=result[shop['id']]
        if m['status']!='UNMATCHED':continue
        # A revoked/inactive identity is not an absent shop. Neither a manual
        # conflict nor a failed directory request may silently change sources.
        candidates=[s for s in directory.get('miaoshou',[]) if
            normalized_name(s['name'])==normalized_name(shop['name']) or
            shop.get('platform_shop_id') and shop['platform_shop_id']==s.get('platform_shop_id')]
        result[shop['id']]=dict(status='UNAVAILABLE' if candidates else 'TMS_ONLY',
            method='MIAOSHOU_UNAVAILABLE' if candidates else 'MIAOSHOU_ABSENT',candidates=[s['id'] for s in candidates])
    return result

def apply_mapping(shops,mapping):
    for s in shops:
        m=mapping.get(s['id'],{})
        s['mapping_confirmed']=m.get('status')=='MATCHED'
        s['source_mode']='TMS_ONLY' if m.get('status')=='TMS_ONLY' else 'DUAL'
        if s['mapping_confirmed']:s.update(miaoshou_shop_id=m['miaoshou_shop_id'],miaoshou_shop_name=m['miaoshou_shop_name'])
        elif s['source_mode']=='TMS_ONLY':s.update(miaoshou_shop_id='',miaoshou_shop_name='')

def blank_shop(s):
    return dict(id=s['id'],name=s['name'],tms_shop_id=s['id'],monitor=True,push=False,hidden=False,hidden_skus=[],source_mode='DUAL',display_name='',currency='AUTO',mapping_confirmed=False,miaoshou_shop_id='',miaoshou_shop_name='',sku_aliases={'tms':{},'miaoshou':{}},products=[],default_rules={w:rule({}) for w in WINDOWS})

def migrate_config(config,shops):
    result=deepcopy(config);old={s['id']:s for s in config['shops']};result['view_version']=2
    result.setdefault('collection_mode','ROLLING_INCREMENTAL');result.setdefault('cycle_hours',2)
    if result['collection_mode']=='CYCLE':
        result['collection_mode']='ROLLING_INCREMENTAL'
    result['shops']=[]
    for entry in shops:
        s=deepcopy(old.get(entry['id']) or blank_shop(entry));s.update(name=entry['name'],monitor=True)
        s.setdefault('hidden',False);s.setdefault('display_name','');s.setdefault('default_rules',{w:rule({}) for w in WINDOWS})
        s.setdefault('mapping_choice',None)
        s.setdefault('hidden_skus',[]);s.setdefault('source_mode','DUAL')
        s['default_rules']=ruleset(s['default_rules'])
        if result['collection_mode']=='CYCLE':s['default_rules'].setdefault('cycle',rule({}))
        for p in s['products']:
            p.setdefault('override',True)
            p['rules']=ruleset(p.get('rules',{}))
            if result['collection_mode']=='CYCLE':p['rules'].setdefault('cycle',rule({}))
        result['shops'].append(s)
    return result

def discover_products(orders,catalog,shop):
    lookup={};conflicts=set()
    for fid,f in catalog.items():
        for sku in f['member_skus']:
            if sku in lookup and lookup[sku]!=fid:conflicts.add(sku)
            lookup[sku]=fid
    for sku in conflicts:lookup.pop(sku,None)
    observed={sku for o in orders for sku in o['skus']};grouped={}
    old={p['family_id']:p for p in shop.get('products',[])}
    for sku in sorted(observed):
        fid=lookup.get(sku) or 'sku_'+digest(sku)[:20]
        p=grouped.setdefault(fid,dict(family_id=fid,name=catalog[fid]['product_name'] if fid in catalog else '未分组 · '+sku,skus=[],unknown=fid not in catalog))
        p['skus'].append(sku)
    for fid,p in grouped.items():
        saved=old.get(fid,{})
        p['display_name']=saved.get('display_name','');p['override']=saved.get('override',True) if saved else False
        p['rules']=deepcopy(saved['rules'] if p['override'] and 'rules' in saved else shop['default_rules'])
        p['observed_skus']=p['skus'][:]
        if fid in catalog:p['skus']=sorted(catalog[fid]['member_skus'])
    return list(grouped.values())

def alert_fingerprint(shop,p):
    identity=[shop['id'],shop.get('miaoshou_shop_id'),shop.get('sku_aliases'),p['family_id'],p['skus'],p['rules']]
    if shop.get('source_mode')=='TMS_ONLY':identity.append('TMS_ONLY')
    if p.get('period_hours'):identity.append(['cycle',p['period_hours']])
    return digest(identity)

def shop_windows(orders,cutoff,shop,fx,**kwargs):
    # One order enters this aggregate once regardless of the number of products.
    return calculate(orders,cutoff,{s for o in orders for s in o['skus']},shop['default_rules'],fx,**kwargs)

def visible_cards(config,snapshot,mapping):
    active={'cycle'} if config.get('collection_mode')=='CYCLE' else set(WINDOWS)
    cards={c['id']:deepcopy(c) for c in (snapshot or {}).get('shop_cards',[])}
    out=[]
    for s in config['shops']:
        c=cards.get(s['id'],dict(id=s['id'],status='NOT_COLLECTED',products=[],windows={},updated_at=None))
        c.update(name=s['name'],display_name=s.get('display_name') or s['name'],hidden=s.get('hidden',False),hidden_skus=s.get('hidden_skus',[]),push=s['push'],mapping=mapping.get(s['id'],{'status':'UNMATCHED'}))
        c['windows']={w:v for w,v in c.get('windows',{}).items() if w in active}
        overrides={p['family_id']:p for p in s.get('products',[])}
        for p in c['products']:
            if 'windows' in p:p['windows']={w:v for w,v in p['windows'].items() if w in active}
            if p['family_id'] in overrides:p['display_name']=overrides[p['family_id']].get('display_name','')
        if c['mapping']['status'] not in ('MATCHED','TMS_ONLY'):c['status']='UNMATCHED'
        elif c['mapping']['status']=='TMS_ONLY' and c['status']=='UNMATCHED':c['status']='NOT_COLLECTED'
        out.append(c)
    return out
