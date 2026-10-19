"""Persistent collection-health incidents, separate from balance-value alerts."""
from datetime import datetime, timedelta, timezone
import json
import uuid

from .store import parse_time

KINDS = frozenset({'monitor_fault', 'monitor_reminder', 'monitor_recovery', 'monitor_test'})
KEY = 'collection_health_v1'
REMINDER = timedelta(hours=6)
RETRY = timedelta(minutes=15)


class CollectionHealth:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def enabled(self):
        s = self.store.settings()
        return s['setup_complete'] and not s['paused']

    def state(self):
        return self.store.get(KEY, {})

    def last_success(self):
        return parse_time(self.store.get('last_success'))

    def _stage(self, state, now):
        # Silence since the last successful read is independently meaningful;
        # do not claim that all of that time was a verified connection failure.
        anchors = [parse_time(state['started_at'])]
        if self.last_success():
            anchors.append(max(self.last_success(), parse_time(state.get('resumed_at')) or self.last_success()))
        elif state.get('resumed_at'):
            anchors.append(parse_time(state['resumed_at']))
        return max(0, int(max(now - t for t in anchors) / REMINDER))

    def _queue(self, state, now):
        stage = self._stage(state, now)
        if stage <= state.get('stage', -1):
            return
        kind = 'monitor_reminder' if stage else 'monitor_fault'
        payload = dict(episode=state['episode'], stage=stage, error=state['error'],
                       error_code=state['error_code'], started_at=state['started_at'],
                       last_success_at=self.store.get('last_success'), detected_at=now.isoformat())
        self.store.event(kind, payload, event_id=f"balance-health-{state['episode']}-{stage}")
        state['stage'] = stage

    def failure(self, message, code='COLLECTION_FAILED', *, at=None):
        if not self.enabled():
            return
        now = self.clock()
        with self.store.transaction():
            state = self.state()
            if not state.get('active'):
                state = dict(episode=uuid.uuid4().hex, active=True, stage=-1,
                             started_at=(at or now).isoformat(), resumed_at=state.get('resumed_at'))
            state.update(error=str(message)[:700], error_code=str(code)[:64])
            self._queue(state, now)
            self.store.set(KEY, state)

    def success(self):
        if not self.enabled():
            return
        with self.store.transaction():
            state = self.state()
            if not state.get('active'):
                return
            state.update(active=False, recovered_at=self.clock().isoformat())
            self.store.set(KEY, state)
            # Queue atomically even if an initial send is finishing; validity is
            # checked again before sending so an unseen incident has no recovery.
            self.store.event('monitor_recovery', dict(episode=state['episode'],
                started_at=state['started_at'], detected_at=state['recovered_at'],
                last_success_at=self.store.get('last_success')),
                event_id=f"balance-health-{state['episode']}-recovery")

    def _notified(self, episode):
        with self.store.lock:
            rows = self.store.db.execute("SELECT payload FROM balance_push_events WHERE kind IN ('monitor_fault','monitor_reminder') AND status IN ('success','uncertain','sending')").fetchall()
        return any(json.loads(r[0]).get('episode') == episode for r in rows)

    def valid(self, event):
        if event['kind'] == 'monitor_test':
            return True
        if not self.enabled():
            return False
        state, p = self.state(), event['payload']
        if state.get('episode') != p.get('episode'):
            return False
        if event['kind'] == 'monitor_recovery':
            return not state.get('active') and self._notified(p['episode'])
        return state.get('active') is True and p.get('stage') == state.get('stage')

    def poll(self):
        now, state = self.clock(), self.state()
        if not self.enabled():
            if not state.get('paused'):
                self.store.set(KEY, dict(state, active=False, paused=True))
            return
        if state.get('paused'):
            state = dict(active=False, resumed_at=now.isoformat())
            self.store.set(KEY, state)
        if not self.last_success() and not state.get('resumed_at'):
            state['resumed_at']=now.isoformat()
            self.store.set(KEY,state)
        collection = self.store.get('collection', {})
        success = self.last_success()
        baseline = max(t for t in (success, parse_time(state.get('resumed_at'))) if t) if success or state.get('resumed_at') else None
        if collection.get('status') == 'failed':
            self.failure(collection.get('error') or '余额采集未完成', collection.get('error_code') or 'COLLECTION_FAILED')
        elif baseline and now - baseline >= REMINDER:
            self.failure('已超过 6 小时没有成功采集余额，请检查 Linkme 授权、浏览器和采集任务', 'NO_SUCCESS_6H')
        elif state.get('active') and success and success > parse_time(state['started_at']):
            self.success()
        if self.state().get('active'):
            with self.store.transaction():
                state = self.state()
                self._queue(state, now)
                self.store.set(KEY, state)
        # Only definitely failed deliveries may retry. Timeouts/uncertain sends
        # retain their event IDs and are never retried automatically.
        for event in self.store.events(include_payload=True):
            if event['kind'] not in KINDS or event['status'] != 'failed' or not self.valid(event):
                continue
            retry = parse_time(event['payload'].get('retry_at'))
            if retry and now >= retry:
                self.store.update_event(event['id'], 'pending', '故障通知等待重试', payload=event['payload'])

    def delivery(self, event, status):
        state = self.state()
        state['last_delivery'] = dict(event_id=event['id'], status=status, at=self.clock().isoformat())
        if status == 'success':
            state['last_alert_at'] = self.clock().isoformat()
        self.store.set(KEY, state)
        if status == 'failed':
            event['payload']['retry_at'] = (self.clock() + RETRY).isoformat()
            with self.store.transaction() as db:
                db.execute('UPDATE balance_push_events SET payload=? WHERE id=?',
                           (json.dumps(event['payload'],ensure_ascii=False),event['id']))

    def status(self):
        state = self.state()
        success = self.last_success()
        baseline=max(t for t in (success,parse_time(state.get('resumed_at'))) if t) if success or state.get('resumed_at') else None
        overdue = bool(baseline and self.clock() - baseline >= REMINDER)
        active = self.enabled() and (state.get('active') or overdue)
        return dict(enabled=self.enabled(), active=bool(active), reminder_hours=6,
                    started_at=state.get('started_at') if active else None,
                    error=state.get('error') if active else None,
                    last_delivery=state.get('last_delivery'), last_alert_at=state.get('last_alert_at'))

    def message(self, event, tz):
        from .engine import local_time
        p, kind = event['payload'], event['kind']
        if kind == 'monitor_test':
            return 'Order Pulse 余额监控 · 故障报警测试\n这是一条测试消息，不代表当前连接故障。\n规则：采集失败时报警，持续异常每 6 小时提醒，恢复后通知一次。'
        title = {'monitor_fault':'余额监控连接／采集异常', 'monitor_reminder':'余额监控长时间异常提醒',
                 'monitor_recovery':'余额监控已恢复'}[kind]
        lines = [f'Order Pulse · {title}', f"检测时间：{local_time(p['detected_at'], tz)}",
                 f"最近成功采集：{local_time(p.get('last_success_at'), tz)}"]
        if kind == 'monitor_recovery':
            lines.append('已重新成功采集余额，自动监控继续运行。')
        else:
            if kind == 'monitor_reminder':
                lines.append(f"持续异常或未成功采集已达到 {p['stage'] * 6} 小时；本轮仅提醒一次。")
            # Use the current reason if it changed while the same outage remained.
            lines.extend([f"原因：{self.state().get('error') or p['error']}",
                '上次成功余额已保留，不代表当前实时余额；低余额判断等待有效数据。',
                '请在「Order Pulse · 余额监控」检查 Linkme 登录和验证码。',
                'https://tt.linkme.live/client/dashboard'])
        return '\n'.join(lines)
