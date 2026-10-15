from datetime import datetime, timezone, timedelta
import hashlib,json,re
from .domain import aware
PARTITIONS={'current':{'route':'myorderlist','state':0},'completed':{'route':'myorderhistorylist','state':5}}

def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def binding_input(data):
    if not isinstance(data,dict) or set(data)!={'space_id','page_label'}:
        raise ValueError('请选择空间和页面；不能提交账号或店铺目录')
    if type(data['space_id']) is not int or data['space_id']<1 or not isinstance(data['page_label'],str) or not re.fullmatch(r'p[1-9][0-9]*',data['page_label']):
        raise ValueError('空间/页面无效')
    return dict(data)

def parse_collection(payload,scope):
    """Keep candidate rows, including unresolved compound SKU text. No sales guess."""
    if not isinstance(payload,dict) or payload.get('account')!=scope['account']:raise ValueError('ACCOUNT_SCOPE_CHANGED')
    batches=payload.get('batches')
    expected={(p,s) for p in scope['partitions'] for s in scope['skus']}
    if not isinstance(batches,list) or len(batches)!=len(expected):raise ValueError('INCOMPLETE_SCOPE')
    result={};seen_batches=set();receipts=[];boundary_excluded=0
    start,end=aware(scope['start']),aware(scope['end'])
    for batch in batches:
        key=(batch.get('partition'),batch.get('sku'))
        if key not in expected or key in seen_batches:raise ValueError('INCOMPLETE_SCOPE')
        seen_batches.add(key);partition,sku=key
        pages=batch.get('pages',[])
        if not pages:raise ValueError('INCOMPLETE_PAGINATION')
        total=None;ids=set();first_rows=None;page_receipts=[]
        for n,page in enumerate(pages,1):
            q=page.get('request',{})
            timekey='orderTimes' if partition=='current' else 'timeStr'
            if (q.get('page')!=n or q.get('pageSize')!=50 or q.get(timekey)!=scope['source_ranges'][partition]
                or q.get('stockSku')!=sku or str(q.get('keytype2'))!='1'
                or q.get('orderStatus')!=PARTITIONS[partition]['state']
                or sorted(map(str,q.get('shopIdsList',[])))!=scope['shop_ids']
                or (partition=='current' and (q.get('skutype')!='5' or q.get('platformId')!=104))):
                raise ValueError('REQUEST_SCOPE_MISMATCH')
            rows=page.get('rows');t=page.get('total')
            empty=page.get('business_code')=='2' and page.get('explicit_empty') is True and t==0 and rows in (None,[])
            if page.get('http_status')!=200 or not (page.get('business_code')=='200' or empty):raise ValueError('SOURCE_FAILED')
            if empty and rows is None:rows=[]
            if type(t) is not int or t<0 or not isinstance(rows,list) or len(rows)>50:raise ValueError('INVALID_PAGE')
            if total is not None and t!=total:raise ValueError('SNAPSHOT_CHANGED')
            total=t
            if (n-1)*50+len(rows)>total or (n*50<total and len(rows)!=50):raise ValueError('INCOMPLETE_PAGINATION')
            if n==1:first_rows=rows
            for raw in rows:
                rid=raw.get('id')
                if not isinstance(rid,str) or not rid or rid in ids:raise ValueError('DUPLICATE_OR_LOSSY_ROW_ID')
                ids.add(rid)
                if str(raw.get('shopId')) not in scope['shop_ids'] or raw.get('platformId')!=104:raise ValueError('ROW_SCOPE_MISMATCH')
                if str(raw.get('companyId'))!=scope['account']['customer_id'] or str(raw.get('supplierId'))!=scope['account']['supplier_id']:raise ValueError('ACCOUNT_SCOPE_CHANGED')
                ts=raw.get('orderTime')
                if type(ts) is not int or not 0<ts<2**53:raise ValueError('PAYMENT_INVALID')
                try:paid=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=ts)
                except (ValueError,OverflowError):raise ValueError('PAYMENT_INVALID') from None
                if paid==end:boundary_excluded+=1;continue
                if not start<=paid<end:raise ValueError('SOURCE_TIME_MISMATCH')
                # Do not split comma text or infer parent relationships from flags.
                sku_text=raw.get('stockSku')
                if not isinstance(sku_text,str) or not sku_text:raise ValueError('SKU_METADATA_MISSING')
                compound=sku_text!=sku
                if compound and ',' not in sku_text:raise ValueError('EXACT_SKU_MISMATCH')
                refs={k:raw.get(k) if isinstance(raw.get(k),str) else None for k in ('orderId','platformOrderId','originalOrderId')}
                record_key=digest([scope['account'],partition,str(raw['shopId']),rid])
                item={'record_key':record_key,'source_row_id':rid,'partition':partition,'shop_id':str(raw['shopId']),
                      'paid_at':paid.isoformat(),'paid_at_raw':ts,'source_sku_text':sku_text,'matched_queries':[sku],
                      'sku_status':'DETAILS_UNVERIFIED' if compound else 'EXACT_QUERY_OBSERVED',
                      'identity_status':'UNVERIFIED','order_references':refs,
                      'is_split_raw':raw.get('isSplit'),'is_union_raw':raw.get('isUnion')}
                if record_key in result:
                    before=dict(result[record_key]);before.pop('matched_queries');after=dict(item);after.pop('matched_queries')
                    if before!=after:raise ValueError('ROW_CHANGED')
                    result[record_key]['matched_queries']=sorted(set(result[record_key]['matched_queries']+[sku]))
                else:result[record_key]=item
            page_receipts.append({'page':n,'rows':len(rows),'total':t,'request':q})
        if len(ids)!=total or not batch.get('end_confirmed'):raise ValueError('INCOMPLETE_PAGINATION')
        if batch.get('recheck_total')!=total or batch.get('recheck_rows')!=first_rows:raise ValueError('SNAPSHOT_CHANGED')
        receipts.append({'partition':partition,'sku':sku,'pages':page_receipts,'source_total':total,'first_page_rechecked':True})
    records=sorted(result.values(),key=lambda r:(r['partition'],r['shop_id'],r['source_row_id']))
    return {'records':records,'collected_scope':scope,'receipts':receipts,'run_status':'SUCCEEDED',
            'scope_collection_status':'COMPLETE_DECLARED_SCOPE','coverage_status':'PARTIAL',
            'identity_status':'UNVERIFIED','candidate_record_count':len(records),'unresolved_record_count':len(records),
            'full_paid_orders':None,'excluded_upper_boundary':boundary_excluded,
            'warnings':['仅完整读取所声明的来源范围，全部付款状态覆盖尚未核验','原始平台订单及拆合单身份未核验；候选记录数不是付款订单数']}
