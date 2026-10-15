"""B2 composes the proved UI/time contracts; it does not invent original-order links."""
from .monitor_domain import parse_collection, digest
from .sales_domain import COVERAGE_NOTE, USAGE_NOTE
import time


class SalesCollector:
    def __init__(self,connector):self.connector=connector

    def collect(self,binding,scope,continue_check=lambda:True):
        records={};receipts=[];excluded=0;started=time.monotonic()
        # Internal shards only. The product-facing scope always contains the complete
        # current authorized TikTok catalog. No user-managed mandatory shop selection.
        for offset in range(0,len(scope['shop_ids']),10):
            if not continue_check():return {'error':'SERVICE_INTERRUPTED'}
            if time.monotonic()-started>1200:return {'error':'SCOPE_TOO_LARGE'}
            shard=dict(scope,shop_ids=scope['shop_ids'][offset:offset+10])
            raw=self.connector.execute(dict(mode='collect',space_id=binding['space_id'],page_label=binding['page_label'],
                                            account=binding['account'],scope=shard,include_items=True))
            if raw.get('error'):return raw
            parsed=parse_collection(raw,shard)
            details={}
            for batch in raw['batches']:
                for page in batch['pages']:
                    for row in page['rows']:
                        key=digest([scope['account'],batch['partition'],str(row['shopId']),row['id']])
                        items=row.get('itemList')
                        if not isinstance(items,list):continue
                        cleaned=[]
                        for item in items:
                            if not isinstance(item,dict):raise ValueError('ITEM_STRUCTURE_CHANGED')
                            # Statistical source observations only; no buyer data or parcel inference.
                            clean={k:item.get(k) for k in ('id','taskItemId','taskId','orderItemId','transactionId','platformOrderId','originalOrderId','stockSku','originalSku','platformSku','productNum','itemCount','platformQuantity','originTaskId','packageId','isCombo','groupId','itemId') if k in item}
                            cleaned.append(clean)
                        if key in details and details[key]!=cleaned:raise ValueError('SNAPSHOT_CHANGED')
                        details[key]=cleaned
            for record in parsed['records']:
                if record['record_key'] in records:raise ValueError('OVERLAPPING_SHOP_SHARDS')
                record['source_items']=details.get(record['record_key'],[])
                if record['source_items']:
                    observed={i.get('stockSku') for i in record['source_items']}
                    if not set(record['matched_queries'])<=observed:raise ValueError('DETAIL_EXACT_SKU_MISMATCH')
                    record['sku_status']='EXACT_DETAIL_OBSERVED'
                records[record['record_key']]=record
            if len(records)>10000:return {'error':'SCOPE_TOO_LARGE'}
            receipts.extend(dict(r,shop_ids=shard['shop_ids']) for r in parsed['receipts'])
            excluded+=parsed['excluded_upper_boundary']
        return dict(scope=scope,records=list(records.values()),receipts=receipts,
                    scope_collection_status='COMPLETE_DECLARED_SCOPE',coverage_status='DECLARED_SOURCES',
                    candidate_record_count=len(records),excluded_upper_boundary=excluded,
                    coverage_note=COVERAGE_NOTE,usage_note=USAGE_NOTE,warnings=[])
