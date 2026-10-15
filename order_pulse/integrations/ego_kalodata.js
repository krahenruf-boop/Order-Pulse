// Migrated from the production-verified Kalodata DOM observer.
// It runs only in the explicitly bound page of ego lite's existing Space.
// UI-only adapter. It never reads cookies, response bodies, scripts or media URLs.
async function observeDOM(input){
 const {operation,payload}=input;
 const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const text=e=>(e?.innerText||e?.textContent||'').trim();
 const wait=async predicate=>{const until=Date.now()+16000;while(Date.now()<until){check();const x=predicate();if(x)return x;await new Promise(r=>setTimeout(r,150));}throw Error('SCHEMA_CHANGED');};
 const check=()=>{const t=document.body.innerText;if(/您的账号已经在新的设备上登录|请再次登录|登录后查看/.test(t))throw Error('LOGIN_REQUIRED');if(/安全验证|请完成验证|验证码/.test(t))throw Error('LOGIN_REQUIRED');if(/请求过于频繁|操作频繁|访问过于频繁/.test(t))throw Error('RATE_LIMITED');if(/账号每天可查看\s*\d+\s*次详情页[，,]?\s*今天剩余\s*0\s*次/.test(t))throw Error('DETAIL_QUOTA_EXHAUSTED');if(/升级.*解锁|暂无查看权限|权益不足/.test(t))throw Error('PERMISSION_DENIED');if(operation==='resolve_observed_link'&&/加载失败，请刷新重试/.test(t))throw Error('SOURCE_LOAD_FAILED');};
 const safeURL=(value,path)=>{const u=new URL(value,location.href);if(u.origin!=='https://www.kalodata.com'||u.pathname!==path||u.username||u.password)throw Error('CONTEXT_CHANGED');return u;};
 const table=()=>Array.from(document.querySelectorAll('table')).find(e=>visible(e)&&text(e.querySelector('thead')).includes('视频内容'));
 const rows=()=>Array.from(table()?.querySelectorAll('tbody tr[data-row-key]')||[]).filter(visible);
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
 const currentPage=()=>Number(text(pagination()?.querySelector('.ant-pagination-item-active')));
 const rowIds=()=>rows().map(r=>r.getAttribute('data-row-key'));
 const settled=async()=>{let prior='',since=0;return wait(()=>{if(document.querySelector('.ant-spin-spinning')){since=0;return false;}const sig=JSON.stringify([currentPage(),rowIds(),...Array.from(document.querySelectorAll('input[placeholder$=日期]')).map(e=>e.value)]);if(sig!==prior){prior=sig;since=Date.now();}return table()&&since&&Date.now()-since>=450;});};
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
  check();const u=safeURL(location.href,'/product/detail');await wait(()=>table()&&!document.querySelector('.ant-spin-spinning'));
  const selected=Array.from(document.querySelectorAll('label.ant-radio-button-wrapper')).find(e=>visible(e)&&e.querySelector('input')?.checked);
  const label=text(selected);const period=label==='昨日'?'1D':(label.match(/^过去(7|30|90|180|365)天$/)||[])[1]+'D';
  const a=document.querySelector('input[placeholder="开始日期"]')?.value?.replaceAll('/','-'),b=document.querySelector('input[placeholder="结束日期"]')?.value?.replaceAll('/','-');
  if(!/^\d{4}-\d{2}-\d{2}$/.test(a||'')||!/^\d{4}-\d{2}-\d{2}$/.test(b||'')||!selected)throw Error('DATE_CONTEXT_UNVERIFIED');
  const rs=rows(),pg=pagination();if(!pg||!rs.length)throw Error('SCHEMA_CHANGED');
  if(!Number.isInteger(currentPage())||currentPage()<1)throw Error('PAGE_MISMATCH');
  const prev=pg.querySelector('.ant-pagination-prev'),next=pg.querySelector('.ant-pagination-next');
  const disabled=e=>!!e&&(e.classList.contains('ant-pagination-disabled')||e.getAttribute('aria-disabled')==='true'||!!e.querySelector('button:disabled'));
  // Only visible pagination controls establish the range; never change page size or entitlement.
  const itemNumbers=Array.from(pg.querySelectorAll('.ant-pagination-item')).map(e=>Number(text(e))).filter(Number.isInteger);
  const sort=Array.from(table().querySelectorAll('th[aria-sort]')).filter(e=>['ascending','descending'].includes(e.getAttribute('aria-sort'))).map(e=>({field:text(e),direction:e.getAttribute('aria-sort')==='ascending'?'ASC':'DESC'}));
  const c={product_id:u.searchParams.get('id'),region:u.searchParams.get('region'),period,actual_start_date:a,actual_end_date:b,site_date_label:label+' '+a+' ~ '+b,site_timezone:null,date_basis:'site_preset_verified',page_size:payload._page_size||rs.length,visible_total:null,visible_pages:disabled(prev)&&disabled(next)?1:Math.max(...itemNumbers),observed_sort:sort,filters:{},source_mode:'ui_dom',captured_at:new Date().toISOString()};
  const stable={...c};for(const k of ['captured_at','visible_total','visible_pages','site_date_label'])delete stable[k];c.context_fingerprint=await digest(stable);return c;
 };
 try{
  check();
  if(operation==='probe'){
   if(location.origin!=='https://www.kalodata.com'||/\/login|\/signin/.test(location.pathname))throw Error('LOGIN_REQUIRED');
   return {success:true,data:{url:location.href,login_valid:true}};
  }
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
   const labelText=payload.period==='1D'?'昨日':'过去'+payload.period.slice(0,-1)+'天';
   const label=Array.from(document.querySelectorAll('label.ant-radio-button-wrapper')).find(e=>visible(e)&&text(e)===labelText);if(!label||label.querySelector('input')?.disabled)throw Error('PERIOD_UNAVAILABLE');
   if(!label.querySelector('input')?.checked)label.click();await wait(()=>label.querySelector('input')?.checked&&!document.querySelector('.ant-spin-spinning'));
   await settled();await goPage(1);
   return {success:true,data:await readContext()};
  }
  if(operation==='verify_context'){const c=await readContext();if(c.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');if(payload._expected_page&&currentPage()!==payload._expected_page)throw Error('PAGE_MISMATCH');if(payload._row_ids&&JSON.stringify(rowIds())!==JSON.stringify(payload._row_ids))throw Error('CONTEXT_CHANGED');return {success:true,data:c};}
  if(operation==='read_visible_page'){
   const before=await readContext();if(before.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');
   if(!Number.isInteger(payload.source_page)||payload.source_page<1||payload.source_page>before.visible_pages)throw Error('RANGE_EXCEEDS_VISIBLE');
   await goPage(payload.source_page);const c=await readContext();if(c.context_fingerprint!==payload.context_fingerprint)throw Error('CONTEXT_CHANGED');const originalIds=rowIds();
   const result=[];
   for(const row of rows()){
    check();const id=row.getAttribute('data-row-key');if(!/^\d+$/.test(id||''))throw Error('SCHEMA_CHANGED');
    const td=row.querySelector('td:nth-child(2)');
    const title=Array.from(td?.querySelectorAll('div,span,p')||[]).find(e=>visible(e)&&e.children.length===0&&text(e).length>0&&!/^(?:AD|工具箱|\d+(?:m|s)(?: \d+s)?)$/.test(text(e))&&!e.classList.contains('kds-icon'));
    let href=null;try{href=await clickDestination(title,u=>u.origin==='https://www.kalodata.com'&&u.pathname==='/video/detail'&&u.searchParams.get('id')===id);}catch(e){if(e.message!=='LINK_NOT_FOUND')throw e;}
    result.push({video_id:id,kalodata_video_url:href});
   }
   const after=await readContext();if(after.context_fingerprint!==c.context_fingerprint||JSON.stringify(rowIds())!==JSON.stringify(originalIds)||currentPage()!==payload.source_page)throw Error('CONTEXT_CHANGED');
   return {success:true,data:{source_page:payload.source_page,context_fingerprint:c.context_fingerprint,product_id:c.product_id,region:c.region,page_size:c.page_size,visible_pages:c.visible_pages,verified:true,rows:result,observed_at:new Date().toISOString(),terminal_page:currentPage()===c.visible_pages&&pagination().querySelector('.ant-pagination-next')?.getAttribute('aria-disabled')==='true'}};
  }
  if(operation==='inspect_ui')return {success:true,data:{context:await readContext(),rows:rows().slice(0,1).map(r=>({id:r.getAttribute('data-row-key'),elements:Array.from(r.querySelectorAll('td:nth-child(2) div,td:nth-child(2) span')).filter(e=>visible(e)&&e.children.length===0).map(e=>({text:text(e).slice(0,200),class:e.className}))})),pagination:pagination()?.outerHTML.replace(/<svg[\s\S]*?<\/svg>/g,'').slice(0,6000)}};
  throw Error('SCHEMA_CHANGED');
 }catch(e){const code=e.message;return {success:false,error_code:code,...(code==='LOGIN_REQUIRED'?{login_required:true}:{}),...(code==='PERMISSION_DENIED'?{entitlement_denied:true}:{}),...(code==='RATE_LIMITED'?{http_status:429,retry_after:60}:{}),data:{error:code}};}
}


const MARKER='ORDER_PULSE_KALODATA:';
let result;
try {
 const spec=INPUT;
 const spaces=await listTaskSpaces();
 const entry=spaces.find(s=>s.id===spec.space_id);
 if(!entry) result={success:false,error_code:'BOUND_SPACE_MISSING',data:{error:'Bound Space unavailable'}};
 else if(entry.name!==spec.space_name) result={success:false,error_code:'SESSION_MISMATCH',data:{error:'Space mismatch'}};
 else if(spec.expected_profile_id&&entry.profileId!==spec.expected_profile_id) result={success:false,error_code:'SESSION_MISMATCH',data:{error:'Profile mismatch'}};
 else if(spec.expected_profile_name&&entry.profileName!==spec.expected_profile_name) result={success:false,error_code:'SESSION_MISMATCH',data:{error:'Profile identity mismatch'}};
 else {
  let task,takeover=false;
  if(entry.ownership==='agent') task=await taskSpace(entry.id);
  else if(['user','agentDelegatedToUser'].includes(entry.ownership)&&spec.auto_takeover===true) {
   try {task=entry.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(entry.id):await claimTaskSpace(entry.id);takeover=true;}
   catch {result={success:false,error_code:'WAITING_BROWSER',data:{error:'Dedicated Space takeover failed'}};}
  } else result={success:false,error_code:'WAITING_USER',data:{error:'User controls dedicated Space'}};
  if(result) {/* bounded takeover failure */}
  else if(task.spaceId!==spec.space_id) result={success:false,error_code:'SESSION_MISMATCH',data:{error:'Different Space returned'}};
  else {
  const tab=(await task.tabs()).find(t=>t.label===spec.page_label);
  if(!tab) result={success:false,error_code:'BLOCKED_TRANSPORT',data:{error:'Bound page missing'}};
  else {
   const page=tab.page;
   if(spec.operation==='probe') {
    const url=await page.url();
    result=await page.evaluate(observeDOM,{operation:'probe',payload:{}});
    if(result.success)result.data={...result.data,url,ownership:'agent',takeover_performed:takeover,space_id:entry.id,profile_id:entry.profileId,profile_name:entry.profileName,title:await page.title()};
   } else if(spec.operation==='bind_product') {
    const target=new URL(spec.payload.product_url);
    if(target.origin!=='https://www.kalodata.com'||target.pathname!=='/product/detail'||!/^[0-9]+$/.test(target.searchParams.get('id')||''))
      result={success:false,error_code:'CONTEXT_CHANGED',data:{error:'Invalid product URL'}};
    else {
     const current=new URL(await page.url());
     if(current.origin!==target.origin||current.pathname!==target.pathname||current.searchParams.get('id')!==target.searchParams.get('id')||current.searchParams.get('region')!==target.searchParams.get('region')) {
      await page.goto(target.href);
     }
     result=await page.evaluate(observeDOM,{operation:'bind_product',payload:spec.payload});
    }
   } else if(spec.operation==='resolve_observed_link') {
    const verify={...spec.payload,_expected_page:spec.payload._expected_page,_row_ids:spec.payload._row_ids};
    const checked=await page.evaluate(observeDOM,{operation:'verify_context',payload:verify});
    if(!checked.success) result=checked;
    else {
     const detail=new URL(spec.payload.observed_detail_url);
     if(detail.origin!=='https://www.kalodata.com'||detail.pathname!=='/video/detail'||detail.searchParams.get('id')!==spec.payload.video_id)
      result={success:false,error_code:'LINK_ID_MISMATCH',data:{error:'Detail mismatch'}};
     else {
      let owned;
      try {
       owned=await task.newPage();await owned.goto(detail.href);
       result=await owned.evaluate(observeDOM,{operation:spec.operation,payload:spec.payload});
       if(result.success){const after=await page.evaluate(observeDOM,{operation:'verify_context',payload:verify});if(!after.success)result=after;}
      } finally {if(owned)await owned.close().catch(()=>{});}
     }
    }
   } else result=await page.evaluate(observeDOM,{operation:spec.operation,payload:spec.payload});
  }
  }
 }
} catch(error) {
 const code=['WAITING_USER','WAITING_BROWSER','BOUND_SPACE_MISSING'].includes(error?.message)?error.message:error?.message==='PAGE_LOAD_TIMEOUT'?'NETWORK_ERROR':'SCHEMA_CHANGED';
 result={success:false,error_code:code,data:{error:code}};
}
console.log(MARKER+JSON.stringify(result));
