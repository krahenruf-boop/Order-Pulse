// UI-only adapter. It never reads cookies, response bodies, scripts or media URLs.
export async function observeDOM(operation,payload){
 const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const text=e=>(e?.innerText||e?.textContent||'').trim();
 const wait=async (predicate,error='SCHEMA_CHANGED')=>{const until=Date.now()+16000;while(Date.now()<until){check();const x=predicate();if(x)return x;await new Promise(r=>setTimeout(r,150));}throw Error(error);};
 const check=()=>{const t=document.body.innerText;if(/您的账号已经在新的设备上登录|请再次登录|登录后查看/.test(t))throw Error('LOGIN_REQUIRED');if(/安全验证|请完成验证|验证码/.test(t))throw Error('LOGIN_REQUIRED');if(/请求过于频繁|操作频繁|访问过于频繁/.test(t))throw Error('RATE_LIMITED');if(/升级.*解锁|暂无查看权限|权益不足/.test(t))throw Error('PERMISSION_DENIED');};
 const safeURL=(value,path)=>{const u=new URL(value,location.href);if(u.origin!=='https://www.kalodata.com'||u.pathname!==path||u.username||u.password)throw Error('CONTEXT_CHANGED');return u;};
 const table=()=>Array.from(document.querySelectorAll('table')).find(e=>visible(e)&&text(e.querySelector('thead')).includes('视频内容'));
 const rows=()=>Array.from(table()?.querySelectorAll('tbody tr[data-row-key]')||[]).filter(visible);
 const verifiedEmpty=()=>{const e=table()?.querySelector('.ant-table-placeholder');return visible(e)&&rows().length===0&&text(e).includes('暂无查询结果，请修改筛选条件或搜索词后重试。');};
 const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
 const digest=async x=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical(x)))))).map(b=>b.toString(16).padStart(2,'0')).join('');
 // Capture only the destination emitted by this normal UI click, without loading TikTok media.
 const clickDestination=async (element,validate)=>{
  if(!visible(element))throw Error('LINK_NOT_FOUND');let observed=null;const original=window.open;
  const wrapper=function(url){if(typeof url==='string'&&url){const u=new URL(url,location.href);if(validate(u))observed=u.href;}return null;};
  window.open=wrapper;
  try{element.click();await new Promise(r=>setTimeout(r,100));if(!observed)throw Error('LINK_NOT_FOUND');return observed;}
  finally{if(window.open===wrapper)window.open=original;}
 };
 const pagination=()=>table()?.closest('.ant-table-wrapper')?.querySelector('.ant-pagination');
 const currentPage=()=>pagination()?Number(text(pagination().querySelector('.ant-pagination-item-active'))):verifiedEmpty()?1:0;
 const rowIds=()=>rows().map(r=>r.getAttribute('data-row-key'));
 const dateTokens=value=>Array.from(String(value||'').matchAll(/(?:^|[^0-9])(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?![0-9])/g),m=>{
  const y=Number(m[1]),month=Number(m[2]),day=Number(m[3]),d=new Date(Date.UTC(y,month-1,day));
  return d.getUTCFullYear()===y&&d.getUTCMonth()===month-1&&d.getUTCDate()===day?`${String(y).padStart(4,'0')}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`:null;
 }).filter(Boolean);
 const periodDates=selected=>{
  const oldStart=document.querySelector('input[placeholder="开始日期"]')?.value;
  const oldEnd=document.querySelector('input[placeholder="结束日期"]')?.value;
  if(dateTokens(oldStart).length===1&&dateTokens(oldEnd).length===1)return [dateTokens(oldStart)[0],dateTokens(oldEnd)[0]];
  // Kalodata now renders its date range in a combined/renamed date control.
  // Read only visible controls near the selected preset; never infer dates from URL params.
  let scope=selected?.el||selected;
  for(let depth=0;scope&&depth<7;depth++,scope=scope.parentElement){
   const controls=Array.from(scope.querySelectorAll('input, [role="textbox"], [class*="range"], [class*="Range"], [class*="picker"], [class*="Picker"]')).filter(visible);
   const dates=[];
   for(const e of controls){const value=(e.value||text(e)||'').slice(0,500);dates.push(...dateTokens(value));}
   const unique=[...new Set(dates)];if(unique.length===2)return unique;
  }
  return null;
 };
 const periodFromText=value=>{const label=String(value||'').replace(/\s+/g,'');if(label==='昨日')return '1D';const m=label.match(/^(?:过去)?(7|30|90|180|365)天$/);return m?m[1]+'D':null;};
 const presetControls=()=>{
  const nodes=Array.from(document.querySelectorAll('label,button,[role="radio"],[role="tab"],[aria-pressed],[aria-current]')).filter(visible),byPeriod=new Map();
  for(const node of nodes){const input=node.matches('input')?node:node.querySelector('input[type="radio"]');const period=periodFromText(text(node)||text(input?.labels?.[0]));if(!period||byPeriod.has(period))continue;
   const label=node.closest('label')||node;const selected=input?.checked===true||[node,label].some(e=>{const classes=String(e?.className||'').split(/\s+/);return e?.getAttribute('aria-checked')==='true'||e?.getAttribute('aria-selected')==='true'||e?.getAttribute('aria-pressed')==='true'||['true','page','location'].includes(e?.getAttribute('aria-current'))||e?.getAttribute('data-state')==='checked'||classes.some(c=>/(?:^|[-_])(?:active|selected|checked)(?:$|[-_])/i.test(c));});
   const disabled=input?.disabled===true||node.getAttribute('aria-disabled')==='true'||label.getAttribute('aria-disabled')==='true';byPeriod.set(period,{el:label,period,selected,disabled,label:text(label)});
  }
  return byPeriod;
 };
 const selectedPeriod=()=>[...presetControls().values()].find(x=>x.selected)||null;
 // A selected preset changes before the asynchronous video list does. Freeze a
 // context only after list refresh evidence, then a quiet interval including pagination.
 const listSignature=()=>JSON.stringify([currentPage(),rows().map(r=>[r.getAttribute('data-row-key'),text(r)]),text(pagination()),Array.from(table()?.querySelectorAll('th[aria-sort]')||[]).map(e=>[text(e),e.getAttribute('aria-sort')])]);
 const loading=()=>Array.from(document.querySelectorAll('.ant-spin-spinning')).some(visible);
 const settled=async({before=null,requireRefresh=false}={})=>{
  const until=Date.now()+12000;let prior=null,since=0,refreshed=!requireRefresh;
  while(Date.now()<until){
   check();
   if(loading()){refreshed=true;prior=null;since=0;}
   else if(table()&&(pagination()||verifiedEmpty())&&currentPage()>0){
    const sig=listSignature();if(before!==null&&sig!==before)refreshed=true;
    if(sig!==prior){prior=sig;since=Date.now();}
    if(refreshed&&Date.now()-since>=600)return true;
   }else{prior=null;since=0;}
   await new Promise(r=>setTimeout(r,150));
  }
  if(loading())throw Error('SOURCE_REFRESH_TIMEOUT');
  if(!table())throw Error('SOURCE_LIST_NOT_READY');
  if(!pagination()&&!verifiedEmpty())throw Error('PAGINATION_SCHEMA_CHANGED');
  // A declined date selection may leave a usable, stable list. Dates stay advisory.
  if(prior!==null&&Date.now()-since>=600)return false;
  throw Error('SOURCE_REFRESH_TIMEOUT');
 };
 let refreshUnconfirmed=false;
 const goPage=async n=>{
  if(!Number.isInteger(n)||n<1)throw Error('PAGE_MISMATCH');if(currentPage()===n)return;
  const pg=pagination();if(!pg)throw Error('SCHEMA_CHANGED');
  const link=Array.from(pg.querySelectorAll('.ant-pagination-item')).find(e=>text(e)===String(n));
  if(link)link.click();else{
   const input=pg.querySelector('.ant-pagination-options-quick-jumper input');if(!input||!visible(input)||input.disabled)throw Error('RANGE_EXCEEDS_VISIBLE');
   Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,String(n));input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));input.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
  }
  await wait(()=>currentPage()===n);await settled();
 };
 const readContext=async()=>{
  check();const u=safeURL(location.href,'/product/detail');if(/商品不存在|未找到该商品|商品已下架/.test(document.body.innerText))throw Error('PRODUCT_NOT_FOUND');await wait(()=>table()&&!document.querySelector('.ant-spin-spinning'),'SOURCE_LIST_NOT_READY');
  const selected=selectedPeriod();
  const period=selected?.period;const label=selected?.label||'';
  const dateRange=selected&&periodDates(selected),a=dateRange?.[0],b=dateRange?.[1];
  const diagnostic={expected_range:payload.period||'unknown',observed_range:period||'unknown',observed_start:a||'',observed_end:b||''};
  // Dates are advisory. A missing/late/mismatched range must never block a readable video list.
  const requestedDays={"1D":1,"7D":7,"30D":30,"90D":90,"180D":180,"365D":365}[payload.period];
  const days=a&&b?Math.round((Date.parse(b+'T00:00:00Z')-Date.parse(a+'T00:00:00Z'))/86400000)+1:null;
  const tolerance={"1D":[1,3],"7D":[5,10],"30D":[25,35],"90D":[80,100],"180D":[165,195],"365D":[330,400]}[payload.period];
  const dateStatus=refreshUnconfirmed?'UNKNOWN':selected&&period===payload.period&&days===requestedDays?'MATCHED':days&&tolerance&&days>=tolerance[0]&&days<=tolerance[1]?'APPROXIMATE':'UNKNOWN';
  const rs=rows(),pg=pagination(),empty=verifiedEmpty();if(!table())throw Error('SOURCE_LIST_NOT_READY');if(!pg&&!empty)throw Error('PAGINATION_SCHEMA_CHANGED');
  if(!Number.isInteger(currentPage())||currentPage()<1)throw Error('PAGE_MISMATCH');
  const prev=pg?.querySelector('.ant-pagination-prev'),next=pg?.querySelector('.ant-pagination-next');
  const disabled=e=>!!e&&(e.classList.contains('ant-pagination-disabled')||e.getAttribute('aria-disabled')==='true'||!!e.querySelector('button:disabled'));
  // Only visible pagination controls establish the range; never change page size or entitlement.
  const itemNumbers=Array.from(pg?.querySelectorAll('.ant-pagination-item')||[]).map(e=>Number(text(e))).filter(Number.isInteger);
  const sort=Array.from(table().querySelectorAll('th[aria-sort]')).filter(e=>['ascending','descending'].includes(e.getAttribute('aria-sort'))).map(e=>({field:text(e),direction:e.getAttribute('aria-sort')==='ascending'?'ASC':'DESC'}));
  const c={product_id:u.searchParams.get('id'),region:u.searchParams.get('region'),period:period||null,requested_range:payload.period||null,observed_range:period||null,date_status:dateStatus,date_warning:dateStatus==='MATCHED'?null:dateStatus==='APPROXIMATE'?'页面日期范围可能与所选范围略有差异':'未确认页面日期范围，结果按当前 Kalodata 页面实际显示内容提取',actual_start_date:a||null,actual_end_date:b||null,site_date_label:a&&b?label+' '+a+' ~ '+b:'',site_timezone:null,date_basis:dateStatus==='UNKNOWN'?'unknown':'page_visible_range',page_size:payload._page_size||rs.length||10,visible_total:empty?0:null,visible_pages:empty||disabled(prev)&&disabled(next)?1:Math.max(...itemNumbers),observed_sort:sort,filters:{},source_mode:'ui_dom',captured_at:new Date().toISOString()};
  const stable={...c};for(const k of ['captured_at','visible_total','visible_pages','site_date_label','date_warning','period','requested_range','observed_range','date_status','actual_start_date','actual_end_date','date_basis'])delete stable[k];c.context_fingerprint=await digest(stable);return c;
 };
 try{
  check();
  if(operation==='resolve_observed_link'){
   const u=safeURL(location.href,'/video/detail');if(u.searchParams.get('id')!==payload.video_id)throw Error('LINK_ID_MISMATCH');
   const button=await wait(()=>Array.from(document.querySelectorAll('button')).find(e=>visible(e)&&text(e).startsWith('TikTok视频链接')));check();
   const href=await clickDestination(button,u=>u.origin==='https://www.tiktok.com'&&new RegExp('^/@[A-Za-z0-9_.]+/video/'+payload.video_id+'/?$').test(u.pathname));
   return {success:true,data:{kind:'detail_anchor',observed_href:href,source_url:location.href,observed_at:new Date().toISOString(),source_page:payload.source_page,source_index:payload.source_index}};
  }
  const u=safeURL(location.href,'/product/detail');
  if(operation==='bind_product'){const target=safeURL(payload.product_url,'/product/detail');if(u.searchParams.get('id')!==target.searchParams.get('id')||u.searchParams.get('region')!==target.searchParams.get('region'))throw Error('CONTEXT_CHANGED');return {success:true,data:{bound:true}};}
  if(operation==='apply_period'){
   if(!['1D','7D','30D','90D','180D','365D'].includes(payload.period))throw Error('PERIOD_UNAVAILABLE');
   // Let an initially opening product finish rendering before locating its presets.
   await settled();
   const current=selectedPeriod(),target=presetControls().get(payload.period);
   if(current?.period!==payload.period&&target&&!target.disabled){
    const before=listSignature();target.el.click();
    refreshUnconfirmed=!(await settled({before,requireRefresh:true}));
   }
   await goPage(1);
   return {success:true,data:await readContext()};
  }
  if(operation==='verify_context'){const c=await readContext();if(c.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');if(payload._expected_page&&currentPage()!==payload._expected_page)throw Error('PAGE_MISMATCH');if(payload._row_ids&&JSON.stringify(rowIds())!==JSON.stringify(payload._row_ids))throw Error('CONTEXT_CHANGED');return {success:true,data:c};}
  if(operation==='read_visible_page'){
   const before=await readContext();if(before.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');
   if(!Number.isInteger(payload.source_page)||payload.source_page<1||payload.source_page>before.visible_pages)throw Error('RANGE_EXCEEDS_VISIBLE');
   await goPage(payload.source_page);const c=await readContext();if(c.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');const originalIds=rowIds();
   const result=[];
   for(const row of rows()){
   check();const id=row.getAttribute('data-row-key');if(!/^\d+$/.test(id||''))throw Error('VIDEO_ITEM_SCHEMA_CHANGED');
    const td=row.querySelector('td:nth-child(2)');
    const title=Array.from(td?.querySelectorAll('div,span,p')||[]).find(e=>visible(e)&&e.children.length===0&&text(e).length>0&&!/^(?:AD|工具箱|\d+(?:m|s)(?: \d+s)?)$/.test(text(e))&&!e.classList.contains('kds-icon'));
    let href=null;try{href=await clickDestination(title,u=>u.origin==='https://www.kalodata.com'&&u.pathname==='/video/detail'&&u.searchParams.get('id')===id);}catch(e){if(e.message!=='LINK_NOT_FOUND')throw e;}
    result.push({video_id:id,kalodata_video_url:href});
   }
   const after=await readContext();if(after.context_fingerprint!==c.context_fingerprint||JSON.stringify(rowIds())!==JSON.stringify(originalIds)||currentPage()!==payload.source_page)throw Error('CONTEXT_CHANGED');
   return {success:true,data:{source_page:payload.source_page,context_fingerprint:c.context_fingerprint,product_id:c.product_id,region:c.region,page_size:c.page_size,visible_pages:c.visible_pages,verified:true,rows:result,empty_verified:result.length===0,observed_at:new Date().toISOString(),terminal_page:verifiedEmpty()||currentPage()===c.visible_pages&&pagination()?.querySelector('.ant-pagination-next')?.getAttribute('aria-disabled')==='true'}};
  }
  if(operation==='inspect_ui')return {success:true,data:{context:await readContext(),rows:rows().slice(0,1).map(r=>({id:r.getAttribute('data-row-key'),elements:Array.from(r.querySelectorAll('td:nth-child(2) div,td:nth-child(2) span')).filter(e=>visible(e)&&e.children.length===0).map(e=>({text:text(e).slice(0,200),class:e.className}))})),pagination:pagination()?.outerHTML.replace(/<svg[\s\S]*?<\/svg>/g,'').slice(0,6000)}};
  throw Error('SCHEMA_CHANGED');
 }catch(e){const code=e.message;return {success:false,error_code:code,...(code==='LOGIN_REQUIRED'?{login_required:true}:{}),...(code==='PERMISSION_DENIED'?{entitlement_denied:true}:{}),...(code==='RATE_LIMITED'?{http_status:429,retry_after:60}:{}),data:{error:code,...(e.diagnostic?{diagnostic:e.diagnostic}:{})}};}
}
