"""After acceptance, keep one verified compressed rollback archive, remove duplicates."""
import gzip,hashlib,os,shutil,subprocess
from pathlib import Path
from workstations import health

def main():
    health('finance');health('materials')
    if subprocess.run(['launchctl','print',f'gui/{os.getuid()}/local.order-pulse.service'],capture_output=True).returncode==0:raise RuntimeError('旧混合后台仍在运行')
    old=Path.home()/'Library/Application Support/Order Pulse'
    candidates=sorted((old/'backups').glob('workstation-split-*/migration-report.json'))
    if len(candidates)!=1:raise RuntimeError('回滚归档不唯一，未清理')
    archive=candidates[0].parent;source=archive/'order_pulse.sqlite3';target=archive/'order_pulse.sqlite3.gz'
    if target.exists():raise RuntimeError('压缩归档已存在，未覆盖')
    digest=hashlib.sha256()
    with source.open('rb') as f,os.fdopen(os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as out:
        with gzip.GzipFile(fileobj=out,mode='wb',compresslevel=1) as zipped:
            while chunk:=f.read(4*1024*1024):digest.update(chunk);zipped.write(chunk)
    check=hashlib.sha256()
    with gzip.open(target,'rb') as zipped:
        while chunk:=zipped.read(4*1024*1024):check.update(chunk)
    if check.digest()!=digest.digest():target.unlink();raise RuntimeError('压缩归档核验失败，未删除原文件')
    (archive/'SHA256.txt').write_text(digest.hexdigest()+'  order_pulse.sqlite3 (uncompressed)\n');source.unlink()
    reclaimed=0
    for path in old.glob('*.sqlite3*'):
        if path.is_file() and not path.is_symlink():reclaimed+=path.stat().st_size;path.unlink()
    for path in (old/'backups').iterdir():
        if path!=archive and path.is_dir() and not path.is_symlink():
            reclaimed+=sum(f.stat().st_size for f in path.rglob('*') if f.is_file());shutil.rmtree(path)
    for name in ('config','logs','modules'):
        path=old/name
        if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
    for name in ('aov-verification.json','aov-refund-verification.json','service.lock'):
        (old/name).unlink(missing_ok=True)
    print('已清理旧运行库和重复备份；保留已校验压缩回滚归档。',round(reclaimed/1024**3,2),'GiB')
    print('离线归档大小',round(target.stat().st_size/1024**2,1),'MiB')
if __name__=='__main__':main()
