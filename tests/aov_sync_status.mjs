import assert from 'node:assert/strict';
import {aovCardDataLabel,aovSyncProgress,aovRefundSummary} from '../static/aov-monitor.js';
assert.equal(aovCardDataLabel({status:'INCOMPLETE',updated_at:'2026-10-14T00:00:00Z',windows:{'24h':{}},errors:['AUTH_REQUIRED']}),'更新未完成');
assert.equal(aovCardDataLabel({status:'INCOMPLETE',errors:['ORDER_FIELDS_UNVERIFIED']}),'字段待核验');
assert.equal(aovCardDataLabel({status:'INCOMPLETE',errors:['SOURCE_NOT_READ']}),'采集未完成');
assert.equal(aovCardDataLabel({status:'COMPLETE',alert_eligible:false}),'参考统计');
assert.match(aovSyncProgress({phase:'AUTH_REQUIRED'}),/登录已过期.*已暂停/);
assert.doesNotMatch(aovSyncProgress({phase:'AUTH_REQUIRED',collection_seconds:0}),/正在读取|已完成 0/);
assert.match(aovSyncProgress({phase:'ORDERS',completed:2,total:4,collection_seconds:125}),/2分5秒/);
console.log('sync status PASS');

assert.equal(aovCardDataLabel({status:'COMPLETE',alert_eligible:true,diagnostics:{refund_estimation:{estimated:true}}}),'退款估算');
assert.equal(aovCardDataLabel({status:'COMPLETE',alert_eligible:false,diagnostics:{refund_estimation:{estimated:true}}}),'估算待更新');
assert.match(aovRefundSummary({status:'UNAVAILABLE'}),/暂停异常和恢复推送/);
assert.match(aovRefundSummary({status:'AVAILABLE',reused:true,sample_shops:12,sample_orders:2071,order_pct:'0.00',amount_pct:'0.00',aov_impact_pct:'0.00'}),/未读取退款页面.*不代表全店实际退款率/);
console.log('refund estimate labels PASS');
