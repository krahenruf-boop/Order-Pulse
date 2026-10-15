"""Detection-cycle scope; prior comparison reads certified local receipts only."""
from .domain import digest

CYCLE_HOURS=(1,2,4,6,8,12,24)

def cycle_hours(config):
    hours=config.get('cycle_hours',2)
    if type(hours) is not int or hours not in CYCLE_HOURS:raise ValueError('检测周期请选择1、2、4、6、8、12或24小时')
    return hours

def is_cycle(config):return config.get('collection_mode')=='CYCLE'

def order_identity(shop):
    return digest({k:shop.get(k) for k in ('id','tms_shop_id','miaoshou_shop_id','source_mode','sku_aliases')})
