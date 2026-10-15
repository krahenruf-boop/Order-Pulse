const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=v=>JSON.parse(JSON.stringify(v));
const money=v=>v===null||v===undefined||v===''?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const when=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';

const legacyTemplateKeys=new Set(['failure','failure_template','error_template','failure_message_template']);
export function balanceSettingsDraft(settings){
 const clean=Object.fromEntries(Object.entries(settings).filter(([key])=>!legacyTemplateKeys.has(key)));
 clean.templates=Object.fromEntries(['title','account','alert','recovery'].map(key=>[key,settings.templates[key]]));
 return clone(clean);
}

export function createBalance({api,toast}){
 let state=null,draft=null,binding=null,root=null,dirty=false,poll=null,search='',filter='all',accountSaving=false;
 const dialog=()=>document.querySelector('#balance-settings');
 async function load(){state=await api('/balance/state');state.settings=balanceSettingsDraft(state.settings);if(!dirty)draft=balanceSettingsDraft(state.settings);render();return state;}
 function statusText(a){if(!a.enabled)return '已停用';if(a.data_status!=='valid')return ({stale:'已过期',missing:'本次未获取',failed:'采集失败'})[a.data_status]||'待确认';return a.balance_status==='low'?'低余额':'正常';}
 function rows(){return (state?.accounts||[]).filter(a=>{const text=`${a.shop_number||''} ${a.shop_name||''} ${a.display_name||''} ${a.source_name||''} ${a.asset_id}`.toLowerCase();return text.includes(search.toLowerCase())&&(filter==='all'||filter==='low'&&a.balance_status==='low'||filter==='enabled'&&a.enabled||filter==='disabled'&&!a.enabled);});}
 function render(){
  if(!root||!state)return;const accounts=state.accounts||[],enabled=accounts.filter(a=>a.enabled),low=enabled.filter(a=>a.balance_status==='low'),st=state.status||{};
  root.innerHTML=`<section class="page-heading"><div><p class="eyebrow">BALANCE MONITOR</p><h1>余额监控<span class="title-dot">.</span></h1><p class="subtitle">账户余额、采集状态与自动推送 · Asia/Shanghai</p></div><div class="actions"><button data-balance-action="query">立即查询</button><button data-balance-action="push" class="primary">查询并推送 ↗</button></div></section>
  <div class="balance-status-line"><span class="status ${st.data_status==='valid'?'success':st.data_status==='failed'?'error':'warning'}"><i></i>${st.busy?'正在采集':st.paused?'自动任务已暂停':'自动任务运行中'}</span><span>下一次执行 <strong>${st.paused?'未开启':when(st.next_send)}</strong></span><span>最近成功 <strong>${when(st.last_success)}</strong></span></div>
  ${st.last_error?`<div class="notice error">${esc(st.last_error)}；现有余额保留。</div>`:''}
  <section class="balance-metrics"><article class="card balance-metric"><span>已启用账户</span><strong>${enabled.length}</strong><small>共 ${accounts.length} 个账户</small></article><article class="card balance-metric"><span>低余额账户</span><strong>${low.length}</strong><small>现金与Grant分别判断</small></article><article class="card balance-metric"><span>最近成功采集</span><strong class="balance-time-metric">${when(st.last_success)}</strong><small>本机采集时间</small></article><article class="card balance-metric"><span>下一次自动任务</span><strong class="balance-time-metric">${st.paused?'已暂停':when(st.next_send)}</strong><small>Asia/Shanghai</small></article></section>
  <section class="section card balance-panel"><div class="matrix-heading"><div><h2>账户余额</h2><p>现金余额与Grant余额保持独立；来源过期时保留上次成功值。</p></div><div class="balance-tools"><input id="balance-search" placeholder="搜索账户或店铺" value="${esc(search)}"><select id="balance-filter"><option value="all" ${filter==='all'?'selected':''}>全部状态</option><option value="low" ${filter==='low'?'selected':''}>低余额</option><option value="enabled" ${filter==='enabled'?'selected':''}>已启用</option><option value="disabled" ${filter==='disabled'?'selected':''}>已停用</option></select></div></div>
  <div class="table-scroll"><table class="balance-table"><thead><tr><th>账户</th><th>现金余额</th><th>Grant余额</th><th>预警阈值</th><th>状态</th><th>来源更新时间</th><th>最近采集</th><th></th></tr></thead><tbody>${rows().map(a=>`<tr><td class="shop-name"><strong>${esc(a.display_name||a.source_name||a.asset_id)}</strong><small>${esc([a.shop_number,a.shop_name].filter(Boolean).join(' ')||a.asset_id)}</small></td><td class="number">${esc(a.currency||'BRL')} ${money(a.cash_balance)}</td><td class="number">${esc(a.currency||'BRL')} ${money(a.grant_balance)}</td><td>${a.threshold===null||a.threshold===''?'—':`${a.alert_metric==='grant_balance'?'Grant':'现金'} &lt; ${money(a.threshold)}`}</td><td><span class="module-badge ${a.balance_status==='low'?'error':a.data_status==='valid'&&a.enabled?'success':'muted'}">${esc(statusText(a))}</span></td><td>${when(a.source_updated_at)}</td><td>${when(a.collected_at)}</td><td><button data-balance-account="${esc(a.asset_id)}">配置</button></td></tr>`).join('')}</tbody></table></div><div class="table-footer"><span>${rows().length} / ${accounts.length} 个账户</span><span>按资产ID稳定关联</span></div></section>`;
  bindRoot();
 }
 function bindRoot(){
  root.querySelectorAll('[data-balance-action]').forEach(b=>b.onclick=()=>run(b.dataset.balanceAction));
  root.querySelectorAll('[data-balance-account]').forEach(b=>b.onclick=()=>openAccount(b.dataset.balanceAccount));
  root.querySelector('#balance-search').oninput=e=>{search=e.target.value;render();root.querySelector('#balance-search')?.focus();};
  root.querySelector('#balance-filter').onchange=e=>{filter=e.target.value;render();};
 }
 async function run(action){
  if(state.status.busy)return;try{const result=await api('/balance/action',{action});toast(result.message);await load();clearInterval(poll);poll=setInterval(async()=>{try{await load();if(!state.status.busy){clearInterval(poll);poll=null;toast(action==='push'?'余额查询与推送已完成':'余额查询已完成');}}catch{}},1000);}catch(e){toast(e.message);}
 }
 function ensureDialog(){if(dialog())return;document.body.insertAdjacentHTML('beforeend',`<dialog id="balance-settings" class="drawer balance-settings-drawer"><div class="drawer-title"><div><p class="eyebrow">BALANCE</p><h2>余额监控设置</h2></div><button data-balance-close class="icon-button">✕</button></div><form id="balance-settings-form"><section><h3>浏览器连接</h3><div class="balance-readonly"><span>浏览器空间</span><strong id="balance-space-name">检查中</strong><span>状态</span><strong id="balance-space-status">检查中</strong><span>Space ID</span><strong id="balance-space-id">—</strong><span>自动接管</span><strong id="balance-auto-takeover">—</strong><span>Linkme</span><strong id="balance-linkme-status">检查中</strong></div><div class="actions"><button type="button" data-balance-browser-check>检查浏览器</button><button type="button" data-balance-browser-rebind>重新绑定</button></div><p class="helper">仅使用 Order Pulse 专用Space；不安装扩展、不复制Cookie，也不使用其他Space。</p></section><hr><section><h3>自动采集</h3><label class="check"><input name="automatic" type="checkbox">启用余额自动任务</label><label>检测间隔（分钟）<input name="interval_minutes" type="number" min="1" max="1440"></label><label>来源余额最长有效期（分钟）<input name="max_age_minutes" type="number" min="1" max="1440"></label><div class="balance-readonly"><span>时区</span><strong>Asia/Shanghai</strong></div><label>每日推送时间（每行一个 HH:mm）<textarea name="schedules" rows="4"></textarea></label><label class="check"><input name="setup_complete" type="checkbox">首次配置已完成</label></section><hr><section><h3>通知与重试</h3><label>最多重试次数<input name="max_retries" type="number" min="0" max="5"></label><label>日志保留天数<input name="log_retention_days" type="number" min="1" max="365"></label><label>机器人关键词<input name="keyword" maxlength="200"></label><details><summary>余额飞书凭据</summary><label>Webhook<input name="webhook" type="password" autocomplete="off" placeholder="留空保留已保存地址"></label><label>签名密钥<input name="secret" type="password" autocomplete="off" placeholder="留空保留"></label></details></section><hr><section><h3>消息模板</h3><label>汇总标题<textarea name="template_title" rows="2"></textarea></label><label>单账户模板<textarea name="template_account" rows="6"></textarea></label><label>低余额模板<textarea name="template_alert" rows="6"></textarea></label><label>恢复模板<textarea name="template_recovery" rows="6"></textarea></label></section><p id="balance-settings-dirty" class="helper" hidden>未保存修改</p><p id="balance-settings-feedback" class="helper"></p><div class="drawer-bottom"><button type="button" data-balance-cancel>取消</button><button class="primary" type="submit">保存设置</button></div></form></dialog><dialog id="balance-account" class="confirm balance-account-modal" aria-labelledby="balance-account-title"><form id="balance-account-form" novalidate>
 <header class="balance-account-header"><div><p class="eyebrow">BALANCE ACCOUNT</p><h2 id="balance-account-title">配置余额账户</h2><p id="balance-account-name"></p><span id="balance-account-id"></span></div><button type="button" data-account-cancel class="icon-button" aria-label="关闭账户配置">✕</button></header>
 <div class="balance-account-body">
  <div id="balance-account-error" class="notice error" role="alert" hidden></div>
  <section class="balance-account-section"><h3>账户状态</h3><div class="balance-account-switches">
   <label class="balance-account-toggle"><span><strong>启用账户</strong><small>参与余额监控</small></span><input name="enabled" type="checkbox" role="switch"></label>
   <label class="balance-account-toggle"><span><strong>加入定时汇总</strong><small>在每日余额汇总中展示</small></span><input name="include_summary" type="checkbox" role="switch"></label>
   <label class="balance-account-toggle"><span><strong>开启低余额预警</strong><small>低于已设阈值时通知</small></span><input name="alert_enabled" type="checkbox" role="switch"></label>
   <label class="balance-account-toggle"><span><strong>发送恢复通知</strong><small>余额恢复正常时通知</small></span><input name="recovery_enabled" type="checkbox" role="switch"></label>
  </div></section>
  <section class="balance-account-section"><h3>账户信息</h3><div class="balance-account-grid">
   <label>店铺编号<input name="shop_number" maxlength="500" autocomplete="off"></label>
   <label>店铺名称<input name="shop_name" maxlength="500" autocomplete="off"></label>
   <label class="balance-account-wide">显示名称<input name="display_name" maxlength="500" autocomplete="off"></label>
  </div></section>
  <section class="balance-account-section"><h3>余额预警</h3><fieldset id="balance-account-alert-fields" class="balance-account-grid" aria-label="余额预警设置">
   <label>预警依据<select name="alert_metric"><option value="cash_balance">现金余额</option><option value="grant_balance">Grant余额</option></select></label>
   <label>预警阈值<input name="threshold" inputmode="decimal" autocomplete="off" aria-describedby="balance-threshold-help balance-threshold-error"><small id="balance-threshold-error" class="balance-field-error" role="status" hidden></small></label>
  </fieldset><p id="balance-threshold-help" class="helper">未开启预警时保留当前设置；阈值留空表示未配置。</p></section>
  <section class="balance-account-section balance-account-note"><h3>说明</h3><p class="helper">现金余额与 Grant 余额分别判断，预警以所选依据为准。</p></section>
 </div>
 <footer class="balance-account-footer"><button type="button" data-account-cancel>取消</button><button id="balance-account-save" class="primary" type="submit">保存账户</button></footer>
 </form></dialog>`);
  const d=dialog(),form=d.querySelector('form');form.oninput=()=>{dirty=true;d.querySelector('#balance-settings-dirty').hidden=false;};form.onsubmit=saveSettings;d.querySelector('[data-balance-close]').onclick=closeSettings;d.querySelector('[data-balance-cancel]').onclick=closeSettings;
  d.querySelector('[data-balance-browser-check]').onclick=checkBrowser;d.querySelector('[data-balance-browser-rebind]').onclick=rebindBrowser;document.querySelectorAll('[data-account-cancel]').forEach(b=>b.onclick=()=>{if(!accountSaving)document.querySelector('#balance-account').close();});const account=document.querySelector('#balance-account');account.oncancel=e=>{if(accountSaving)e.preventDefault();};const af=document.querySelector('#balance-account-form');af.onsubmit=saveAccount;af.alert_enabled.onchange=syncAlertFields;af.threshold.oninput=()=>{af.threshold.removeAttribute('aria-invalid');document.querySelector('#balance-threshold-error').hidden=true;};
 }
 function populate(){ensureDialog();const f=dialog().querySelector('form'),s=draft;f.automatic.checked=!s.paused;f.interval_minutes.value=s.interval_minutes;f.max_age_minutes.value=s.max_age_minutes;f.max_retries.value=s.max_retries;f.log_retention_days.value=s.log_retention_days;f.setup_complete.checked=s.setup_complete;f.keyword.value=s.keyword||'';f.schedules.value=s.schedules.filter(x=>x.enabled).map(x=>x.time).join('\n');for(const k of ['title','account','alert','recovery'])f['template_'+k].value=s.templates[k];f.webhook.value='';f.secret.value='';const b=binding?.binding;dialog().querySelector('#balance-space-name').textContent=b?.expected_name||'未绑定';dialog().querySelector('#balance-space-status').textContent=binding?.bound?'已绑定':'不可用';dialog().querySelector('#balance-space-id').textContent=b?`#…${String(b.space_id).slice(-2)}`:'—';dialog().querySelector('#balance-auto-takeover').textContent=b?.auto_takeover?'ON':'OFF';dialog().querySelector('#balance-linkme-status').textContent=state.connection?.browser_connected?'已登录':'待检查';dirty=false;f.querySelector('#balance-settings-dirty').hidden=true;f.querySelector('#balance-settings-feedback').textContent='';}
 async function openSettings(){const values=await Promise.all([load(),api('/browser-binding')]);binding=values[1];populate();dialog().showModal();}

 async function checkBrowser(){const d=dialog(),feedback=d.querySelector('#balance-settings-feedback');feedback.textContent='正在检查专用浏览器…';try{await api('/balance/action',{action:'connect'});binding=await api('/browser-binding');await load();populate();feedback.textContent='Linkme 登录和27账户范围已核验';toast('专用浏览器检查完成');}catch(err){feedback.textContent=err.message;}}
 async function rebindBrowser(){const d=dialog(),feedback=d.querySelector('#balance-settings-feedback');try{if(!binding?.binding)throw Error('尚无可恢复的专用Space绑定');binding=await api('/browser-binding',binding.binding);await checkBrowser();toast('专用浏览器已重新绑定');}catch(err){feedback.textContent=err.message;}}
 function closeSettings(){if(dirty&&!confirm('余额设置有未保存修改，是否放弃？'))return;dirty=false;dialog().close();}
 async function saveSettings(e){e.preventDefault();const f=e.target;try{const times=f.schedules.value.split(/\s+/).filter(Boolean);if(times.some(x=>!/^([01]\d|2[0-3]):[0-5]\d$/.test(x))||new Set(times).size!==times.length)throw Error('推送时间应为不重复的 HH:mm');const schedules=times.map((time,i)=>({id:draft.schedules.find(x=>x.time===time)?.id||`balance-${Date.now()}-${i}`,time,enabled:true}));const payload={...draft,paused:!f.automatic.checked,interval_minutes:Number(f.interval_minutes.value),max_age_minutes:Number(f.max_age_minutes.value),max_retries:Number(f.max_retries.value),log_retention_days:Number(f.log_retention_days.value),setup_complete:f.setup_complete.checked,keyword:f.keyword.value,schedules,templates:{title:f.template_title.value,account:f.template_account.value,alert:f.template_alert.value,recovery:f.template_recovery.value}};await api('/balance/settings',payload);if(f.webhook.value||f.secret.value)await api('/balance/credentials',{webhook:f.webhook.value,secret:f.secret.value});dirty=false;await load();populate();toast('余额监控设置已保存');}catch(err){f.querySelector('#balance-settings-feedback').textContent=err.message;}}
 function syncAlertFields(){const f=document.querySelector('#balance-account-form');document.querySelector('#balance-account-alert-fields').disabled=!f.alert_enabled.checked;}
 function accountBusy(value){
  accountSaving=value;const d=document.querySelector('#balance-account');d.querySelector('form').setAttribute('aria-busy',String(value));
  d.querySelectorAll('button').forEach(b=>b.disabled=value);d.querySelector('#balance-account-save').textContent=value?'保存中…':'保存账户';
 }
 function clearAccountErrors(){
  document.querySelector('#balance-account-error').hidden=true;document.querySelector('#balance-threshold-error').hidden=true;
  document.querySelector('#balance-account-form').threshold.removeAttribute('aria-invalid');
 }
 function openAccount(id){
  ensureDialog();const a=state.accounts.find(x=>String(x.asset_id)===String(id)),d=document.querySelector('#balance-account'),f=d.querySelector('form');
  d.dataset.id=id;d.querySelector('#balance-account-name').textContent=a.source_name||a.display_name||a.asset_id;d.querySelector('#balance-account-id').textContent=a.asset_id;
  for(const k of ['enabled','include_summary','alert_enabled','recovery_enabled'])f[k].checked=!!a[k];
  for(const k of ['shop_number','shop_name','display_name','alert_metric'])f[k].value=a[k]??'';
  f.threshold.value=a.threshold??'';clearAccountErrors();accountBusy(false);syncAlertFields();d.showModal();d.querySelector('.balance-account-body').scrollTop=0;
 }
 async function saveAccount(e){
  e.preventDefault();if(accountSaving)return;const d=document.querySelector('#balance-account'),f=e.target;clearAccountErrors();accountBusy(true);
  try{
   const patch={asset_id:d.dataset.id};for(const k of ['enabled','include_summary','alert_enabled','recovery_enabled'])patch[k]=f[k].checked;
   for(const k of ['shop_number','shop_name','display_name','alert_metric'])patch[k]=f[k].value.trim();patch.threshold=f.threshold.value.trim()||null;
   await api('/balance/accounts',{accounts:[patch]});d.close();await load();toast('余额账户已保存');
  }catch(err){
   const banner=d.querySelector('#balance-account-error');banner.textContent=err.message;banner.hidden=false;
   if(/额度|金额|threshold/.test(err.message)){const field=d.querySelector('#balance-threshold-error');field.textContent='预警阈值必须为有效金额';field.hidden=false;f.threshold.setAttribute('aria-invalid','true');f.threshold.focus();}
   else d.querySelector('.balance-account-body').scrollTop=0;
  }finally{accountBusy(false);}
 }
 return {mount(el){root=el;load().catch(e=>{root.innerHTML=`<div class="notice error">${esc(e.message)}</div>`;});},load,openSettings,dispose(){clearInterval(poll);poll=null;root=null;}};
}
