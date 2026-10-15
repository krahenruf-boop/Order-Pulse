from .domain import aware,window_for
from .monitor_domain import digest
from .source_time import request_window
SOURCES=('completed','current')
POLICY_VERSION='operational-observed-20260922'
COVERAGE_NOTE='统计当前订单 + 已完成已发货；忽略退货、作废、归档等状态。'
USAGE_NOTE='数据用于运营趋势监测，不作为财务结算依据。'

def sales_scope(config,binding,cutoff,mode=None):
    start,end=window_for(dict(config,window_mode=mode or config['window_mode']),cutoff)
    if start==end:raise ValueError('EMPTY_WINDOW')
    stores=[s for s in binding['stores'] if s['platform_id']==104 and s['company_id']==binding['account']['customer_id']]
    ids=sorted(s['shop_id'] for s in stores)
    if not ids or len(ids)!=len(set(ids)):raise ValueError('CATALOG_CHANGED')
    return dict(start=start.isoformat(),end=end.isoformat(),platform='TIKTOK',shop_ids=ids,skus=sorted(config['skus']),
                partitions=list(SOURCES),partition_states={'current':0,'completed':5},account=binding['account'],
                binding_revision=binding['revision'],config_revision=config['revision'],catalog_id=digest(ids),
                source_ranges={p:request_window(p,start.isoformat(),end.isoformat()) for p in SOURCES},
                window_mode=mode or config['window_mode'],cutoff=cutoff,policy_version=POLICY_VERSION)

def observed_quantity(item):
    """Direct positive integral purchase quantities only; SKU text has no arithmetic."""
    from decimal import Decimal, InvalidOperation
    for field in ('platformQuantity','productNum'):
        value=item.get(field)
        if isinstance(value,bool) or not isinstance(value,(str,int,float)):continue
        try:
            number=Decimal(str(value))
            if number.is_finite() and number>0 and number==number.to_integral_value():
                return int(number),False,field
        except (InvalidOperation,ValueError):pass
    return 1,True,'FALLBACK_ONE'

def observed_metrics(records,scope,history=()):
    """Exact literal reference equality only; never infer parcel/parent relationships.

    Repeated copies of one order contribute one quantity per SKU. Prefer a copy
    with direct quantities, then completed before current, then stable row key.
    Conflicting copies are disclosed, not added together. Historical observations
    preserve first-seen time only and never inflate this round's observed count.
    """
    targets=set(scope['skus']);start,end=aware(scope['start']),aware(scope['end'])
    groups=[];aliases={}
    def refs(r):
        values=r.get('order_references') or {}
        return sorted({v for k in ('originalOrderId','platformOrderId')
                       for v in [values.get(k)] if isinstance(v,str) and v.strip()})
    def keys(r):return [(r['shop_id'],v) for v in refs(r)]
    # A union by equality, not a split/merge relationship model.
    for r in sorted(records,key=lambda r:(r['partition']!='completed',r['record_key'])):
        if r['shop_id'] not in scope['shop_ids'] or not start<=aware(r['paid_at'])<end:raise ValueError('ROW_SCOPE_MISMATCH')
        if not set(r['matched_queries'])<=targets:raise ValueError('EXACT_SKU_MISMATCH')
        rk=keys(r) or [(r['shop_id'],'source:'+r['record_key'])]
        found={aliases[k] for k in rk if k in aliases}
        index=min(found) if found else len(groups)
        if not found:groups.append([])
        for other in sorted(found-{index}):
            groups[index].extend(groups[other]);groups[other]=[]
            for key,value in list(aliases.items()):
                if value==other:aliases[key]=index
        groups[index].append(r)
        for key in rk:aliases[key]=index
    old_first={}
    for r in history:
        if r.get('account_scope')!=digest(scope['account']):continue
        for key in keys(r):
            if r.get('first_seen_at'):
                identity=(key,aware(r['paid_at']))
                old_first[identity]=min(old_first.get(identity,r['first_seen_at']),r['first_seen_at'],key=aware)
    orders=[]
    for group in groups:
        if not group:continue
        if len({aware(r['paid_at']) for r in group})!=1:raise ValueError('PAYMENT_IDENTITY_CONFLICT')
        options={};all_refs=sorted({v for r in group for v in refs(r)})
        for r in group:
            items=r.get('source_items',[])
            members=(set(r['matched_queries'])|{i.get('stockSku') for i in items})&targets
            for sku in sorted(members):
                quantities=[observed_quantity(i) for i in items if i.get('stockSku')==sku] or [(1,True,'FALLBACK_ONE')]
                option=dict(sku_quantity=sum(q[0] for q in quantities),quantity_estimated=any(q[1] for q in quantities),
                            quantity_sources=sorted({q[2] for q in quantities}),source_record_key=r['record_key'],partition=r['partition'])
                options.setdefault(sku,[]).append(option)
        sku_values={}
        for sku,values in sorted(options.items()):
            chosen=dict(sorted(values,key=lambda v:(v['quantity_estimated'],v['partition']!='completed',v['source_record_key']))[0])
            chosen['quantity_conflict']=len({v['sku_quantity'] for v in values})>1
            sku_values[sku]=chosen
        r=group[0];first=[x['first_seen_at'] for x in group if x.get('first_seen_at')]
        first += [old_first[(k,aware(r['paid_at']))] for x in group for k in keys(x) if (k,aware(r['paid_at'])) in old_first]
        orders.append(dict(observed_order_key=digest([scope['account'],r['shop_id'],all_refs or ['source:'+r['record_key']]]),
                           shop_id=r['shop_id'],paid_at=r['paid_at'],order_references=all_refs,
                           identity_estimated=not bool(all_refs),skus=sku_values,
                           quantity_estimated=any(v['quantity_estimated'] for v in sku_values.values()),
                           source_record_keys=sorted({x['record_key'] for x in group}),
                           first_seen_at=min(first,key=aware) if first else None))
    def counts(values):
        values=list(values);skus={}
        for sku in sorted(targets):
            relevant=[o['skus'][sku] for o in values if sku in o['skus']]
            skus[sku]=dict(sku_order_count=len(relevant),sku_quantity=sum(v['sku_quantity'] for v in relevant),
                           quantity_estimated=any(v['quantity_estimated'] for v in relevant),
                           quantity_estimated_order_count=sum(v['quantity_estimated'] for v in relevant),
                           quantity_conflict_order_count=sum(v['quantity_conflict'] for v in relevant))
        return dict(product_order_count=len(values),observed_paid_orders=len(values),skus=skus,
                    quantity_estimated_order_count=sum(o['quantity_estimated'] for o in values),
                    identity_estimated_order_count=sum(o['identity_estimated'] for o in values))
    result=counts(orders)
    result.update(stores={sid:counts(o for o in orders if o['shop_id']==sid) for sid in sorted({o['shop_id'] for o in orders})},
                  metric_policy=POLICY_VERSION,coverage_note=COVERAGE_NOTE,usage_note=USAGE_NOTE,
                  duplicate_record_count=len(records)-len(orders))
    return result,orders
