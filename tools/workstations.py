"""Manage two isolated local launchd services. No mixed service is started."""
import argparse,hashlib,json,os,plistlib,subprocess,sys,time,urllib.request
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
ROLES={'finance':(8786,'Order Pulse Finance'),'materials':(8797,'Order Pulse Materials')}
def config(role):
    port,folder=ROLES[role];root=Path.home()/'Library/Application Support'/folder;label='local.order-pulse.'+role;path=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
    value=dict(Label=label,ProgramArguments=[str(PROJECT/'.venv/bin/python'),'-m','order_pulse.server'],WorkingDirectory=str(PROJECT),EnvironmentVariables={'ORDER_PULSE_HOME':str(root),'ORDER_PULSE_ROLE':role,'ORDER_PULSE_PRODUCTION':'1'},RunAtLoad=True,KeepAlive=True,ThrottleInterval=10,ProcessType='Background',StandardOutPath=str(root/'logs/service.log'),StandardErrorPath=str(root/'logs/service.log'))
    return port,root,path,value,f'gui/{os.getuid()}/{label}'
def health(role):
    port,root,*_=config(role)
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=2) as r:value=json.load(r)
    if value.get('role')!=role or value.get('data_root_id')!=hashlib.sha256(str(root).encode()).hexdigest():raise RuntimeError('端口被其他后台占用')
    return value

def run(action,role):
    port,root,path,value,target=config(role)
    if path.exists():
        current=plistlib.loads(path.read_bytes())
        if any(current.get(k)!=value[k] for k in ('Label','ProgramArguments','WorkingDirectory','EnvironmentVariables')):raise RuntimeError('启动项身份不一致，未覆盖')
    if action=='status':
        try:print(role,health(role))
        except OSError:print(role,'stopped')
        return
    loaded=subprocess.run(['launchctl','print',target],capture_output=True).returncode==0
    if action=='stop':
        if loaded:
            r=subprocess.run(['launchctl','bootout',target],capture_output=True)
            if r.returncode:raise RuntimeError('停止服务失败')
        for _ in range(60):
            present=subprocess.run(['launchctl','print',target],capture_output=True).returncode==0
            try:health(role);serving=True
            except OSError:serving=False
            if not present and not serving:break
            time.sleep(.15)
        else:raise RuntimeError('后台尚未完成退出，未重启')
        if path.exists():path.unlink()
        print(role,'stopped');return
    if not loaded:
        import socket
        probe=socket.socket()
        try:
            probe.bind(('127.0.0.1',port))
        except OSError:
            try:health(role);print(role,'already running');return
            except OSError:raise RuntimeError(f'{port} 端口不可用，未启动或停止其他服务') from None
        finally:probe.close()
    (root/'logs').mkdir(parents=True,exist_ok=True,mode=0o700)
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:plistlib.dump(value,f)
    if loaded:
        for _ in range(60):
            try:health(role);print(role,'already running');return
            except OSError:pass
            if subprocess.run(['launchctl','print',target],capture_output=True).returncode!=0:break
            time.sleep(.15)
        else:raise RuntimeError('后台尚未完成退出，请稍后启动')
    cmd=['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)]
    r=subprocess.run(cmd,capture_output=True)
    if r.returncode and not loaded:
        time.sleep(1);r=subprocess.run(cmd,capture_output=True)
    if r.returncode:raise RuntimeError('启动服务失败')
    for _ in range(30):
        try:health(role);print(role,f'http://127.0.0.1:{port}');return
        except (OSError,RuntimeError):time.sleep(.5)
    raise RuntimeError('服务尚未就绪，请检查该工作台日志')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status']);p.add_argument('role',choices=[*ROLES,'all'],default='all',nargs='?');a=p.parse_args()
    for role in ROLES if a.role=='all' else [a.role]:run(a.action,role)
