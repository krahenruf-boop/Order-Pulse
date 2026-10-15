"""Per-partition time contracts backed by the local source evidence.

These are session-scoped observations, not a global TMS/account timezone.
Archive time interpretation is deliberately unavailable until a nonempty sample.
"""
from datetime import timedelta, timezone
from .domain import aware


SOURCE_TIME = {
    'current': {'offset_hours': 8, 'parameter': 'orderTimes',
                'evidence': 'stage-b-backend-current-preview.json'},
    'completed': {'offset_hours': -3, 'parameter': 'timeStr',
                  'evidence': 'stage-b-completed-payment-boundary.json'},
}


def request_window(partition, start, end):
    if partition not in SOURCE_TIME:
        raise ValueError('SOURCE_TIME_UNVERIFIED')
    start,end=aware(start),aware(end)
    if start>=end or start.microsecond or end.microsecond:
        raise ValueError('来源查询范围应为非空整秒区间')
    zone=timezone(timedelta(hours=SOURCE_TIME[partition]['offset_hours']))
    return [value.astimezone(zone).strftime('%Y-%m-%d %H:%M:%S') for value in (start,end)]
