BEGIN;
ALTER TABLE kalodata_runs ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}';
INSERT OR IGNORE INTO kalodata_schema_migrations(version,applied_at)
VALUES(2,strftime('%Y-%m-%dT%H:%M:%SZ','now'));
COMMIT;
