-- Design-only schema. Execute only against a NEW isolated Kalodata database.
-- Never execute against monitor.sqlite3. Application must enforce docs/05 invariants.
PRAGMA foreign_keys = ON;
BEGIN;
CREATE TABLE IF NOT EXISTS kalodata_schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
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
  result_revision INTEGER NOT NULL DEFAULT 0 CHECK(result_revision>=0)
);
CREATE TABLE IF NOT EXISTS kalodata_run_pages (
  run_id TEXT NOT NULL REFERENCES kalodata_runs(run_id),
  source_page INTEGER NOT NULL CHECK(source_page>0),
  page_size INTEGER NOT NULL CHECK(page_size>0),
  observed_count INTEGER NOT NULL CHECK(observed_count>=0),
  page_fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('observed','verified','failed')),
  PRIMARY KEY(run_id,source_page)
);
CREATE TABLE IF NOT EXISTS kalodata_run_items (
  run_id TEXT NOT NULL, source_page INTEGER NOT NULL,
  source_index INTEGER NOT NULL CHECK(source_index>0),
  video_id TEXT CHECK(video_id IS NULL OR (length(video_id)>0 AND video_id NOT GLOB '*[^0-9]*')),
  status TEXT NOT NULL CHECK(status IN ('resolved','unresolved','duplicate','invalid_row')),
  duplicate_page INTEGER, duplicate_index INTEGER,
  tiktok_url TEXT, kalodata_video_url TEXT, link_evidence_json TEXT,
  history_at_start INTEGER NOT NULL CHECK(history_at_start IN (0,1)),
  first_acquired_at TEXT, error_code TEXT,
  PRIMARY KEY(run_id,source_page,source_index),
  FOREIGN KEY(run_id,source_page) REFERENCES kalodata_run_pages(run_id,source_page),
  FOREIGN KEY(run_id,duplicate_page,duplicate_index) REFERENCES kalodata_run_items(run_id,source_page,source_index),
  CHECK((duplicate_page IS NULL)=(duplicate_index IS NULL)),
  CHECK(status<>'resolved' OR (video_id IS NOT NULL AND tiktok_url IS NOT NULL AND link_evidence_json IS NOT NULL AND first_acquired_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS kalodata_material_links (
  platform TEXT NOT NULL CHECK(platform='tiktok'),
  video_id TEXT NOT NULL CHECK(length(video_id)>0 AND video_id NOT GLOB '*[^0-9]*'),
  verified_url TEXT NOT NULL, first_acquired_at TEXT NOT NULL,
  last_verified_at TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY(platform,video_id)
);
CREATE TABLE IF NOT EXISTS kalodata_product_video_relations (
  provider TEXT NOT NULL CHECK(provider='kalodata'), product_id TEXT NOT NULL,
  region TEXT NOT NULL, platform TEXT NOT NULL, video_id TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(provider,product_id,region,platform,video_id),
  FOREIGN KEY(platform,video_id) REFERENCES kalodata_material_links(platform,video_id)
);
CREATE TABLE IF NOT EXISTS kalodata_state (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_video ON kalodata_run_items(video_id,run_id);
CREATE INDEX IF NOT EXISTS relations_product ON kalodata_product_video_relations(product_id,region);
INSERT OR IGNORE INTO kalodata_schema_migrations(version,applied_at) VALUES(1,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
COMMIT;
