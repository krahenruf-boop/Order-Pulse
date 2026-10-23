// Prepare source pages only during an authorized collection task.
const entries=await listTaskSpaces();
const matches=entries.filter(e=>e.name===s.expected_name&&e.profileId===s.profile_id&&e.profileName===s.profile_name);
const candidate=matches.length===1?matches[0]:null;
const e=candidate&&(candidate.id===s.space_id||candidate.taskId===s.expected_name)?candidate:null;
const sourceUrl=tab=>{try{const u=new URL(tab.url);return u.origin==='https://tms.mabangerp.com'?u:null;}catch{return null;}};
const isOrder=tab=>sourceUrl(tab)?.hash.startsWith('#/myorder');
let browser,result;
if(!e)result={error:'BROWSER_BINDING_REQUIRED'};
else {
 let task;
 if(e.ownership==='agent')task=await taskSpace(e.id);
 else if(s.task_active===true&&s.auto_takeover===true&&['user','agentDelegatedToUser'].includes(e.ownership)) {
  try {task=e.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(e.id):await claimTaskSpace(e.id);if(task.spaceId===e.id)console.log('AOV_CONTROL:claimed');}
  catch {result={error:'WAITING_USER'};}
 } else result={error:'WAITING_USER'};
 if(!result&&task.spaceId!==e.id)result={error:'BROWSER_BINDING_REQUIRED'};
 if(!result) {
  browser={space_id:e.id,space_name:e.name,profile_id:e.profileId,profile_name:e.profileName};
  const all=await task.tabs(),orders=all.filter(isOrder),sources=all.filter(sourceUrl);
  if(orders.length===1) {
   const p=orders[0].label?task.page(orders[0].label):await task.adopt(orders[0].page);
   result={space_id:e.id,page_label:p.label};
  } else if(orders.length>1||sources.length>1)result={error:'BROWSER_BINDING_REQUIRED'};
  else if(sources.some(t=>sourceUrl(t).hash.startsWith('#/login')))result={error:'AUTH_REQUIRED'};
  else if(s.task_active!==true)result={error:'TMS_PAGE_REQUIRED'};
  else {
   // Reuse the unique Mabang tab, even if the user left it on products/home.
   // When closed, open one source tab in this same verified Space. Retries reuse it.
   let p,created=false;
   try {
    if(sources.length)p=sources[0].label?task.page(sources[0].label):await task.adopt(sources[0].page);
    else {p=await task.newPage();created=true;}
    try {await p.goto('https://tms.mabangerp.com/#/myorderlist',{waitUntil:'domcontentloaded',timeout:12000});}
    catch { /* A committed SPA can still be usable after a load timeout. */ }
    await p.waitForFunction(()=>location.origin==='https://tms.mabangerp.com'&&(
     location.hash.startsWith('#/login')||location.hash.startsWith('#/myorder')&&!!document.querySelector('input[placeholder*="SKU"]')
    ),undefined,{timeout:15000});
    const actual={url:await p.url()};
    result=sourceUrl(actual)?.hash.startsWith('#/login')?{error:'AUTH_REQUIRED'}:
     isOrder(actual)?{space_id:e.id,page_label:p.label}:{error:'WAITING_CONNECTION'};
   } catch {
    result={error:'WAITING_CONNECTION'};
    // Never close an existing user page. Only discard our failed blank/error tab.
    if(created&&p&&!sourceUrl({url:await p.url().catch(()=>'')}))await p.close().catch(()=>{});
   }
  }
 }
}
console.log('AOV_BINDING:'+JSON.stringify({...result,...(browser?{browser}:{})}));
