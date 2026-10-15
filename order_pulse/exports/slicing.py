import time
from datetime import timedelta
from .model import ExportError
from ..tms_core.domain import aware


class Budget:
    def __init__(self, settings, still_valid=lambda: True, clock=time.monotonic):
        self.settings, self.still_valid, self.clock = settings, still_valid, clock
        self.started = clock()
        self.queries = 0

    def check(self):
        if not self.still_valid(): raise ExportError('EXPORT_CANCELLED')
        if self.clock() - self.started >= self.settings.max_export_runtime:
            raise ExportError('EXPORT_RUNTIME_LIMIT')

    def query(self):
        self.check()
        self.queries += 1
        if self.queries > 2 * self.settings.max_export_slices - 1:
            raise ExportError('EXPORT_SLICE_LIMIT')


def _count(page):
    value=page.get('total')
    if type(value) is not int or value < 0: raise ExportError('EXPORT_COUNT_INVALID')
    return value


def _split(queue, batches, start, end, limit):
    midpoint = (start + (end - start) / 2).replace(second=0, microsecond=0)
    left_end = midpoint + timedelta(seconds=1)
    if not start < midpoint < left_end < end: raise ExportError('EXPORT_DENSE_MINUTE')
    if len(batches) + len(queue) + 2 > limit: raise ExportError('EXPORT_SLICE_LIMIT')
    queue[0:0] = [(start, left_end), (midpoint, end)]


def _receipt(source, window, action, count, **values):
    result=dict(partition=source,source=source,start=window['start'],end=window['end'],
                slice_start=window['start'],slice_end=window['end'],total=count,action=action)
    result.update(values)
    return result


class CurrentOrdersExportAdapter:
    name = 'current'

    def __init__(self, transport, settings):
        self.transport, self.settings = transport, settings

    def collect(self, binding, scope, budget):
        queue = [(aware(scope['start']), aware(scope['end']))]
        batches, receipts, local_retries = [], [], {}
        reconciliation=scope.get('reconciliation_contract')=='CURRENT_FIRST_V1'
        while queue:
            budget.query()
            start, end = queue.pop(0)
            window = dict(scope, start=start.isoformat(), end=end.isoformat())
            page = self.transport.query(self.name, binding, window, 50)
            count = _count(page)
            if count > self.settings.max_current_export_rows:
                _split(queue,batches,start,end,self.settings.max_export_slices)
                receipts.append(_receipt(self.name,window,'SPLIT',count))
                continue
            if not count:
                receipts.append(_receipt(self.name,window,'EMPTY',0,before_count=0,after_count=0,
                                         expected_count=0,exported_count=0,drift_delta=0,
                                         reconciliation_result='STABLE'))
                continue

            if not reconciliation:
                # Filtered export contract: exact count and exact selected IDs.
                for attempt in (1, 2):
                    budget.check();full = self.transport.query(self.name, binding, window, 500)
                    stable = full['total'] == count and full['loaded'] == count and full['page'] == 1
                    if stable: break
                    if attempt == 1:
                        page = self.transport.query(self.name, binding, window, 50);count = _count(page)
                        if count > self.settings.max_current_export_rows:
                            queue.insert(0, (start, end));break
                        continue
                    code = 'EXPORT_SOURCE_CHANGED' if full['total'] != count else 'EXPORT_INCOMPLETE'
                    error=ExportError(code);error.details=dict(stage='page_load',partition=self.name,
                        expected_total=count,loaded=full['loaded'],page=full['page']);raise error
                if count > self.settings.max_current_export_rows: continue
                batch=self.transport.export(self.name,binding,window,full);budget.check();batches.append(batch)
                receipts.append(_receipt(self.name,window,'EXPORTED',count))
                continue

            # Product Ranking freezes the payment window, but allows the live
            # order state to move while retaining complete selected-ID checks.
            budget.check();full=self.transport.query(self.name,binding,window,500)
            full_count=_count(full)
            if full_count > self.settings.max_current_export_rows:
                _split(queue,batches,start,end,self.settings.max_export_slices)
                receipts.append(_receipt(self.name,window,'SPLIT_AFTER_DRIFT',full_count,
                                         before_count=count,after_count=full_count,
                                         drift_delta=full_count-count,reconciliation_result='RECONCILIATION_REQUIRED'))
                continue
            if full.get('page')!=1 or full.get('loaded')!=full_count or len(full.get('order_ids',()))!=full_count:
                error=ExportError('EXPORT_INCOMPLETE');error.details=dict(stage='page_load',source=self.name,
                    slice_start=window['start'],slice_end=window['end'],expected_count=full_count,
                    exported_count=full.get('loaded'));raise error
            full['before_count']=count
            key=(window['start'],window['end'])
            try:
                batch=self.transport.export(self.name,binding,window,full)
            except ExportError as error:
                details=getattr(error,'details',None) or {}
                if (str(error)=='EXPORT_SOURCE_CHANGED' and details.get('stage') in {'export-precheck','export-request'} and
                        not local_retries.get(key)):
                    local_retries[key]=1;queue.insert(0,(start,end))
                    receipts.append(_receipt(self.name,window,'RETRY_SOURCE_CHANGED',count,
                                             before_count=count,after_count=details.get('actual_total'),
                                             drift_delta=(details.get('actual_total')-count if type(details.get('actual_total')) is int else None),
                                             reconciliation_result='RECONCILIATION_REQUIRED'))
                    continue
                raise
            budget.check()
            after=self.transport.query(self.name,binding,window,500);after_count=_count(after)
            if (after_count>self.settings.max_current_export_rows or after.get('page')!=1 or
                    after.get('loaded')!=after_count or len(after.get('order_ids',()))!=after_count):
                error=ExportError('EXPORT_UNEXPLAINED_GAP');error.details=dict(stage='post_export_reconciliation',
                    source=self.name,slice_start=window['start'],slice_end=window['end'],
                    expected_count=batch['total'],before_count=count,after_count=after_count);raise error
            exported_ids=set(batch.get('expected_order_ids') or ())
            candidates=(set(full['order_ids'])|set(after['order_ids']))-exported_ids
            drift=bool(count!=full_count or batch.get('reconciliation_required') or
                       after_count!=batch['total'] or candidates)
            batch.update(before_count=count,after_count=after_count,
                         reconciliation_candidate_ids=sorted(candidates),
                         reconciliation_result='RECONCILIATION_REQUIRED' if drift else 'STABLE')
            batches.append(batch)
            receipts.append(_receipt(self.name,window,'EXPORTED',batch['total'],expected_count=batch['total'],
                                     exported_count=batch['total'],before_count=count,after_count=after_count,
                                     drift_delta=after_count-count,reconciliation_result=batch['reconciliation_result']))
        return batches, receipts


