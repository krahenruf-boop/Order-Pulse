"""Validate observed page anchors; never synthesize a handle or media URL."""
import re
import json
from datetime import date
from urllib.parse import urlsplit, parse_qs
from ..models import KalodataError, strict_url, product_url, iso_time

def detail_url(value, video_id):
    try:
        u=strict_url(value,'www.kalodata.com','/video/detail');q=parse_qs(u.query,keep_blank_values=True)
        if q.get('id')!=[video_id]: raise ValueError()
        if any(k not in ('id','region','language','currency','dateRange') for k in q): raise ValueError()
        if 'dateRange' in q:
            if len(q['dateRange'])!=1:raise ValueError()
            dates=json.loads(q['dateRange'][0])
            if not isinstance(dates,list) or len(dates)!=2 or not all(isinstance(x,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',x) for x in dates):raise ValueError()
            if date.fromisoformat(dates[0])>date.fromisoformat(dates[1]):raise ValueError()
        return value
    except (KalodataError,ValueError): raise KalodataError('LINK_NOT_FOUND')

def verified_link(evidence, video_id, page, index, context, observed_detail=None):
    fields={'kind','observed_href','source_url','observed_at','source_page','source_index'}
    if not isinstance(evidence,dict) or set(evidence)!=fields: raise KalodataError('LINK_NOT_FOUND')
    if type(evidence['source_page'])!=int or type(evidence['source_index'])!=int or evidence['source_page']!=page or evidence['source_index']!=index: raise KalodataError('LINK_ID_MISMATCH')
    href=evidence['observed_href']
    try:
        if not isinstance(href,str) or any(ord(c)<33 for c in href): raise ValueError()
        u=urlsplit(href)
        if u.scheme!='https' or u.hostname!='www.tiktok.com' or u.username is not None or u.password is not None or u.port is not None: raise ValueError()
        match=re.fullmatch(r'/@([A-Za-z0-9_.]{1,64})/video/([0-9]{1,64})/?',u.path)
        if not match: raise ValueError()
    except ValueError: raise KalodataError('LINK_NOT_FOUND')
    if match[2]!=video_id: raise KalodataError('LINK_ID_MISMATCH')
    if evidence['kind']=='detail_anchor':
        if not observed_detail or evidence['source_url']!=observed_detail: raise KalodataError('LINK_NOT_FOUND')
        detail_url(observed_detail,video_id)
        detail_params=parse_qs(urlsplit(observed_detail).query)
        if detail_params.get('dateRange') and json.loads(detail_params['dateRange'][0])!=[context['actual_start_date'],context['actual_end_date']]:raise KalodataError('CONTEXT_CHANGED',409)
        if detail_params.get('region') and detail_params['region']!=[context['region']]:raise KalodataError('LINK_ID_MISMATCH')
    elif evidence['kind']=='list_anchor':
        try: _,p=product_url(evidence['source_url'])
        except KalodataError: raise KalodataError('LINK_NOT_FOUND')
        if p['id']!=context['product_id'] or (p.get('region') and p['region']!=context['region']): raise KalodataError('LINK_ID_MISMATCH')
    else: raise KalodataError('LINK_NOT_FOUND')
    iso_time(evidence['observed_at'])
    return 'https://www.tiktok.com'+u.path.rstrip('/'),dict(evidence)
