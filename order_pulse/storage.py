"""Independent storage. No imports or database connections to the old application."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .tms_core.monitor_domain import stamp

class Repository:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for d in ('config', 'logs'):
            (self.root / d).mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / 'order_pulse.sqlite3'
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError('DATABASE_VERSION_UNSUPPORTED')
            # Retained modules share a database with separate namespaces.
            db.executescript('''
              BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS product_catalog_versions (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);

              CREATE TABLE IF NOT EXISTS balance_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS balance_accounts (source TEXT NOT NULL, asset_id TEXT NOT NULL, data TEXT NOT NULL, config TEXT NOT NULL, alert TEXT NOT NULL, legacy_source_id TEXT, PRIMARY KEY(source,asset_id));
              CREATE TABLE IF NOT EXISTS balance_logs (id INTEGER PRIMARY KEY, at TEXT NOT NULL, level TEXT, kind TEXT, message TEXT, task_id TEXT, details TEXT, legacy_source_id TEXT UNIQUE);
              CREATE TABLE IF NOT EXISTS balance_push_events (id TEXT PRIMARY KEY, kind TEXT, created_at TEXT, status TEXT, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL, legacy_source_id TEXT UNIQUE);
              CREATE TABLE IF NOT EXISTS balance_schedules (key TEXT PRIMARY KEY, at TEXT, status TEXT, event_id TEXT, legacy_source_id TEXT UNIQUE);

              CREATE TABLE IF NOT EXISTS kalodata_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS kalodata_runs (
                run_id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES kalodata_runs(run_id),
                client_request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('created','preflighting','reading','resolving','completed','completed_with_errors','partial','cancelled','failed','blocked','interrupted')),
                request_json TEXT NOT NULL, query_context_json TEXT,
                history_snapshot_json TEXT NOT NULL DEFAULT '[]',
                coverage TEXT NOT NULL DEFAULT 'unknown' CHECK(coverage IN ('requested_range_complete','partial','empty_verified','unknown')),
                site_cap_observed INTEGER NOT NULL DEFAULT 0 CHECK(site_cap_observed IN (0,1)),
                stop_reason TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>0),
                result_revision INTEGER NOT NULL DEFAULT 0 CHECK(result_revision>=0),
                legacy_source_id TEXT UNIQUE
              );
              CREATE TABLE IF NOT EXISTS kalodata_run_pages (
                run_id TEXT NOT NULL REFERENCES kalodata_runs(run_id), source_page INTEGER NOT NULL CHECK(source_page>0),
                page_size INTEGER NOT NULL CHECK(page_size>0), observed_count INTEGER NOT NULL CHECK(observed_count>=0),
                page_fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('observed','verified','failed')),
                legacy_source_id TEXT UNIQUE, PRIMARY KEY(run_id,source_page)
              );
              CREATE TABLE IF NOT EXISTS kalodata_run_items (
                run_id TEXT NOT NULL, source_page INTEGER NOT NULL, source_index INTEGER NOT NULL CHECK(source_index>0),
                video_id TEXT CHECK(video_id IS NULL OR (length(video_id)>0 AND video_id NOT GLOB '*[^0-9]*')),
                status TEXT NOT NULL CHECK(status IN ('resolved','unresolved','duplicate','invalid_row')),
                duplicate_page INTEGER, duplicate_index INTEGER, tiktok_url TEXT, kalodata_video_url TEXT,
                link_evidence_json TEXT, history_at_start INTEGER NOT NULL CHECK(history_at_start IN (0,1)),
                first_acquired_at TEXT, error_code TEXT, legacy_source_id TEXT UNIQUE,
                PRIMARY KEY(run_id,source_page,source_index),
                FOREIGN KEY(run_id,source_page) REFERENCES kalodata_run_pages(run_id,source_page),
                FOREIGN KEY(run_id,duplicate_page,duplicate_index) REFERENCES kalodata_run_items(run_id,source_page,source_index),
                CHECK((duplicate_page IS NULL)=(duplicate_index IS NULL)),
                CHECK(status<>'resolved' OR (video_id IS NOT NULL AND tiktok_url IS NOT NULL AND link_evidence_json IS NOT NULL AND first_acquired_at IS NOT NULL))
              );
              CREATE TABLE IF NOT EXISTS kalodata_material_links (
                platform TEXT NOT NULL CHECK(platform='tiktok'), video_id TEXT NOT NULL CHECK(length(video_id)>0 AND video_id NOT GLOB '*[^0-9]*'),
                verified_url TEXT NOT NULL, first_acquired_at TEXT NOT NULL, last_verified_at TEXT NOT NULL,
                source TEXT NOT NULL, legacy_source_id TEXT UNIQUE, PRIMARY KEY(platform,video_id)
              );
              CREATE TABLE IF NOT EXISTS kalodata_product_video_relations (
                provider TEXT NOT NULL CHECK(provider='kalodata'), product_id TEXT NOT NULL, region TEXT NOT NULL,
                platform TEXT NOT NULL, video_id TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                legacy_source_id TEXT UNIQUE, PRIMARY KEY(provider,product_id,region,platform,video_id),
                FOREIGN KEY(platform,video_id) REFERENCES kalodata_material_links(platform,video_id)
              );
              CREATE TABLE IF NOT EXISTS kalodata_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS kalodata_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS legacy_migrations (
                source TEXT NOT NULL, version INTEGER NOT NULL, applied_at TEXT NOT NULL,
                summary_json TEXT NOT NULL, PRIMARY KEY(source,version)
              );
              INSERT OR IGNORE INTO kalodata_schema_migrations(version,applied_at) VALUES(1,datetime('now'));
              PRAGMA user_version=4;
              COMMIT;
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def active_catalog(self):
        version=self.get('active_catalog_version')
        if not version:return None
        with self.db() as db:
            row=db.execute('SELECT payload FROM product_catalog_versions WHERE id=? AND status=?',(version,'ACTIVE')).fetchone()
        return json.loads(row[0]) if row else None

    def activate_catalog(self, catalog, activated_at):
        """Publish a complete ordinary+combo candidate in one transaction."""
        version='catalog_'+catalog['content_hash'][:16]
        payload=dict(catalog,catalog_version=version,activated_at=activated_at)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active=db.execute("SELECT value FROM settings WHERE key='active_catalog_version'").fetchone()
            if active:
                old_id=json.loads(active[0])
                old=db.execute('SELECT payload FROM product_catalog_versions WHERE id=?',(old_id,)).fetchone()
                if old and json.loads(old[0]).get('content_hash')==catalog['content_hash']:
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('catalog_last_checked',json.dumps(activated_at)))
                    return dict(json.loads(old[0]),unchanged=True,checked_at=activated_at)
            db.execute("UPDATE product_catalog_versions SET status='HISTORICAL' WHERE status='ACTIVE'")
            db.execute('INSERT OR REPLACE INTO product_catalog_versions VALUES (?,?,?,?)',
                       (version,'ACTIVE',activated_at,json.dumps(payload,ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('active_catalog_version',json.dumps(version)))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('catalog_last_checked',json.dumps(activated_at)))
        return dict(payload,unchanged=False)

    def record_catalog_failure(self, attempted_at, code):
        self.set('catalog_last_attempt',dict(status='FAILED',attempted_at=attempted_at,error=code))

    def product_overrides(self):
        from .product_catalog import default_overrides
        return self.get('product_group_overrides',default_overrides())

    def save_product_overrides(self, value):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                       ('product_group_overrides',json.dumps(value,ensure_ascii=False)))
        return value

class Credentials:
    def __init__(self, root):
        self.path = Path(root) / 'config/credentials.json'

    def get(self):
        if self.path.is_symlink():
            raise ValueError('凭据路径不可用')
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def configured(self):
        return bool(self.get().get('webhook'))

    def namespace(self, name):
        if not isinstance(name, str) or not name or '.' in name:
            raise ValueError('凭据命名空间无效')
        value = self.get().get('namespaces', {}).get(name, {})
        return dict(value) if isinstance(value, dict) else {}

    def save_namespace(self, name, value):
        if not isinstance(value, dict):
            raise ValueError('凭据配置无效')
        data = self.get()
        namespaces = dict(data.get('namespaces', {}))
        namespaces[name] = dict(value)
        data['namespaces'] = namespaces
        self.save(data)

    def save(self, data):
        from .feishu import validate_webhook
        if data.get('webhook'):
            validate_webhook(data['webhook'])
        import tempfile
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.secret-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f); f.flush(); os.fsync(f.fileno())
            os.replace(name, self.path)
            self.path.chmod(0o600)
        finally:
            if os.path.exists(name): os.unlink(name)
