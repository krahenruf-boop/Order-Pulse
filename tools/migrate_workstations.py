"""One-time offline, consistent migration. Never recomputes business history."""
import hashlib,json,os,shutil,sqlite3,subprocess
from datetime import datetime
from pathlib import Path
BASE=Path.home()/'Library/Application Support';OLD=BASE/'Order Pulse'

def fingerprint(db,table):
    # Byte-exact row multiset check independent of SQLite page layout.
    total=0;count=0
    for row in db.execute('SELECT * FROM "'+table+'"'):
        total=(total+int.from_bytes(hashlib.sha256(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()).digest(),'big'))%(1<<256);count+=1
    return dict(count=count,digest=f'{total:064x}')

def main():
    for label in ('local.order-pulse.service','local.order-pulse.finance','local.order-pulse.materials'):
        if subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True).returncode==0:raise RuntimeError('请先正常停止后台后再迁移')
    roots={role:BASE/f'Order Pulse {name}' for role,name in [('finance','Finance'),('materials','Materials')]}
    if any(p.exists() for p in roots.values()):raise RuntimeError('目标数据目录已存在，未覆盖')
    archive=OLD/'backups'/('workstation-split-'+datetime.now().strftime('%Y%m%d-%H%M%S'));archive.mkdir(parents=True,mode=0o700)
    with sqlite3.connect(OLD/'order_pulse.sqlite3') as source,sqlite3.connect(archive/'order_pulse.sqlite3') as backup:source.backup(backup)
    shutil.copytree(OLD/'config',archive/'config')
    report={};settings=None
    with sqlite3.connect(archive/'order_pulse.sqlite3') as src:
        tables=[r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        settings={k:json.loads(v) for k,v in src.execute('SELECT key,value FROM settings')}
        creds=json.loads((OLD/'config/credentials.json').read_text())
        for role,root in roots.items():
            root.mkdir(mode=0o700);(root/'config').mkdir(mode=0o700);(root/'logs').mkdir(mode=0o700)
            dbpath=root/'order_pulse.sqlite3'
            with sqlite3.connect(dbpath) as dst:
                # Copy only owned schema and rows; no opposite-module free pages.
                keep=[t for t in tables if t=='settings' or (t.startswith('balance_') if role=='finance' else not t.startswith('balance_'))]
                for table in keep:
                    sql=src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0];dst.execute(sql)
                    if table=='settings':continue
                    cols=src.execute('PRAGMA table_info("'+table+'")').fetchall();marks=','.join('?' for _ in cols)
                    dst.executemany('INSERT INTO "'+table+'" VALUES('+marks+')',src.execute('SELECT * FROM "'+table+'"'))
                for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name IN ("+','.join('?' for _ in keep)+')',keep):dst.execute(sql)
                for key,value in settings.items():
                    if key=='browser_bindings':value={k:v for k,v in value.items() if (k=='balance_kalodata')==(role=='finance')}
                    elif role=='finance':continue
                    dst.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
                dst.execute('PRAGMA user_version=4');dst.commit()
                report[role]={t:fingerprint(dst,t) for t in keep if t!='settings'}
                for t,fp in report[role].items():
                    if fp!=fingerprint(src,t):raise RuntimeError('迁移校验失败：'+t)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('数据库完整性校验失败')
            dbpath.chmod(0o600)
            namespaces=creds.get('namespaces',{})
            clean={'namespaces':{'balance':namespaces['balance']}} if role=='finance' else {**{k:v for k,v in creds.items() if k!='namespaces'},'namespaces':{k:v for k,v in namespaces.items() if k!='balance'}}
            path=root/'config/credentials.json';path.write_text(json.dumps(clean,ensure_ascii=False));path.chmod(0o600)
            if role=='materials':
                for name in ('aov-refund-verification.json','aov-verification.json','modules/tms_orders'):
                    source=OLD/name;target=root/name
                    if source.is_dir():shutil.copytree(source,target)
                    elif source.is_file():shutil.copy2(source,target)
    (archive/'migration-report.json').write_text(json.dumps(report,indent=2));(archive/'migration-report.json').chmod(0o600)
    (OLD/'OFFLINE-ARCHIVE.txt').write_text('旧混合后台已停用。本目录只作离线回滚归档，不参与运行。\n新工作台：Order Pulse Finance (8786)、Order Pulse Materials (8797)。\n')
    print('一致性备份与逐表校验完成：',archive)
if __name__=='__main__':main()
