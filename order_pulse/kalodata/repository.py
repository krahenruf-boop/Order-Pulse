"""Independent persistence. All writes are transactional and generation-checked."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from .models import ACTIVE, KalodataError, now_iso, COUNT_KEYS
from .item_normalizer import KalodataItemNormalizer

def dumps(x):return json.dumps(x,ensure_ascii=False,separators=(',',':'))
class Repository:
    def __init__(self,path):self.path=Path(path);self.db=None;self.writable=False
    def open(self,writable=False):
        self.close();self.writable=writable
        if not writable and not self.path.exists():return
        if writable:
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);self.path.parent.chmod(0o700)
        try:
            uri=str(self.path) if writable else self.path.as_uri()+'?mode=ro'
            self.db=sqlite3.connect(uri,uri=not writable,timeout=2);self.db.row_factory=sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables and writable:
                sql=(Path(__file__).parent/'migrations/001_init.sql').read_text()
                self.db.executescript(sql)
            elif not tables:raise KalodataError('STORAGE_ERROR',503)
            versions=[r[0] for r in self.db.execute('SELECT version FROM kalodata_schema_migrations')]
            if writable and versions==[1]:
                self.db.executescript((Path(__file__).parent/'migrations/002_plugin_diagnostics.sql').read_text())
                versions=[r[0] for r in self.db.execute('SELECT version FROM kalodata_schema_migrations')]
            if versions not in ([1],[1,2]) or (writable and versions!=[1,2]):raise KalodataError('STORAGE_ERROR',503)
            if writable:self.path.chmod(0o600)
        except Exception:
            self.close();raise KalodataError('STORAGE_ERROR',503)
    def close(self):
        if self.db:self.db.close()
        self.db=None
    @contextmanager
    def tx(self):
        if not self.writable or self.db is None:raise KalodataError('STORAGE_ERROR',503)
        try:
            self.db.execute('BEGIN IMMEDIATE');yield self.db;self.db.commit()
        except KalodataError:self.db.rollback();raise
        except Exception:self.db.rollback();raise KalodataError('STORAGE_ERROR',503)
    def state(self,key,default=None):
        if self.db is None:return default
        row=self.db.execute('SELECT value_json FROM kalodata_state WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_state(self,key,value):
        with self.tx() as db:db.execute('INSERT OR REPLACE INTO kalodata_state(key,value_json) VALUES(?,?)',(key,dumps(value)))
    def recover(self):
        with self.tx() as db:
            db.execute("UPDATE kalodata_runs SET state='interrupted',stop_reason='INTERRUPTED',finished_at=?,generation=generation+1,result_revision=result_revision+1 WHERE state IN ('created','preflighting','reading','resolving')",(now_iso(),))
    def _row(self,run_id):
        row=self.db.execute('SELECT * FROM kalodata_runs WHERE run_id=?',(run_id,)).fetchone() if self.db else None
        if not row:raise KalodataError('NOT_FOUND',404)
        return row
    def _active(self,db,run_id,generation):
        row=db.execute('SELECT * FROM kalodata_runs WHERE run_id=?',(run_id,)).fetchone()
        if not row or row['state'] not in ACTIVE or row['generation']!=generation:raise KalodataError('CANCELLED',409)
        return row
    def by_client(self,client_id):
        if not self.db:return None
        row=self.db.execute('SELECT run_id FROM kalodata_runs WHERE client_request_id=?',(client_id,)).fetchone()
        return self.get_run(row[0]) if row else None
    def create(self,run_id,client_id,request,context,parent=None):
        with self.tx() as db:
            if db.execute("SELECT 1 FROM kalodata_runs WHERE state IN ('created','preflighting','reading','resolving')").fetchone():raise KalodataError('CONFLICT',409)
            history=[r[0] for r in db.execute("SELECT video_id FROM kalodata_material_links WHERE platform='tiktok'")]
            db.execute('INSERT INTO kalodata_runs(run_id,parent_run_id,client_request_id,state,request_json,query_context_json,history_snapshot_json,started_at,site_cap_observed) VALUES(?,?,?,?,?,?,?,?,?)',(run_id,parent,client_id,'created',dumps(request),dumps(context),dumps(history),now_iso(),int(context['visible_pages'] is not None)))
        return self.get_run(run_id)
    def stage(self,run_id,generation,state):
        if state not in ACTIVE:raise ValueError('Invalid stage')
        with self.tx() as db:
            self._active(db,run_id,generation);db.execute('UPDATE kalodata_runs SET state=? WHERE run_id=?',(state,run_id))
    def set_context(self,run_id,generation,context):
        with self.tx() as db:
            self._active(db,run_id,generation)
            db.execute('UPDATE kalodata_runs SET query_context_json=?,site_cap_observed=? WHERE run_id=?',
                       (dumps(context),int(context['visible_pages'] is not None),run_id))
    def page(self,run_id,generation,page,page_size,count,fingerprint,observed_at):
        with self.tx() as db:
            self._active(db,run_id,generation)
            if count and db.execute('SELECT 1 FROM kalodata_run_pages WHERE run_id=? AND page_fingerprint=? AND source_page<>?',(run_id,fingerprint,page)).fetchone():raise KalodataError('PAGE_MISMATCH',409)
            db.execute('INSERT INTO kalodata_run_pages(run_id,source_page,page_size,observed_count,page_fingerprint,observed_at,status) VALUES(?,?,?,?,?,?,?)',(run_id,page,page_size,count,fingerprint,observed_at,'observed'))
    def row(self,run_id,generation,page,index,video_id,detail=None):
        with self.tx() as db:
            run=self._active(db,run_id,generation)
            first=db.execute('SELECT * FROM kalodata_run_items WHERE run_id=? AND video_id=? ORDER BY source_page,source_index LIMIT 1',(run_id,video_id)).fetchone() if video_id else None
            duplicate=(first['source_page'],first['source_index']) if first else (None,None)
            status='invalid_row' if video_id is None else 'duplicate' if first else 'unresolved'
            db.execute('INSERT INTO kalodata_run_items(run_id,source_page,source_index,video_id,status,duplicate_page,duplicate_index,kalodata_video_url,history_at_start,error_code) VALUES(?,?,?,?,?,?,?,?,?,?)',(run_id,page,index,video_id,status,*duplicate,detail,int(video_id in json.loads(run['history_snapshot_json'])), 'SCHEMA_CHANGED' if video_id is None else None))
            db.execute('UPDATE kalodata_runs SET result_revision=result_revision+1 WHERE run_id=?',(run_id,))
        return first
    def failure(self,run_id,generation,page,index,code):
        with self.tx() as db:
            self._active(db,run_id,generation)
            db.execute('UPDATE kalodata_run_items SET error_code=? WHERE run_id=? AND source_page=? AND source_index=?',(code,run_id,page,index))
            db.execute('UPDATE kalodata_runs SET result_revision=result_revision+1 WHERE run_id=?',(run_id,))
    def resolve(self,run_id,generation,page,index,url,evidence,detail):
        with self.tx() as db:
            run=self._active(db,run_id,generation)
            item=db.execute('SELECT * FROM kalodata_run_items WHERE run_id=? AND source_page=? AND source_index=?',(run_id,page,index)).fetchone()
            if not item or not item['video_id']:raise KalodataError('STORAGE_ERROR',503)
            vid=item['video_id'];stamp=now_iso();context=json.loads(run['query_context_json'])
            db.execute("INSERT INTO kalodata_material_links(platform,video_id,verified_url,first_acquired_at,last_verified_at,source) VALUES('tiktok',?,?,?,?,?) ON CONFLICT(platform,video_id) DO UPDATE SET verified_url=excluded.verified_url,last_verified_at=excluded.last_verified_at",(vid,url,stamp,stamp,'kalodata'))
            acquired=db.execute("SELECT first_acquired_at FROM kalodata_material_links WHERE platform='tiktok' AND video_id=?",(vid,)).fetchone()[0]
            db.execute("INSERT INTO kalodata_product_video_relations(provider,product_id,region,platform,video_id,first_seen_at,last_seen_at) VALUES('kalodata',?,?,'tiktok',?,?,?) ON CONFLICT(provider,product_id,region,platform,video_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",(context['product_id'],context['region'],vid,stamp,stamp))
            # A later duplicate can supply the first valid evidence. Preserve the original position.
            target=(item['duplicate_page'],item['duplicate_index']) if item['duplicate_page'] else (page,index)
            db.execute("UPDATE kalodata_run_items SET status='resolved',tiktok_url=?,kalodata_video_url=?,link_evidence_json=?,first_acquired_at=?,error_code=NULL WHERE run_id=? AND source_page=? AND source_index=?",(url,detail,dumps(evidence),acquired,run_id,*target))
            if target!=(page,index):
                db.execute('UPDATE kalodata_run_items SET tiktok_url=?,link_evidence_json=?,first_acquired_at=?,error_code=NULL WHERE run_id=? AND source_page=? AND source_index=?',(url,dumps(evidence),acquired,run_id,page,index))
            db.execute('UPDATE kalodata_runs SET result_revision=result_revision+1 WHERE run_id=?',(run_id,))
    def verify_page(self,run_id,generation,page):
        with self.tx() as db:
            self._active(db,run_id,generation)
            db.execute("UPDATE kalodata_run_pages SET status='verified' WHERE run_id=? AND source_page=?",(run_id,page))
    def finish(self,run_id,state,reason=None,diagnostics=None):
        with self.tx() as db:
            row=db.execute('SELECT * FROM kalodata_runs WHERE run_id=?',(run_id,)).fetchone()
            if not row or row['state'] not in ACTIVE:return
            r=self.get_run(run_id);missing=r['missing_pages']
            coverage='partial' if r['counts']['visited_rows'] or r['visited_pages'] else 'unknown'
            if not missing:
                coverage='requested_range_complete' if r['counts']['visited_rows'] else 'empty_verified'
            fields={k:v[:80] for k,v in (diagnostics or {}).items() if k in {'expected_range','observed_range','observed_start','observed_end'} and isinstance(v,str)}
            db.execute('UPDATE kalodata_runs SET state=?,stop_reason=?,coverage=?,diagnostics_json=?,finished_at=?,generation=generation+1,result_revision=result_revision+1 WHERE run_id=?',(state,reason,coverage,dumps(fields),now_iso(),run_id))
    def get_items(self,run_id,include_duplicates=False):
        self._row(run_id)
        rows=self.db.execute('SELECT * FROM kalodata_run_items WHERE run_id=? ORDER BY source_page,source_index',(run_id,)).fetchall()
        result=[]
        for r in rows:
            if not include_duplicates and r['status']=='duplicate':continue
            result.append(dict(source_page=r['source_page'],source_index=r['source_index'],video_id=r['video_id'],duplicate_of=dict(source_page=r['duplicate_page'],source_index=r['duplicate_index']) if r['duplicate_page'] else None,status=r['status'],tiktok_url=r['tiktok_url'],kalodata_video_url=r['kalodata_video_url'],link_evidence=json.loads(r['link_evidence_json']) if r['link_evidence_json'] else None,history_at_start=bool(r['history_at_start']),first_acquired_at=r['first_acquired_at'],error_code=r['error_code']))
        return result
    def get_run(self,run_id,items=True):
        r=self._row(run_id);all_items=self.get_items(run_id,True);counts=dict.fromkeys(COUNT_KEYS,0)
        counts['visited_rows']=len(all_items)
        for item in all_items:
            if item['status']=='invalid_row':counts['invalid_rows']+=1
            elif item['status']=='duplicate':counts['duplicate_occurrences']+=1
            else:
                counts['unique_videos']+=1
                if item['status']=='resolved':
                    counts['resolved_unique']+=1;counts['already_acquired_unique' if item['history_at_start'] else 'new_unique']+=1
                else:counts['unresolved_unique']+=1
        context=json.loads(r['query_context_json']) if r['query_context_json'] else {}
        for item in all_items:
            item['normalized']=KalodataItemNormalizer.normalize(item,page_size=context.get('page_size'))
        request=json.loads(r['request_json']);visited=[x[0] for x in self.db.execute("SELECT source_page FROM kalodata_run_pages WHERE run_id=? AND status='verified' ORDER BY source_page",(run_id,))]
        state=r['state'];coverage=r['coverage'];reason=r['stop_reason'];finished=r['finished_at']
        if not self.writable and state in ACTIVE:
            state='interrupted';reason='INTERRUPTED';finished=None;coverage='partial' if all_items or visited else 'unknown'
        status=('QUEUED' if state=='created' else 'RUNNING' if state in ACTIVE else 'SUCCEEDED_WITH_WARNING' if state=='completed' and context.get('date_status')!='MATCHED' else 'SUCCEEDED' if state=='completed' else
                'CANCELLED' if state=='cancelled' else 'PARTIAL_SUCCESS' if counts['resolved_unique'] else 'FAILED')
        current_page=self.db.execute('SELECT MAX(source_page) FROM kalodata_run_pages WHERE run_id=?',(run_id,)).fetchone()[0]
        diagnostics=json.loads(r['diagnostics_json']) if 'diagnostics_json' in r.keys() else {}
        return dict(run_id=r['run_id'],parent_run_id=r['parent_run_id'],state=state,status=status,transport=request.get('transport','legacy'),current_page=current_page,request=request,query_context=context if context.get('product_id') else None,counts=counts,coverage=coverage,visited_pages=visited,missing_pages=[p for p in range(request['start_page'],request['end_page']+1) if p not in visited],site_cap_observed=bool(r['site_cap_observed']),stop_reason=reason,diagnostics=diagnostics,started_at=r['started_at'],finished_at=finished,generation=r['generation'],result_revision=r['result_revision'],items=[x for x in all_items if x['status']!='duplicate'] if items else [])
    def list_runs(self,limit=20):
        if self.db is None:return []
        return [self.get_run(r[0],False) for r in self.db.execute('SELECT run_id FROM kalodata_runs ORDER BY started_at DESC,run_id DESC LIMIT ?',(limit,)).fetchall()]
    def history(self,product_id=None,region=None,offset=0,limit=100):
        if self.db is None:return []
        sql="SELECT r.*,m.verified_url,m.first_acquired_at,m.last_verified_at FROM kalodata_product_video_relations r JOIN kalodata_material_links m USING(platform,video_id) WHERE 1=1";args=[]
        if product_id:sql+=' AND r.product_id=?';args.append(product_id)
        if region:sql+=' AND r.region=?';args.append(region)
        sql+=' ORDER BY r.first_seen_at,r.product_id,r.region,r.video_id LIMIT ? OFFSET ?';args.extend([limit,offset])
        return [dict(r) for r in self.db.execute(sql,args).fetchall()]
