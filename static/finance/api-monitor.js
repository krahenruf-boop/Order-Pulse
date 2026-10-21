const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const num=v=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
export function createApiMonitor({api,toast}){
 let root,state,poll;
 async function load(){state=await api('/api-monitor/state');render();if(state.providers.some(x=>x.busy)){clearTimeout(poll);poll=setTimeout(()=>load().catch(e=>toast(e.message)),2000)}return state}
 function render(){if(!root||!state)return;root.innerHTML=`<section class="page-heading"><div><p class="eyebrow">API MONITOR</p><h1>API 额度监控<span class="title-dot">.</span></h1><p class="subtitle">正常时每日检测，低额度时按设定间隔复查 · 北京时间</p></div><button id="api-test">发送测试消息</button></section><div class="quota-grid">${state.providers.map(c=>`<article class="card quota-card"><div class="matrix-heading"><h2>${esc(c.name)}</h2><button data-settings="${c.id}">设置</button></div><p class="quota-account">${esc(c.last?.identity||'首次读取后绑定账户／工作区')}</p><span class="status ${c.fault?'error':c.ratio!=null&&Number(c.ratio)<=Number(c.threshold)?'warning':'success'}"><i></i>${c.busy?'正在读取':c.fault?'读取异常':!c.enabled?'监控已关闭':!c.last?'等待首次读取':c.ratio==null?'待设置正余额基准':Number(c.ratio)<=Number(c.threshold)?'低额度':'正常'}</span><div class="quota-value">${num(c.last?.remaining)} <small>${esc(c.last?.unit||'')}</small></div><p>剩余比例 <strong>${c.ratio==null?'—':num(c.ratio)+'%'}</strong> <span class="helper">· 阈值 ≤ ${esc(c.threshold)}%</span></p>${c.id==='deepseek'?`<p class="helper">基准 ${num(c.baseline)} ${esc(c.last?.unit||'')} · ${when(c.baseline_at)}</p>`:`<p class="helper">已用 ${num(c.last?.used)} / 总计 ${num(c.last?.total)} 积分</p><p class="helper">${esc(c.last?.reset_label||'续订日期待核验')}</p>`}${c.fault?`<div class="notice error">${esc(c.fault.error)}；已有额度保留。</div>`:''}<p class="helper quota-cadence">${!c.enabled?'自动监控已关闭':c.schedule_mode==='low_recheck'?`低额度复查 · 每 ${num(c.low_interval_hours)} 小时`:`每日检测 · ${esc(c.time)}（北京时间）`}</p><dl class="quota-dates"><dt>最近读取</dt><dd>${when(c.last?.at)}</dd><dt>下次检测</dt><dd>${c.enabled?when(c.next_at):'已关闭'}</dd></dl><div class="actions"><button data-refresh="${c.id}" ${c.busy?'disabled':''}>${c.busy?'读取中…':'手动刷新'}</button><button data-history="${c.id}">历史</button></div></article>`).join('')}</div><p class="helper">仅读取额度页面，不调用模型或语音生成。登录失效即时通知，持续故障每 6 小时提醒；超过 26 小时未成功更新也会报警。</p>`;
 root.querySelectorAll('[data-settings]').forEach(b=>b.onclick=()=>openSettings(b.dataset.settings));root.querySelectorAll('[data-refresh]').forEach(b=>b.onclick=async()=>{try{await api('/api-monitor/check/'+b.dataset.refresh,{});await load()}catch(e){toast(e.message)}});root.querySelectorAll('[data-history]').forEach(b=>b.onclick=()=>history(b.dataset.history));root.querySelector('#api-test').onclick=async e=>{e.currentTarget.disabled=true;try{const r=await api('/api-monitor/test',{});toast(r.status==='success'?'测试消息已发送':'发送未确认，请查看通知记录')}catch(e){toast(e.message)}finally{e.currentTarget.disabled=false}};
 }
 function dialog(id,title,body,description=''){document.getElementById(id)?.remove();document.body.insertAdjacentHTML('beforeend',`<dialog id="${id}" class="confirm quota-dialog" aria-labelledby="${id}-title" ${description?`aria-describedby="${id}-description"`:''}><div class="drawer-title"><div>${description?'<span class="quota-dialog-eyebrow">API MONITOR</span>':''}<h2 id="${id}-title">${esc(title)}</h2>${description?`<p id="${id}-description">${esc(description)}</p>`:''}</div><button data-close aria-label="关闭">✕</button></div>${body}</dialog>`);const d=document.getElementById(id);d.querySelector('[data-close]').onclick=()=>d.close();d.showModal();return d}
 async function openSettings(id){
  if(!state)await load();
  const selected=id||state.providers[0].id;
  const c=state.providers.find(x=>x.id===selected);
  const d=dialog('api-settings',c.name+' 监控设置',`
   <form>
    <div class="quota-settings-body">
     <section class="quota-settings-section" aria-labelledby="quota-switches-title">
      <h3 id="quota-switches-title">监控与通知</h3>
      <div class="quota-switches">
       <label class="quota-toggle"><span><strong>启用监控</strong><small>按设定时间自动读取账户额度</small></span><input name="enabled" type="checkbox" role="switch" aria-label="启用监控" ${c.enabled?'checked':''}></label>
       <label class="quota-toggle"><span><strong>飞书推送</strong><small>接收低额度、读取故障与恢复通知</small></span><input name="push" type="checkbox" role="switch" aria-label="飞书推送" ${c.push?'checked':''}></label>
      </div>
     </section>
     <section class="quota-settings-section" aria-labelledby="quota-rules-title">
      <h3 id="quota-rules-title">检测规则</h3>
      <div class="quota-fields">
       <label class="quota-field"><span>每日检测时间 <small>北京时间</small></span><input name="time" type="time" required value="${esc(c.time)}"></label>
       <label class="quota-field"><span>剩余比例预警阈值</span><span class="quota-input-unit"><input name="threshold" type="number" min="0" max="100" step="0.01" required value="${esc(c.threshold)}" aria-describedby="quota-threshold-help"><span aria-hidden="true">%</span></span></label>
      </div>
      <p class="quota-field-help" id="quota-threshold-help">剩余比例 ≤ 设定阈值时提醒，持续低额度不重复推送。</p>
      <div class="quota-recheck-rule"><label class="quota-field"><span>低额度复查间隔</span><span class="quota-input-unit"><input name="low_interval_hours" type="number" min="1" max="24" step="1" required value="${esc(c.low_interval_hours??1)}" aria-describedby="quota-recheck-help"><span aria-hidden="true">小时</span></span></label><p class="quota-field-help" id="quota-recheck-help">低额度时按此间隔复查，恢复后回到每日检测，并通知一次。</p></div>
     </section>
     ${c.id==='deepseek'?`
      <section class="quota-settings-section quota-baseline" aria-labelledby="quota-baseline-title">
       <h3 id="quota-baseline-title">余额基准 <span>100% 起始值</span></h3>
       <label class="quota-field"><span class="quota-input-unit"><input name="baseline" type="number" min="0.000001" step="any" aria-label="基准余额" aria-describedby="quota-baseline-help" value="${esc(c.baseline||'')}" placeholder="首次正余额自动建立"><span aria-hidden="true">${esc(c.last?.unit||'')}</span></span></label>
       <p class="quota-field-help" id="quota-baseline-help">用于计算剩余比例，余额增加时不会自动重设。</p>
       <div class="quota-baseline-meta"><span>设定于 ${when(c.baseline_at)}</span><button type="button" id="reset-baseline" ${!c.last||Number(c.last.remaining)<=0?'disabled':''}>使用最近余额重设 ↻</button></div>
      </section>`:''}
     <details class="quota-advanced"><summary>账户与工作区绑定</summary><div><p class="quota-field-help">仅在账户或工作区更换时重新绑定，历史记录保留。</p><button type="button" id="api-rebind">重新读取并绑定</button></div></details>
    </div>
    <div class="quota-settings-footer"><p class="notice error" id="api-feedback" role="alert" hidden></p><div class="actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">保存设置</button></div></div>
   </form>`, '管理每日检测与低额度提醒');
  const form=d.querySelector('form');
  d.querySelector('[data-cancel]').onclick=()=>d.close();
  async function save(value){
   const feedback=d.querySelector('#api-feedback');
   const buttons=[...form.querySelectorAll('button')];
   const disabled=buttons.map(b=>b.disabled);
   const submit=form.querySelector('[type=submit]');
   feedback.hidden=true;
   form.setAttribute('aria-busy','true');
   buttons.forEach(b=>b.disabled=true);
   submit.textContent='保存中…';
   try{await api('/api-monitor/settings/'+c.id,value);d.close();await load();toast('API 监控设置已保存')}
   catch(e){feedback.hidden=false;feedback.textContent=e.message}
   finally{buttons.forEach((b,i)=>b.disabled=disabled[i]);submit.textContent='保存设置';form.removeAttribute('aria-busy')}
  }
  form.onsubmit=e=>{e.preventDefault();if(form.getAttribute('aria-busy')==='true')return;const value={enabled:form.enabled.checked,push:form.push.checked,time:form.time.value,threshold:form.threshold.value,low_interval_hours:Number(form.low_interval_hours.value)};if(form.baseline?.value&&form.baseline.value!==c.baseline)value.baseline=form.baseline.value;save(value)};
  d.querySelector('#reset-baseline')?.addEventListener('click',()=>save({reset_baseline:true}));
  d.querySelector('#api-rebind').onclick=()=>save({rebind:true});
 }
 async function history(id){try{const h=await api('/api-monitor/history/'+id);dialog('api-history','检测与通知记录',`<div class="quota-history">${h.readings.length?`<table><thead><tr><th>时间</th><th>额度／状态</th></tr></thead><tbody>${h.readings.map(r=>`<tr><td>${when(r.at)}</td><td>${r.ok?`${num(r.reading.remaining)} ${esc(r.reading.unit)}`:esc(r.error)}</td></tr>`).join('')}</tbody></table>`:'暂无记录'}<h3>推送记录</h3>${h.notifications.map(n=>`<p>${when(n.at)} · ${esc(({low:'低额度预警',recovery:'额度恢复',fault:'读取故障',reminder:'持续故障提醒',fault_recovery:'采集恢复',test:'测试'})[n.kind]||n.kind)} · ${esc(({success:'已发送',failed:'发送失败',pending:'待发送',sending:'发送中',uncertain:'发送结果待确认',cancelled:'已取消'})[n.status]||n.status)}</p>`).join('')||'暂无推送'}</div>`)}catch(e){toast(e.message)}}
 return {mount(el){root=el;load().catch(e=>toast(e.message))},load,openSettings};
}
