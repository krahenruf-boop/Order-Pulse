from ..models import KalodataError, digest, is_id, iso_time

def validate_page(batch, page, context):
    if not isinstance(batch,dict) or type(batch.get('source_page'))!=int or batch.get('source_page')!=page or batch.get('context_fingerprint')!=context['context_fingerprint']:
        raise KalodataError('PAGE_MISMATCH',409,{'source_page':page})
    if batch.get('product_id')!=context['product_id'] or batch.get('region')!=context['region']: raise KalodataError('CONTEXT_CHANGED',409)
    if type(batch.get('page_size'))!=int or batch.get('page_size')!=context['page_size'] or batch.get('verified') is not True: raise KalodataError('PAGE_MISMATCH',409)
    if context['visible_pages'] is not None and 'visible_pages' in batch and batch['visible_pages']!=context['visible_pages']: raise KalodataError('CONTEXT_CHANGED',409)
    rows=batch.get('rows')
    if not isinstance(rows,list) or len(rows)>context['page_size'] or not all(isinstance(r,dict) for r in rows): raise KalodataError('SCHEMA_CHANGED')
    if not rows and not (page==1 and batch.get('empty_verified') is True and context['visible_total']==0): raise KalodataError('PAGE_MISMATCH',409)
    total=context['visible_total']
    if total is None and len(rows)<context['page_size'] and batch.get('terminal_page') is not True:raise KalodataError('PAGE_MISMATCH',409)
    if total is not None and len(rows)!=min(context['page_size'],max(0,total-(page-1)*context['page_size'])):raise KalodataError('PAGE_MISMATCH',409)
    iso_time(batch.get('observed_at'))
    # Invalid numeric IDs remain visible as invalid rows; never stringify them.
    fingerprint=digest([r.get('video_id') if is_id(r.get('video_id')) else None for r in rows])
    return rows,fingerprint
