from datetime import datetime, time, timedelta, timezone
import re
BUSINESS_TZ=timezone(timedelta(hours=-3))

def aware(value):
    if not isinstance(value, str):
        raise ValueError('付款时间必须是带时区的文本')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('付款时间格式无效') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('付款时间缺少已核验时区')
    return result.astimezone(timezone.utc)

def window_for(config, cutoff):
    """Freeze the cutoff supplied by the caller; never use completion time."""
    cutoff = aware(cutoff)
    local = cutoff.astimezone(BUSINESS_TZ)
    midnight = datetime.combine(local.date(), time(), BUSINESS_TZ)
    mode = config['window_mode']
    if mode == 'custom':
        start, end = aware(config['custom_start']), aware(config['custom_end'])
    elif mode == 'yesterday':
        start, end = midnight - timedelta(days=1), midnight
    elif mode == 'today':
        start, end = midnight, cutoff
    else:
        raise ValueError('付款范围无效')
    if start > end or (mode == 'custom' and start == end):
        raise ValueError('付款起止范围无效')
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

def strings(values, label, maximum):
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(label + '数量无效')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 256 or
           any(ord(c) < 32 for c in v) for v in values):
        raise ValueError(label + '必须为非空单行文本')
    # Preserve Unicode, case, stars, hyphens and spaces exactly.
    return list(dict.fromkeys(values))