class CompletedOrdersExportAdapter:
    name = 'completed'

    def __init__(self, transport, settings):
        self.transport, self.settings = transport, settings

    def collect(self, binding, scope, budget):
        queue = [(aware(scope['start']), aware(scope['end']))]
        batches, receipts, observed_rows, local_retries = [], [], 0, {}
        reconciliation=scope.get('reconciliation_contract')=='CURRENT_FIRST_V1'
        while queue:
            budget.query()
            start, end = queue.pop(0)
            window = dict(scope, start=start.isoformat(), end=end.isoformat())
            page = self.transport.query(self.name, binding, window, 50)
            count = _count(page)
            if count > self.settings.max_completed_export_rows:
                _split(queue,batches,start,end,self.settings.max_export_slices)
                receipts.append(_receipt(self.name,window,'SPLIT',count))
                continue
            if not count:
                receipts.append(_receipt(self.name,window,'EMPTY',0,before_count=0,after_count=0,
                                         expected_count=0,exported_count=0,drift_delta=0,
                                         reconciliation_result='STABLE'))
                continue
            key=(window['start'],window['end'])
            try:
                batch=self.transport.export(self.name,binding,window,page)
            except ExportError as error:
                details=getattr(error,'details',None) or {}
                if (reconciliation and str(error)=='EXPORT_SOURCE_CHANGED' and
                        details.get('stage') in {'export-precheck','export-request'} and not local_retries.get(key)):
                    local_retries[key]=1;queue.insert(0,(start,end))
                    receipts.append(_receipt(self.name,window,'RETRY_SOURCE_CHANGED',count,
                                             before_count=count,after_count=details.get('actual_total'),
                                             drift_delta=(details.get('actual_total')-count if type(details.get('actual_total')) is int else None),
                                             reconciliation_result='RECONCILIATION_REQUIRED'))
                    continue
                raise
            budget.check()
            if reconciliation:
                after=self.transport.query(self.name,binding,window,50);after_count=_count(after)
                batch['acceptable_order_counts']=sorted({batch['total'],after_count})
                drift=bool(count!=batch['total'] or after_count!=batch['total'] or batch.get('reconciliation_required'))
                batch.update(before_count=count,after_count=after_count,reconciliation_candidate_ids=[],
                             reconciliation_result='RECONCILIATION_REQUIRED' if drift else 'STABLE')
                receipts.append(_receipt(self.name,window,'EXPORTED',batch['total'],expected_count=batch['total'],
                                         exported_count=batch['total'],before_count=count,after_count=after_count,
                                         drift_delta=after_count-count,reconciliation_result=batch['reconciliation_result']))
            else:
                receipts.append(_receipt(self.name,window,'EXPORTED',count))
            observed_rows += batch['total']
            if observed_rows > self.settings.max_rows: raise ExportError('EXPORT_ROW_LIMIT')
            batches.append(batch)
        return batches, receipts
