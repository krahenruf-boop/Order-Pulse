"""All output comes from one ordered, committed result snapshot."""
import csv
import io
from .models import KalodataError, TERMINAL
COLUMNS=('export_index','video_id','tiktok_url','kalodata_video_url','product_id','region','period','actual_start_date','actual_end_date','source_page','source_index','history_at_start','first_acquired_at','run_id')
def export_items(run,scope='all'):
    if scope not in ('all','new'):raise KalodataError('INVALID_REQUEST',422)
    if run['state'] not in TERMINAL:raise KalodataError('RUN_NOT_FINISHED',409)
    return [i for i in run['items'] if i['status']=='resolved' and (scope=='all' or not i['history_at_start'])]
def csv_safe(value):
    text='' if value is None else str(value)
    stripped=text.lstrip()
    if (stripped and stripped[0] in '=+-@') or any(ord(c)<32 for c in text):text="'"+text
    return text

def make_export(run,fmt='txt',scope='all'):
    if fmt not in ('txt','csv'):raise KalodataError('INVALID_REQUEST',422)
    items=export_items(run,scope)
    if not items:raise KalodataError('EMPTY_RESULT',409)
    suffix='_partial' if run['state']!='completed' or run['coverage'] not in ('requested_range_complete','empty_verified') else ''
    c=run['query_context'];q=run['request']
    filename='kalodata-'+c['product_id']+'-'+c['actual_start_date']+'_'+c['actual_end_date']+'-pages'+str(q['start_page'])+'-'+str(q['end_page'])+'-'+run['run_id']+suffix+('-new' if scope=='new' else '')+'.'+fmt
    if fmt=='txt':return ('\n'.join(i['tiktok_url'] for i in items)+'\n').encode('utf-8'),'text/plain; charset=utf-8',filename
    out=io.StringIO(newline='');writer=csv.writer(out,lineterminator='\r\n');writer.writerow(COLUMNS);c=run['query_context']
    for n,item in enumerate(items,1):
        row=dict(item,export_index=n,product_id=c['product_id'],region=c['region'],period=c['period'],actual_start_date=c['actual_start_date'],actual_end_date=c['actual_end_date'],run_id=run['run_id'])
        row['history_at_start']='true' if item['history_at_start'] else 'false'
        writer.writerow([csv_safe(row.get(k)) for k in COLUMNS])
    return b'\xef\xbb\xbf'+out.getvalue().encode('utf-8'),'text/csv; charset=utf-8',filename
