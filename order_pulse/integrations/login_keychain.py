"""One app-owned macOS Keychain item; secrets travel only through private pipes.

The small helper process disables interactive Keychain prompts for background
reads. It never enumerates browser/password-manager entries or accepts arbitrary
service names. No password is passed in process arguments or written to a file.
"""
import ctypes as c
import json
from pathlib import Path
import re
import subprocess
import sys

SERVICE=b'com.order-pulse.tms-login'

class KeychainError(Exception):
    pass

class LoginKeychain:
    def _call(self,operation,scope,value=None):
        if not re.fullmatch(r'[a-f0-9]{64}',scope):raise KeychainError('KEYCHAIN_SCOPE_INVALID')
        try:
            p=subprocess.run([sys.executable,str(Path(__file__).resolve()),operation,scope],
                input=json.dumps(value) if value is not None else '',text=True,capture_output=True,timeout=20)
            result=json.loads(p.stdout)
        except (OSError,subprocess.TimeoutExpired,ValueError):raise KeychainError('KEYCHAIN_UNAVAILABLE') from None
        if p.returncode or result.get('error'):raise KeychainError(result.get('error','KEYCHAIN_UNAVAILABLE'))
        return result.get('value')

    def read(self,scope):return self._call('read',scope)
    def save(self,scope,value):self._call('save',scope,value)

def native(operation,scope,value):
    if sys.platform!='darwin':raise KeychainError('KEYCHAIN_UNAVAILABLE')
    security=c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf=c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    find=security.SecKeychainFindGenericPassword
    find.argtypes=[c.c_void_p,c.c_uint32,c.c_char_p,c.c_uint32,c.c_char_p,c.POINTER(c.c_uint32),c.POINTER(c.c_void_p),c.POINTER(c.c_void_p)]
    find.restype=c.c_int32
    add=security.SecKeychainAddGenericPassword
    add.argtypes=[c.c_void_p,c.c_uint32,c.c_char_p,c.c_uint32,c.c_char_p,c.c_uint32,c.c_void_p,c.POINTER(c.c_void_p)]
    add.restype=c.c_int32
    modify=security.SecKeychainItemModifyContent
    modify.argtypes=[c.c_void_p,c.c_void_p,c.c_uint32,c.c_void_p];modify.restype=c.c_int32
    free=security.SecKeychainItemFreeContent
    free.argtypes=[c.c_void_p,c.c_void_p];free.restype=c.c_int32
    cf.CFRelease.argtypes=[c.c_void_p]
    interaction=security.SecKeychainSetUserInteractionAllowed
    interaction.argtypes=[c.c_ubyte];interaction.restype=c.c_int32
    # Setup is explicitly user-initiated; background access never opens a prompt.
    interaction(1 if operation=='save' else 0)
    account=scope.encode();length=c.c_uint32();data=c.c_void_p();item=c.c_void_p()
    try:
        status=find(None,len(SERVICE),SERVICE,len(account),account,c.byref(length),c.byref(data),c.byref(item))
        if operation=='read':
            if status==-25300:return None
            if status:raise KeychainError('KEYCHAIN_LOCKED')
            if length.value>8192:raise KeychainError('KEYCHAIN_INVALID')
            return json.loads(c.string_at(data,length.value))
        raw=json.dumps(value).encode()
        if len(raw)>8192:raise KeychainError('KEYCHAIN_INVALID')
        if status==-25300:status=add(None,len(SERVICE),SERVICE,len(account),account,len(raw),raw,None)
        elif status==0:status=modify(item,None,len(raw),raw)
        if status:raise KeychainError('KEYCHAIN_LOCKED')
    finally:
        if data.value:free(None,data)
        if item.value:cf.CFRelease(item)

if __name__=='__main__':
    try:
        operation,scope=sys.argv[1:]
        if operation not in ('read','save') or not re.fullmatch(r'[a-f0-9]{64}',scope):raise KeychainError('KEYCHAIN_SCOPE_INVALID')
        value=json.loads(sys.stdin.read(8193)) if operation=='save' else None
        print(json.dumps({'value':native(operation,scope,value)}))
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,KeychainError) else 'KEYCHAIN_UNAVAILABLE'}))
        sys.exit(1)
