import {createBalance,balanceSettingsDraft} from '../static/finance/balance.js';

const clone = value => JSON.parse(JSON.stringify(value));
const active = ['title','account','alert','recovery'];
const legacy = ['failure','failure_template','error_template','failure_message_template'];
const settings = {
 paused:true, interval_minutes:30, max_age_minutes:30, max_retries:2, log_retention_days:30,
 setup_complete:true, keyword:'余额', schedules:[{id:'morning',time:'09:30',enabled:true}],
 templates:{title:'余额汇总',account:'{name}',alert:'低余额 {name}',recovery:'余额恢复 {name}'},
};
for(const key of legacy){settings[key]='legacy error';settings.templates[key]='TikTok OPS 数据异常 {error} {event_id}';}
const account={asset_id:'test-001', source_name:'ASSOCIACAO DA GINASTICA DE TRAMPOLIM DE CONTAGEM — 超长账户名称验收',
 enabled:true,include_summary:true,alert_enabled:false,recovery_enabled:true,shop_number:'18',shop_name:'测试店铺',
 display_name:'中文显示名称',alert_metric:'grant_balance',threshold:'150.25',cash_balance:'100.00',grant_balance:'200.00',
 currency:'BRL',data_status:'valid',balance_status:'normal'};
const state={settings,accounts:[account],status:{paused:true,busy:false},connection:{browser_connected:true}};
const requests=[];let mode='success', release;
async function api(path,payload){
 requests.push({path,payload:clone(payload??null)});
 if(path==='/balance/state')return clone(state);
 if(path==='/browser-binding')return {bound:true,binding:{space_id:'fixture',expected_name:'测试专用Space',auto_takeover:true}};
 if(path==='/balance/settings'){state.settings={...state.settings,...clone(payload)};return {};}
 if(path==='/balance/accounts'){
  if(mode==='pending')await new Promise(resolve=>release=resolve);
  if(mode==='error')throw Error('保存失败，请稍后重试');
  if(mode==='invalid')throw Error('额度应为非负金额，最多两位小数；空白表示未配置');
  state.accounts[0]={...state.accounts[0],...clone(payload.accounts[0])};return {};
 }
 throw Error('禁止外部操作：'+path);
}
const app=createBalance({api,toast:()=>{}});
app.mount(document.querySelector('#balance-root'));
const q=s=>document.querySelector(s), form=()=>q('#balance-account-form');
const settle=()=>new Promise(resolve=>setTimeout(resolve,0));
const assert=(condition,message)=>{if(!condition)throw Error(message);};
const open=()=>q('[data-balance-account]').click();
const fieldKeys=['enabled','include_summary','alert_enabled','recovery_enabled','shop_number','shop_name','display_name','alert_metric','threshold'];
document.querySelector('#settings').onclick=()=>app.openSettings();
document.querySelector('#modal-over-drawer').onclick=async()=>{await app.openSettings();open();};
document.querySelector('#run-tests').onclick=async()=>{
 const results=[]; const pass=name=>results.push('PASS '+name);
 try{
  const draft=balanceSettingsDraft(settings);
  assert(legacy.every(key=>!(key in draft)&&!(key in draft.templates)),'legacy in draft');
  assert(active.every(key=>key in draft.templates),'active template missing');pass('前端草稿排除所有旧失败模板');
  await app.openSettings();
  const sf=q('#balance-settings-form');
  assert(active.every(key=>sf['template_'+key]),'active input missing');
  assert(!/失败模板|TikTok OPS 数据异常|\{error\}|\{event_id\}/.test(q('#balance-settings').textContent),'legacy visible');
  assert(!q('[name="template_failure"]'),'legacy input');pass('设置只显示四种模板，旧配置正常加载');
  sf.requestSubmit();await settle();
  const payload=requests.find(r=>r.path==='/balance/settings').payload;
  assert(legacy.every(key=>!(key in payload)&&!(key in payload.templates)),'legacy payload');
  assert(JSON.stringify(Object.keys(payload.templates))===JSON.stringify(active),'template keys');pass('保存payload只有四种有效模板');
  q('[data-balance-cancel]').click();open();
  for(const key of fieldKeys)assert(form()[key] && (typeof account[key]==='boolean'?form()[key].checked:form()[key].value)===account[key],key+' backfill');
  pass('全部九个业务字段原值正确回填');
  assert(form().threshold.matches(':disabled')&&form().alert_metric.matches(':disabled'),'not disabled');pass('预警OFF禁用两字段且保留值');
  form().alert_enabled.click();assert(!form().threshold.matches(':disabled')&&!form().alert_metric.matches(':disabled'),'not enabled');
  form().alert_enabled.click();assert(form().threshold.value==='150.25'&&form().alert_metric.value==='grant_balance','value lost');pass('预警ON可编辑，来回切换保留原值');
  form().display_name.value='取消草稿';q('#balance-account [data-account-cancel]').click();
  assert(!requests.some(r=>r.path==='/balance/accounts'),'cancel saved');open();
  assert(form().display_name.value===account.display_name,'cancel mutated');pass('取消不保存');
  mode='pending';form().requestSubmit();form().requestSubmit();
  assert(q('#balance-account-save').disabled&&q('#balance-account-save').textContent==='保存中…','busy missing');
  assert(requests.filter(r=>r.path==='/balance/accounts').length===1,'duplicate save');pass('保存中状态阻止重复提交');
  release();await settle();
  assert(!q('#balance-account').open,'success not closed');
  const saved=requests.find(r=>r.path==='/balance/accounts').payload.accounts[0];
  for(const key of fieldKeys)assert(saved[key]===account[key],key+' changed');pass('成功保存保留全部字段与禁用阈值');
  open();mode='error';form().display_name.value='保留输入';form().requestSubmit();await settle();
  assert(q('#balance-account').open&&!q('#balance-account-error').hidden&&form().display_name.value==='保留输入','failed draft lost');
  assert(!q('#balance-account-save').disabled,'save stuck');pass('保存失败保留draft并显示轻量错误');
  form().alert_enabled.click();form().threshold.value='1.234';mode='invalid';form().requestSubmit();await settle();
  assert(!q('#balance-threshold-error').hidden&&form().threshold.getAttribute('aria-invalid')==='true','no inline error');
  assert(form().threshold.value==='1.234','invalid value lost');pass('金额校验错误显示在字段下方');
  q('#balance-account [data-account-cancel]').click();mode='success';
  assert(requests.every(r=>['/balance/state','/browser-binding','/balance/settings','/balance/accounts'].includes(r.path)),'external request');
  pass('未触发采集、浏览器检查或飞书');
  q('#results').textContent=results.join('\n')+'\n12 / 12 PASS';
 }catch(error){q('#results').textContent=results.join('\n')+'\nFAIL '+error.message;}
};
