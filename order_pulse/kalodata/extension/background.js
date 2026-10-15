import { observeDOM } from './observer.js';
const BASE='http://127.0.0.1:8876/materials-bridge/v1';
const PLUGIN_VERSION='1.1.4',BUILD_ID='20261010.1',KALODATA_ADAPTER='2026-09-v2';
let busy=false,lastJob=null,context=null,snapshot=null;
async function call(path,body,token){
 const r=await fetch(BASE+path,{method:body?'POST':'GET',headers:{...(body?{'Content-Type':'application/json'}:{}),...(token?{Authorization:'Bearer '+token}:{})},...(body?{body:JSON.stringify(body)}:{})});
 if(!r.ok)throw Error('本机连接失败 '+r.status);return r.json();
}
const inject=async(tabId,op,payload)=>(await chrome.scripting.executeScript({target:{tabId},world:'MAIN',func:observeDOM,args:[op,payload]}))[0]?.result||{success:false};
// DOM readiness is sufficient; images/analytics can leave tab.status='loading'.
// Verify the intended route before injecting the page's own semantic waits.
async function loaded(id,expected){
 const target=new URL(expected),until=Date.now()+10000;
 while(Date.now()<until){
  const tab=await chrome.tabs.get(id);let u;try{u=new URL(tab.url);}catch{}
  if(u?.origin===target.origin&&u.pathname===target.pathname&&u.searchParams.get('id')===target.searchParams.get('id')){
   let ready=false;
   try{ready=(await chrome.scripting.executeScript({target:{tabId:id},func:()=>!!document.body&&['interactive','complete'].includes(document.readyState)}))[0]?.result===true;}catch{/* Navigation can replace the document during this probe. */}
   if(ready)return;
  }
  await new Promise(r=>setTimeout(r,150));
 }
 throw Error('PAGE_LOAD_TIMEOUT');
}
function operationFailure(error){
 // An extension/navigation exception is not a website business response.
 if(error?.message==='PAGE_LOAD_TIMEOUT')return {success:false,error_code:'NETWORK_ERROR',http_status:504,data:{error:'详情或商品页面加载超时，请检查页面与网络后重试'}};
 return {success:false,error_code:'SCHEMA_CHANGED',data:{error:'浏览器页面读取未完成，请检查页面是否关闭或已跳转'}};
}
const failure=code=>({success:false,error_code:code});
async function execute(job,s){
 let owned=null;
 try{
  if(job.operation==='bind_product'){
   const u=new URL(job.payload.product_url);if(u.origin!=='https://www.kalodata.com'||u.pathname!=='/product/detail'||!/^\d+$/.test(u.searchParams.get('id')||''))return failure('CONTEXT_CHANGED');
   const tabs=await chrome.tabs.query({url:'https://www.kalodata.com/product/detail*'});
   let tab=tabs.find(t=>{const x=new URL(t.url);return x.searchParams.get('id')===u.searchParams.get('id')&&x.searchParams.get('region')===u.searchParams.get('region');});
   if(!tab)tab=await chrome.tabs.create({url:u.href,active:true});
   s.tabId=tab.id;await chrome.storage.local.set({tabId:s.tabId});await loaded(s.tabId,u.href);context=null;snapshot=null;
  }
  if(!s.tabId)return failure('CONTEXT_CHANGED');
  const payload={...job.payload,...(context?{_page_size:context.page_size}:{})};
  if(job.operation==='verify_context'&&snapshot)Object.assign(payload,{_expected_page:snapshot.page,_row_ids:snapshot.ids});
  let targetId=s.tabId;
  if(job.operation==='resolve_observed_link'){
   if(!context||!snapshot||snapshot.run!==job.payload.run_id||snapshot.page!==job.payload.source_page)return failure('CONTEXT_CHANGED');
   Object.assign(payload,{_expected_page:snapshot.page,_row_ids:snapshot.ids});
   const checked=await inject(s.tabId,'verify_context',payload);if(!checked.success)return checked;
   const u=new URL(job.payload.observed_detail_url);
   if(u.origin!=='https://www.kalodata.com'||u.pathname!=='/video/detail'||u.searchParams.get('id')!==job.payload.video_id)return failure('LINK_ID_MISMATCH');
   owned=await chrome.tabs.create({url:u.href,active:false});targetId=owned.id;await loaded(targetId,u.href);
  }
  let response=await inject(targetId,job.operation,payload);
  if(response.success&&job.operation==='apply_period'){context=response.data;snapshot=null;}
  if(response.success&&job.operation==='read_visible_page')snapshot={run:job.payload.run_id,page:job.payload.source_page,ids:response.data.rows.map(r=>r.video_id)};
  if(owned){const checked=await inject(s.tabId,'verify_context',payload);if(!checked.success)response=checked;}
  return response;
 }finally{if(owned)await chrome.tabs.remove(owned.id).catch(()=>{});}
}
async function poll(){
 if(busy)return;busy=true;
 try{
  const s=await chrome.storage.local.get(['token','tabId']);if(!s.token)return;
  // A paired extension identifies this Chrome profile; opening a product is user-initiated only.
  const handshake=new URLSearchParams({bound:'true',version:PLUGIN_VERSION,build_id:BUILD_ID,kalodata_adapter:KALODATA_ADAPTER});
  const {job}=await call('/job?'+handshake.toString(),null,s.token);if(!job||job.id===lastJob)return;
  lastJob=job.id;let response;
  try{response=await execute(job,s);}catch(error){response=operationFailure(error);}
  await call('/result',{id:job.id,...response},s.token);
 }catch{}finally{busy=false;}
}
chrome.runtime.onMessage.addListener((m,sender,reply)=>{
 if(sender.id!==chrome.runtime.id)return;
 (async()=>{
  if(m.type==='tabs')return (await chrome.tabs.query({url:'https://www.kalodata.com/*'})).map(t=>({id:t.id,title:t.title}));
  if(m.type==='pair'){
   const result=await call('/pair',{code:m.code,extension_id:chrome.runtime.id});
   await chrome.storage.local.set({...result,tabId:null});context=null;snapshot=null;poll();return {ok:true};
  }
  if(m.type==='status'){const s=await chrome.storage.local.get(['token']);return {paired:!!s.token};}
  throw Error('不支持的操作');
 })().then(reply).catch(e=>reply({error:e.message}));return true;
});
chrome.alarms.create('local-heartbeat',{periodInMinutes:0.5});chrome.alarms.onAlarm.addListener(poll);setInterval(poll,2000);poll();
