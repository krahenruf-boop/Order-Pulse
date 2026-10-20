"""Persistent binding and process-local mutex for the Balance/Kalodata Space."""
from __future__ import annotations

import re
import json
import threading
from copy import deepcopy

from .ego_runtime import BALANCE_SPACE_NAMES, validate_target


KEY = "browser_bindings"
PURPOSE = "balance_kalodata"
DEDICATED_SPACE_NAME = BALANCE_SPACE_NAMES[0]


def _label(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value,str) or not re.fullmatch(r"p[1-9][0-9]*",value):
        raise ValueError("浏览器页面绑定无效")
    return value


def validate_binding(value):
    if not isinstance(value,dict):
        raise ValueError("专用浏览器绑定无效")
    required={"space_id","expected_name","profile_id","profile_name","purpose","auto_takeover","tabs"}
    if set(value)!=required or type(value.get("space_id")) is not int or value["space_id"]<0:
        raise ValueError("专用浏览器绑定无效")
    if (not isinstance(value["expected_name"],str) or not value["expected_name"].strip() or len(value["expected_name"])>150 or value["purpose"]!=PURPOSE or value["auto_takeover"] is not True):
        raise ValueError("专用浏览器只能绑定 Order Pulse 余额监控空间")
    for key in ("profile_id","profile_name"):
        if not isinstance(value[key],str) or not value[key] or len(value[key])>100:
            raise ValueError("专用浏览器身份无效")
    tabs=value["tabs"]
    if not isinstance(tabs,dict) or set(tabs)!={"balance_tab","kalodata_tab"}:
        raise ValueError("专用浏览器页面登记无效")
    balance=_label(tabs["balance_tab"])
    kalodata=_label(tabs["kalodata_tab"],optional=True)
    if kalodata is not None and kalodata==balance:
        raise ValueError("余额与素材页面必须使用不同标签页")
    return {
        "space_id":value["space_id"],"expected_name":value["expected_name"],
        "profile_id":value["profile_id"],"profile_name":value["profile_name"],
        "purpose":PURPOSE,"auto_takeover":True,
        "tabs":{"balance_tab":balance,"kalodata_tab":kalodata},
    }


class BrowserSpaceBinding:
    """The sole owner of the Balance/Kalodata browser binding.

    ``lock`` is intentionally process-local: both browser modules live in the
    same Order Pulse backend process, so it prevents them switching the shared
    Space at the same time without introducing another service or scheduler.
    """
    def __init__(self,repo):
        self.repo=repo
        self.lock=threading.Lock()

    def get(self):
        value=self.repo.get(KEY,{})
        item=value.get(PURPOSE) if isinstance(value,dict) else None
        return validate_binding(item) if item is not None else None

    def save(self,value,*,tms_space_id=None):
        clean=validate_binding(value)
        if tms_space_id is not None and clean["space_id"]==tms_space_id:
            policy=self.repo.get('dedicated_browser') or {}
            target=policy.get('target') or {}
            if not (policy.get('shared_order_pulse_space') is True and
                    target.get('space_id')==clean['space_id'] and
                    target.get('page_label') not in clean['tabs'].values() and
                    self.get()==clean):
                raise ValueError("余额与素材专用Space不能与TMS专用Space相同")
        all_bindings=self.repo.get(KEY,{})
        if not isinstance(all_bindings,dict):all_bindings={}
        all_bindings=dict(all_bindings);all_bindings[PURPOSE]=deepcopy(clean)
        self.repo.set(KEY,all_bindings)
        return clean

    def record_balance_space(self,expected,browser):
        """Persist only a verified relocation of the same logical Balance Space."""
        current=self.get()
        if not current or self.target("balance")!=expected:
            raise ValueError("余额浏览器绑定已被更改，请重新查询")
        if (type(browser.get("space_id")) is not int or browser["space_id"]<0 or
            browser.get("space_name")!=current["expected_name"] or
            browser.get("profile_id")!=current["profile_id"] or
            browser.get("profile_name")!=current["profile_name"]):
            raise ValueError("浏览器返回的专用 Space 身份不一致")
        if current["space_id"]!=browser["space_id"]:
            current=dict(current,space_id=browser["space_id"])
            bindings=dict(self.repo.get(KEY,{}));bindings[PURPOSE]=validate_binding(current)
            self.repo.set(KEY,bindings)
        return self.target("balance")

    def target(self,kind):
        if kind not in ("balance","kalodata"):
            raise ValueError("浏览器页面用途无效")
        value=self.get()
        if not value:return None
        label=value["tabs"][kind+"_tab"]
        if label is None:return None
        return validate_target({
            "space_id":value["space_id"],"space_name":value["expected_name"],"page_label":label,
            "expected_profile_id":value["profile_id"],"expected_profile_name":value["profile_name"],
            "purpose":PURPOSE,"auto_takeover":value["auto_takeover"],
        })

    def state(self):
        value=self.get()
        return {"bound":value is not None,"binding":deepcopy(value),"lock_busy":self.lock.locked()}


