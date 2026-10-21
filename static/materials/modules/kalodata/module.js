import {PERIODS, ACTIVE, STATE_LABELS, esc, safeHref, validateInput, cooldownSeconds, startAllowed, coverageText, sortText} from './view-model.mjs';
const PREFIX = '/api/materials/kalodata/v1';
// This state belongs only to Kalodata and is never written to balance settings.
const model = {form:{product_url:'',period:'30D',start_page:'1',end_page:'1'},capabilities:null,connection:null,run:null,items:[],runs:[],busy:false,error:null,errors:{},onlyNew:false,exportScope:'all',copyFallback:'',selectedRun:'',readRunId:''};
const errorLabels = {BROWSER_BINDING_REQUIRED:'素材空间绑定已变化，请到设置中重新核验',BROWSER_BUSY:'空间正在执行另一项采集，请稍后重试',BROWSER_CONTROL_UNAVAILABLE:'无法接管素材与客单价空间',PLUGIN_RESPONSE_TIMEOUT:'插件页面读取超时，请重试',PLUGIN_OFFLINE:'素材采集插件未连接',PLUGIN_RELOAD_REQUIRED:'采集插件已更新，需要重新加载一次浏览器扩展',PLUGIN_VERSION_MISMATCH:'插件版本或日期适配器不兼容',KALODATA_LOGIN_REQUIRED:'需要登录Kalodata',LOGIN_REQUIRED:'需要登录Kalodata',KALODATA_BUSY:'已有素材任务正在运行',MODULE_DISABLED:'素材模块未启用',SESSION_MISMATCH:'插件会话已改变',PERMISSION_DENIED:'当前账号未获页面读取权限',DETAIL_QUOTA_EXHAUSTED:'今日详情查看额度已用完',SOURCE_LOAD_FAILED:'Kalodata详情页加载失败',SOURCE_LIST_NOT_READY:'Kalodata素材列表未就绪',PAGINATION_SCHEMA_CHANGED:'Kalodata页码信息无法读取',VIDEO_ITEM_SCHEMA_CHANGED:'Kalodata视频条目结构无法识别',PRODUCT_NOT_FOUND:'未找到此商品',EXTRACTION_FAILED:'素材提取失败',DATE_CONTROL_NOT_FOUND:'未找到日期控件',SOURCE_REFRESH_TIMEOUT:'页面数据加载超时',RATE_LIMITED:'网站限制请求频率',INVALID_PRODUCT_URL:'商品网址无效',INVALID_PAGE_RANGE:'页码范围无效',RANGE_EXCEEDS_VISIBLE:'超出当前页面可见页数',PERIOD_UNAVAILABLE:'此统计时间不可用',DATE_CONTEXT_UNVERIFIED:'网站日期未确认（仅提示）',CONTEXT_CHANGED:'商品、市场或列表内容已变化',PAGE_MISMATCH:'网站页码与请求不符',SCHEMA_CHANGED:'页面数据结构发生变化',UPSTREAM_BUSINESS_ERROR:'网站返回业务失败',LINK_NOT_FOUND:'未找到原链接',LINK_ID_MISMATCH:'外链视频 ID 不一致',NETWORK_ERROR:'网络连接失败',TASK_BUDGET_EXCEEDED:'本次读取超时',STORAGE_ERROR:'素材本地保存失败',CANCELLED:'已取消'};
function errorName(code) { return errorLabels[code] || code || '尚未取得原链接'; }
function dateTime(value) { if (!value) return '未提供'; const d=new Date(value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('zh-CN',{hour12:false}); }
function linkHTML(value,kind,id,label) { const href=safeHref(value,kind,id);return href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label || value)}</a>`:esc(label ? '来源地址不可用' : '尚未取得原链接'); }
function decodeError(data,status) { const source=data?.error || data?.detail || {};const error=new Error(typeof source==='string'?source:source.message || `本机请求失败（HTTP ${status}）`);error.code=source.code || 'LOCAL_REQUEST_FAILED';return error; }
function newRequestId() { return crypto.randomUUID(); }
export function mountKalodata(root,{csrf,toast}) {
  let alive=true, pollTimer=null, tickTimer=null, readGeneration=0;
  if(!document.querySelector('link[data-kalodata-style]')) {const style=document.createElement('link');style.rel='stylesheet';style.href='/static/modules/kalodata/style.css';style.dataset.kalodataStyle='true';document.head.append(style);}
  root.className='kalodata-module';
  async function api(path,body) {
    const response=await fetch(PREFIX+path,{credentials:'same-origin',cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)})});
    let data;try{data=await response.json();}catch{throw new Error(`本机服务返回了无法读取的响应（HTTP ${response.status}）`);}
    if(!response.ok) throw decodeError(data,response.status);return data;
  }
  async function loadConnection(){try{model.connection=await api('/connection');}catch{model.connection=null;}}
  async function loadDefaults(){
    try{
      const response=await fetch('/api/kalodata/settings',{credentials:'same-origin',cache:'no-store'});
      if(!response.ok)return;const value=await response.json();
      model.form.period=value.default_period==='YESTERDAY'?'1D':value.default_period || model.form.period;
      model.form.start_page=String(value.default_start_page || model.form.start_page);
      model.form.end_page=String(value.default_end_page || model.form.end_page);
    }catch{}
  }
  const report=error=>{model.error={code:error.code || 'LOCAL_REQUEST_FAILED',message:error.message};render();};
  function capabilityHTML() {
    const c=model.capabilities,remaining=cooldownSeconds(c),conn=model.connection,reason=c?.blocked_reason;
    const connected=conn?.plugin_connected && conn?.state==='CONNECTED';
    const reloadRequired=reason?.code==='PLUGIN_RELOAD_REQUIRED'||conn?.state==='PLUGIN_RELOAD_REQUIRED';
    return `<div class="kd-header"><span class="status ${connected?'success':'warning'}"><i></i>${!conn?'正在检查采集服务':reloadRequired?'插件需要重新加载':conn.kalodata_logged_in===false?'需要登录Kalodata':connected?'插件已连接':'插件未连接'}</span><button type="button" data-kd="refresh-capabilities" ${model.busy||ACTIVE.has(model.run?.state)?'disabled':''}>重新检查</button></div>${c && !c.transport_ready?`<div class="kd-notice kd-error"><strong>${esc(errorName(reason?.code))}</strong>${reloadRequired?'<ol><li>打开 ego lite 当前采集浏览器</li><li>进入 chrome://extensions</li><li>找到「TikTok OPS 素材采集」并点击重新加载</li><li>刷新 Kalodata 商品页</li><li>回到这里点击“重新检查”</li></ol>':esc(reason?.message || '请确认本地插件正在运行。')}</div>`:''}<div id="kd-cooldown" class="kd-notice" ${remaining?'':'hidden'}><strong>网站请求冷却中</strong><span id="kd-cooldown-text">${remaining} 秒</span></div>`;
  }
  function fieldError(name) {return `<span class="kd-field-error" id="kd-${name}-error">${esc(model.errors[name] || '')}</span>`;}
  function formHTML() {
    const running=ACTIVE.has(model.run?.state), disabled=model.busy || running, c=model.capabilities;
    const knownUnavailable=c?.unavailable_periods || {};
    return `<form id="kd-form" novalidate><div class="kd-field"><label for="kd-product_url">Kalodata 商品网址</label><input id="kd-product_url" name="product_url" type="url" value="${esc(model.form.product_url)}" placeholder="https://www.kalodata.com/product/detail?id=…" autocomplete="off" spellcheck="false" aria-describedby="kd-url-help kd-product_url-error" aria-invalid="${Boolean(model.errors.product_url)}" ${disabled?'disabled':''}><span class="kd-subtle" id="kd-url-help">只读您指定的一个商品；网址中的旧日期不作为统计日期。</span>${fieldError('product_url')}</div><fieldset ${disabled?'disabled':''}><legend>统计时间</legend><div class="kd-periods">${PERIODS.map(([value,label])=>`<label class="kd-period" title="${esc(knownUnavailable[value] || label)}"><input name="period" type="radio" value="${value}" ${model.form.period===value?'checked':''} ${knownUnavailable[value]?'disabled':''}><span>${label}${knownUnavailable[value]?' · 不可用':''}</span></label>`).join('')}</div><p class="kd-subtle">日期范围仅供参考，结果以 Kalodata 当前页面数据为准。</p>${Object.entries(knownUnavailable).map(([period,why])=>`<p class="kd-subtle">${esc(PERIODS.find(x=>x[0]===period)?.[1] || period)}：${esc(why)}</p>`).join('')}${fieldError('period')}</fieldset><div class="kd-range">${[['start_page','起始页'],['end_page','结束页']].map(([key,label])=>`<div class="kd-field"><label for="kd-${key}">${label}</label><input id="kd-${key}" name="${key}" type="number" min="1" step="1" inputmode="numeric" value="${esc(model.form[key])}" aria-invalid="${Boolean(model.errors[key])}" aria-describedby="kd-${key}-error" ${disabled?'disabled':''}>${fieldError(key)}</div>`).join('')}<p class="kd-subtle">起始页、结束页都包含。<br>页大小、可见上限和可用时间以本次网页为准。</p></div><div class="kd-actions"><button id="kd-start" class="kd-primary" type="submit" ${startAllowed(c,model.run,model.busy)?'':'disabled'}>${model.busy||running?'正在提取…':'获取 TikTok 原链接'}</button><span class="kd-subtle">${running?'任务在后台运行，关闭此页面也可继续。':'未点击开始，不会采集。'}</span></div></form>`;
  }
  function runHTML() {
    const r=model.run;if(!r)return '<section class="kd-panel"><h2>提取结果</h2><div class="kd-empty">提交商品网址后，结果将保存在这里。</div></section>';
    const c=r.counts||{},active=ACTIVE.has(r.state),req=r.request||{},total=Number(req.end_page)-Number(req.start_page)+1,pages=r.visited_pages?.length||0;
    const elapsed=Math.max(0,(new Date(r.finished_at||Date.now())-new Date(r.started_at))/1000).toFixed(1);
    const canExport=!active&&!model.busy&&c.resolved_unique>0,view=model.onlyNew?model.items.filter(x=>!x.history_at_start):model.items;
    return `<section class="kd-panel"><div class="kd-header"><div><h2>提取结果</h2><span class="kd-subtle">${esc(({QUEUED:'等待提取',RUNNING:'正在提取',SUCCEEDED:'提取完成',SUCCEEDED_WITH_WARNING:'提取完成 · 日期范围仅供参考',PARTIAL_SUCCESS:'部分完成',FAILED:'提取失败',CANCELLED:'已取消'})[r.status]||STATE_LABELS[r.state])} · ${esc(dateTime(r.started_at))}</span></div>${active?'<button data-kd="cancel">取消任务</button>':'<button data-kd="refresh-run">刷新结果</button>'}</div>
    ${active?`<p class="kd-subtle">当前页 ${r.current_page||req.start_page} / ${req.end_page} · 已发现 ${c.visited_rows||0} · 已解析 ${c.resolved_unique||0}</p><progress class="kd-progress" value="${pages}" max="${Math.max(1,total)}"></progress>`:''}
    <div class="kd-metrics">${[[c.visited_rows||0,'原始结果'],[c.resolved_unique||0,'去重结果'],[pages,'页面'],[elapsed+'s','耗时']].map(([value,label])=>`<div class="kd-metric"><span>${label}</span><strong>${value}</strong></div>`).join('')}</div>
    ${r.stop_reason?`<div class="kd-notice kd-error"><strong>${esc(errorName(r.stop_reason))}</strong>${esc(coverageText(r))}${r.diagnostics&&Object.keys(r.diagnostics).length?`<p>预期范围：${esc(r.diagnostics.expected_range||'—')} · 页面识别：${esc(r.diagnostics.observed_range||'—')} · 实际日期：${esc(r.diagnostics.observed_start||'—')} 至 ${esc(r.diagnostics.observed_end||'—')}</p>`:''}</div>`:!active&&(c.unresolved_unique||c.invalid_rows)?`<p class="kd-subtle">${esc(coverageText(r))}</p>`:''}
    ${!active&&!r.stop_reason&&r.query_context?.date_status&&r.query_context.date_status!=='MATCHED'?`<p class="kd-subtle">${esc(r.query_context.date_warning||'页面日期范围可能与所选范围略有差异')} · 请求 ${esc(r.query_context.requested_range||req.period)} · 页面 ${esc(r.query_context.observed_range||'未确认')}${r.query_context.actual_start_date?` · ${esc(r.query_context.actual_start_date)} 至 ${esc(r.query_context.actual_end_date)}`:''}</p>`:''}
    <div class="kd-actions"><button data-kd="copy-all" ${canExport?'':'disabled'}>复制全部</button><button data-kd="export-txt" ${canExport?'':'disabled'}>TXT</button><button data-kd="export-csv" ${canExport?'':'disabled'}>CSV</button><label class="kd-filter"><input id="kd-only-new" type="checkbox" ${model.onlyNew?'checked':''}>仅查看本次新增</label></div>
    ${model.copyFallback?`<div class="kd-notice"><strong>请复制下方原链接</strong><textarea class="kd-copy-text" id="kd-copy-fallback" readonly>${esc(model.copyFallback)}</textarea><button data-kd="select-copy">选中全部文本</button></div>`:''}
    <div class="kd-table-wrap"><table><thead><tr><th>排名</th><th>来源页</th><th>TikTok URL</th><th>状态</th></tr></thead><tbody>${view.length?view.map((item,index)=>`<tr><td>${item.normalized?.rank??index+1}</td><td>${item.source_page}</td><td>${item.status==='resolved'?linkHTML(item.tiktok_url,'tiktok',item.video_id):'—'}</td><td>${item.status==='resolved'?'已提取':esc(errorName(item.error_code))}</td></tr>`).join(''):'<tr><td colspan="4"><div class="kd-empty">暂无已保存结果</div></td></tr>'}</tbody></table></div><p class="kd-subtle">保留来源排名，重复链接仅保留首次出现；只提取链接，不下载视频。</p></section>`;
  }
  function historyHTML() {
    return `<section class="kd-panel"><h2>已保存任务</h2><p class="kd-subtle">模块关闭时仍可查看已有结果。任务重试会创建新记录，不拼接旧榜单。</p><div class="kd-run-select"><select id="kd-run-select" aria-label="选择已保存任务"><option value="">选择任务…</option>${model.runs.map(r=>`<option value="${esc(r.run_id)}" ${r.run_id===model.selectedRun?'selected':''}>${esc(dateTime(r.started_at))} · ${esc(STATE_LABELS[r.state] || r.state)} · ${esc(r.request?.start_page)}–${esc(r.request?.end_page)} 页</option>`).join('')}</select><button data-kd="open-selected" ${model.selectedRun && !model.busy?'':'disabled'}>打开任务</button><button data-kd="refresh-history" ${model.busy?'disabled':''}>刷新记录</button></div><details class="kd-older-runs"><summary class="kd-subtle">通过任务编号打开更早记录</summary><div class="kd-run-select"><input id="kd-run-id" value="${esc(model.readRunId)}" aria-label="任务编号" placeholder="输入已保存的任务编号" autocomplete="off"><button data-kd="open-id" ${model.busy?'disabled':''}>查看</button></div></details></section>`;
  }
  function render() {
    if(!alive)return;
    root.innerHTML=`<section class="page-heading"><div><p class="eyebrow">MATERIAL INTELLIGENCE</p><h1>素材情报 · Kalodata<span class="title-dot">.</span></h1><p class="subtitle">爆款视频检索与原链接提取</p></div></section><section class="kd-panel">${capabilityHTML()}${model.error?`<div class="kd-notice kd-error" role="alert"><strong>${esc(errorName(model.error.code))}</strong>${esc(model.error.message)}</div>`:''}${formHTML()}</section>${runHTML()}${historyHTML()}`;
  }
  async function loadCapabilities() {model.capabilities=await api('/capabilities');}
  async function loadHistory() {const result=await api('/runs?limit=30');model.runs=result.runs || [];}
  async function readRun(id) {
    if(!id || /[/?#]/.test(id))throw new Error('请输入有效任务编号。');
    const generation=++readGeneration;clearTimeout(pollTimer);
    const result=await api(`/runs/${encodeURIComponent(id)}`), run=result.run || result;
    let items=[],cursor=null,revision=null;
    do {
      const result=await api(`/runs/${encodeURIComponent(id)}/items?limit=200${cursor?'&cursor='+encodeURIComponent(cursor):''}`);
      if(revision!==null && result.result_revision!==revision)throw new Error('结果已更新，请刷新本轮任务后查看。');
      revision=result.result_revision;items.push(...result.items);cursor=result.next_cursor;
    } while(cursor && alive);
    if(!alive || generation!==readGeneration)return;
    // No client-side re-sorting or deduplication: all output endpoints share the service order.
    if(!ACTIVE.has(run.state))await loadCapabilities();
    if(!alive || generation!==readGeneration)return;
    model.run=run;model.selectedRun=run.run_id;model.items=items;model.copyFallback='';
    render();schedulePoll();
  }
  function schedulePoll() {clearTimeout(pollTimer);if(alive && ACTIVE.has(model.run?.state))pollTimer=setTimeout(()=>readRun(model.run.run_id).catch(report),1500);}
  async function start(retry=false) {
    if(!startAllowed(model.capabilities,model.run,model.busy))return;
    const source=retry?model.run.request:model.form;
    model.errors=validateInput(source);model.error=null;if(Object.keys(model.errors).length){render();root.querySelector('[aria-invalid=true]')?.focus();return;}
    const parent=retry?model.run.run_id:null;
    model.busy=true;render();
    try {
      const request={product_url:source.product_url,period:source.period,start_page:Number(source.start_page),end_page:Number(source.end_page),client_request_id:newRequestId()};
      const result=await api('/jobs',request);
      model.run=result.run || result;model.items=[];model.onlyNew=false;model.copyFallback='';
      await readRun(model.run.run_id);await loadHistory();
    } catch(error) {model.error={code:error.code || 'LOCAL_REQUEST_FAILED',message:error.message};await loadCapabilities().catch(()=>{});}
    finally {model.busy=false;render();}
  }
  async function copyLinks(scope) {
    if(!model.run || ACTIVE.has(model.run.state))return;
    const result=await api(`/runs/${encodeURIComponent(model.run.run_id)}/links?scope=${scope}`);
    if(result.result_revision!==model.run.result_revision)throw new Error('任务结果已改变，请先刷新任务结果。');
    if(!result.links.length)throw new Error('此范围没有已成功保存的原链接。');
    const text=result.links.join('\n')+'\n';
    try {await navigator.clipboard.writeText(text);toast(`已复制 ${result.links.length} 条原链接。`);model.copyFallback='';}
    catch {model.copyFallback=text;render();root.querySelector('#kd-copy-fallback')?.focus();return;}
    render();
  }
  async function exportLinks(format) {
    if(!model.run || ACTIVE.has(model.run.state))return;
    const response=await fetch(PREFIX+`/runs/${encodeURIComponent(model.run.run_id)}/export?format=${format}&scope=${model.exportScope}`,{credentials:'same-origin',cache:'no-store'});
    if(!response.ok){let data={};try{data=await response.json();}catch{}throw decodeError(data,response.status);}
    const blob=await response.blob();if(!blob.size)throw new Error('没有可导出的成功链接。');
    const disposition=response.headers.get('Content-Disposition') || '';
    let filename=`kalodata_${model.run.run_id}.${format}`;
    const utf=/filename\*=UTF-8''([^;]+)/i.exec(disposition), plain=/filename="?([^";]+)"?/i.exec(disposition);
    try{filename=utf?decodeURIComponent(utf[1]):plain?plain[1]:filename;}catch{}
    filename=filename.replace(/[\/\\\u0000-\u001f]/g,'_');
    const url=URL.createObjectURL(blob),anchor=document.createElement('a');anchor.href=url;anchor.download=filename;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`已生成 ${format.toUpperCase()} 文件，请查看浏览器下载。`);
  }
  async function clicked(event) {
    const button=event.target.closest('[data-kd]');if(!button || button.disabled)return;
    const action=button.dataset.kd;
    try {
      if(action==='refresh-capabilities'){const r=await fetch('/api/kalodata/health',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:'{}'});if(!r.ok)throw decodeError(await r.json(),r.status);await Promise.all([loadCapabilities(),loadConnection()]);render();}
      if(action==='refresh-history'){await loadHistory();render();}
      if(action==='open-selected')await readRun(model.selectedRun);
      if(action==='open-id')await readRun(model.readRunId.trim());
      if(action==='refresh-run')await readRun(model.run.run_id);
      if(action==='retry')await start(true);
      if(action==='cancel'){model.busy=true;render();try{await api(`/runs/${encodeURIComponent(model.run.run_id)}/cancel`,{});await readRun(model.run.run_id);}finally{model.busy=false;render();}}
      if(action==='copy-all' || action==='copy-new')await copyLinks(action==='copy-all'?'all':'new');
      if(action==='export-txt' || action==='export-csv')await exportLinks(action==='export-txt'?'txt':'csv');
      if(action==='select-copy'){const text=root.querySelector('#kd-copy-fallback');text.focus();text.select();}
    } catch(error){report(error);}
  }
  function input(event) {
    const target=event.target;
    if(target.closest('#kd-form') && target.name in model.form)model.form[target.name]=target.value;
    if(target.id==='kd-run-id')model.readRunId=target.value;
  }
  function change(event) {
    input(event);const target=event.target;
    if(target.id==='kd-run-select'){model.selectedRun=target.value;render();}
    if(target.id==='kd-only-new'){model.onlyNew=target.checked;render();}
    if(target.id==='kd-export-scope'){model.exportScope=target.value;render();}
  }
  function submit(event) {if(event.target.id==='kd-form'){event.preventDefault();start();}}
  root.addEventListener('click',clicked);root.addEventListener('input',input);root.addEventListener('change',change);root.addEventListener('submit',submit);
  render();
  (async()=>{try{await Promise.all([loadCapabilities(),loadHistory(),loadConnection(),loadDefaults()]);const latest=model.run||model.runs[0];if(latest)await readRun(latest.run_id);render();}catch(error){report(error);}})();
  tickTimer=setInterval(()=>{if(!alive)return;const remaining=cooldownSeconds(model.capabilities),box=root.querySelector('#kd-cooldown');if(box)box.hidden=!remaining;const text=root.querySelector('#kd-cooldown-text');if(text)text.textContent=`剩余 ${remaining} 秒（截止 ${dateTime(model.capabilities?.cooldown_until)}）`;const start=root.querySelector('#kd-start');if(start)start.disabled=!startAllowed(model.capabilities,model.run,model.busy);const retry=root.querySelector('[data-kd="retry"]');if(retry)retry.disabled=!startAllowed(model.capabilities,model.run,model.busy);},1000);
  return {dispose(){alive=false;clearTimeout(pollTimer);clearInterval(tickTimer);root.removeEventListener('click',clicked);root.removeEventListener('input',input);root.removeEventListener('change',change);root.removeEventListener('submit',submit);}};
}