OPERATIONS_PURPOSE = 'material_aov'
OPERATIONS_SPACE_NAME = 'Order Pulse · 素材与客单价'

class OperationsSpaceBinding:
    def __init__(self,repo):
        self.repo=repo
        self.lock=threading.Lock()

    @staticmethod
    def validate(value):
        required={'space_id','expected_name','profile_id','profile_name','purpose','auto_takeover','tabs'}
        if not isinstance(value,dict) or set(value)!=required:
            raise ValueError('素材与客单价空间绑定无效')
        if type(value['space_id']) is not int or value['space_id']<0 or value['expected_name']!=OPERATIONS_SPACE_NAME or value['purpose']!=OPERATIONS_PURPOSE:
            raise ValueError('素材与客单价空间身份不一致')
        for k in ('profile_id','profile_name'):
            if not isinstance(value[k],str) or not value[k] or len(value[k])>100:raise ValueError('浏览器身份无效')
        if type(value['auto_takeover']) is not bool:raise ValueError('浏览器接管设置无效')
        if not isinstance(value['tabs'],dict) or set(value['tabs'])!={'tms_tab','miaoshou_tab','kalodata_tab'}:raise ValueError('页面登记无效')
        for v in value['tabs'].values():_label(v)
        if len(set(value['tabs'].values()))!=3:raise ValueError('来源必须使用不同标签页')
        return deepcopy(value)

    def get(self):
        value=self.repo.get(KEY,{})
        item=value.get(OPERATIONS_PURPOSE) if isinstance(value,dict) else None
        return self.validate(item) if item is not None else None

    def save(self,value):
        clean=self.validate(value)
        bindings=dict(self.repo.get(KEY,{}))
        balance=bindings.get(PURPOSE) or {}
        if balance.get('space_id')==clean['space_id']:raise ValueError('余额与素材客单价必须分开空间')
        bindings[OPERATIONS_PURPOSE]=clean
        self.repo.set(KEY,bindings)
        return clean

    def record_space(self,expected,browser):
        """Persist verified session-ID recovery without overwriting changed settings."""
        expected=self.validate(expected)
        if (not isinstance(browser,dict) or type(browser.get('space_id')) is not int or browser['space_id']<0 or
            browser.get('space_name')!=expected['expected_name'] or
            browser.get('profile_id')!=expected['profile_id'] or browser.get('profile_name')!=expected['profile_name']):
            raise ValueError('素材与客单价空间身份不一致')
        recovered=dict(expected,space_id=browser['space_id'])
        with self.repo.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
            bindings=json.loads(row[0]) if row else {}
            current=bindings.get(OPERATIONS_PURPOSE)
            # Another verified lookup may already have recorded this exact recovery.
            if current not in (expected,recovered):raise ValueError('浏览器绑定已更改，请重新检查')
            if current!=recovered:
                bindings[OPERATIONS_PURPOSE]=recovered
                db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(bindings,ensure_ascii=False),KEY))
        return recovered

    def state(self):
        value=self.get()
        return {'bound':value is not None,'binding':value,'lock_busy':self.lock.locked()}
