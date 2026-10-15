// Normal TMS UI exports. No HTTP replay, Cookie reads, hidden endpoints or template saves.
const spec=INPUT, origin='https://tms.mabangerp.com';
const routes={current:'myorderlist',completed:'myorderhistorylist'};
let page,installed=false,result,stage='browser-binding';
try {
 const entry=(await listTaskSpaces()).find(s=>s.id===spec.space_id);
 if(!entry||entry.ownership!=='agent')throw Error('WAITING_USER');
 const task=await taskSpace(spec.space_id), tab=(await task.tabs()).find(t=>t.label===spec.page_label);
 if(!tab)throw Error('WAITING_CONNECTION');
 if(tab.url.includes('/#/login'))throw Error('AUTH_REQUIRED');
 if(!Object.values(routes).some(r=>tab.url===origin+'/#/'+r))throw Error('WAITING_CONNECTION');
 page=task.page(spec.page_label);stage='hook-install';
 if(spec.action==='cleanup') {
   await page.evaluate(()=>{
    window.__pulseExport?.restore();
    const box=[...document.querySelectorAll('li.text-center input[type="checkbox"]')].find(x=>x.getClientRects().length);
    if(box?.checked)box.click();
   });
   result={cleaned:true};
 } else {
 await page.evaluate(spec=>{
  if(window.__tmsMonitor||window.__pulseExport)throw Error('BROWSER_BUSY');
  const state={expected:null,result:null,captured:null,suppressed:0,inflight:new Set()};window.__pulseExport=state;
  state.vm=()=>{
   let v=document.querySelector('input[placeholder="SKU模糊搜索"],input[placeholder="SKU精确搜索"]')?.closest('.el-input')?.__vue__;
   for(let i=0;v&&i<14;i++,v=v.$parent)if(v.search&&Array.isArray(v.shopList))return v;
   throw Error('SOURCE_CHANGED');
  };
  const id=v=>typeof v==='string'?v:Number.isSafeInteger(v)?String(v):null;
  state.account=()=>{const e=document.querySelector('#app')?.__vue__?.$store?.state?.employee;return e?{employee_id:id(e.id),supplier_id:id(e.supplierId),customer_id:id(e.customerId)}:null;};
  state.identity=()=>{
   if(JSON.stringify(state.account())!==JSON.stringify(spec.account)) {
    const a=state.account();if(!a||Object.keys(spec.account).some(k=>a[k]!==spec.account[k]))throw Error('ACCOUNT_SCOPE_CHANGED');
   }
   const ids=state.vm().shopList.filter(s=>s.platformId===104&&id(s.companyId)===spec.account.customer_id).map(s=>id(s.id)).sort();
   if(JSON.stringify(ids)!==JSON.stringify([...spec.shop_ids].sort()))throw Error('CATALOG_CHANGED');
  };
  const P=XMLHttpRequest.prototype, open=P.open, send=P.send, meta=new WeakMap();let timer;
  state.restore=()=>{clearTimeout(timer);if(window.__pulseExport!==state)return;
   // A late export response must not start a native download after cancellation.
   for(const xhr of state.inflight)if(xhr.readyState!==4)xhr.abort();
   P.open=open;P.send=send;state.restoreDownload?.();delete window.__pulseExport;};
  timer=setTimeout(state.restore,Math.min(90000,spec.operation_timeout_ms||90000));
  P.open=function(method,url,...rest){meta.set(this,{method,path:new URL(url,location.href).pathname});return open.call(this,method,url,...rest);};
  P.send=function(body){
   const m=meta.get(this);
   if(m&&['/tmsapi/findOrderlist','/tmsapi/findOrderHistorylist','/tmsapi/exportOrder'].includes(m.path)){
    const expected=state.expected;if(!expected){state.result={error:'REQUEST_SCOPE_MISMATCH'};throw Error('ORDER_PULSE_SCOPE_GUARD');}
    let q;try{q=JSON.parse(body)}catch{throw Error('SOURCE_CHANGED');}
    state.identity();
    const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b), current=spec.partition==='current';
    const listPath='/tmsapi/'+(current?'findOrderlist':'findOrderHistorylist');
    let valid=m.method.toUpperCase()==='POST';
    if(expected.kind==='list'||expected.kind==='condition-export'){
     valid=valid&&m.path===listPath&&q.page===1&&q.pageSize===spec.page_size&&q.stockSku===''&&
       String(q.keytype2)==='1'&&q.orderStatus===(current?0:5)&&same(q[current?'orderTimes':'timeStr'],spec.range)&&
       same((q.shopIdsList||[]).map(String).sort(),[...spec.shop_ids].sort());
     // Completed requests may omit platformId (observed on the real page).
     // Exact authorized TikTok shop IDs are mandatory; XLS platform is rechecked.
     if(current||q.platformId!==undefined)valid=valid&&q.platformId===104;
     if(current)valid=valid&&q.skutype==='5'&&q.originalSku==='';
     const defaults=current?{if_purchased:null,isProcessStock:0,itemType:0,hasGoods:0,myLogisticsChannelIdList:[],isSyncLogistics:-1,orderBy:3,sizetype:'1',idtype:'1',orderId:null,keytype:'1',keyword:null,itemCount:0,ordertype:null}
      :{itemType:0,myLogisticsChannelIdList:[],idtype:'1',orderId:null,keytype:'1',keyword:null,itemCount:0,isSyncErp:0};
     valid=valid&&Object.entries(defaults).every(([k,v])=>same(q[k],v));
     if(!current&&expected.kind==='list')valid=valid&&same(q.stockName,'')&&same(q.ordertype,null)&&same(q.platformOrderStatusList,[])&&q.warehouseId===0&&q.if_sorting===-1;
     const keys=new Set([...Object.keys(defaults),'page','pageSize',current?'orderTimes':'timeStr','stockSku','shopIdsList','keytype2','orderStatus',
       'platformId',...(current?['skutype','originalSku']:['stockName','ordertype','platformOrderStatusList','warehouseId','if_sorting']),
       ...(expected.kind==='condition-export'?['type','orderValueArr','itemValueArr']:[])]);
     valid=valid&&Object.keys(q).every(k=>keys.has(k));
     if(expected.kind==='condition-export'){
      const v=state.vm(),complete=Number.isInteger(v.totalNum)&&v.totalNum>=0&&v.totalNum<=spec.max_export_rows&&
       (spec.reconciliation_enabled||v.totalNum===expected.export_count);
      if(!complete){state.result={error:'EXPORT_SOURCE_CHANGED',details:{stage:'export-request',reason:'list-drift',
       expected_total:expected.export_count,actual_total:v.totalNum,actual_order_id_count:v.orderTaskList.length}};
       throw Error('ORDER_PULSE_SCOPE_GUARD');}
      valid=valid&&q.type===1;
      if(valid){state.exportedCount=v.totalNum;state.reconciliationRequired=state.reconciliationRequired||v.totalNum!==expected.export_count;}
     }
    }else{
     const v=state.vm(),currentIds=v.orderTaskList.map(x=>String(x.id)).sort();
     const orderIds=v.orderTaskList.map(x=>x.orderId).sort();
     const complete=Number.isInteger(v.totalNum)&&v.totalNum>=0&&v.totalNum<=spec.max_export_rows&&
       (!current||v.orderTaskList.length===v.totalNum)&&
       (spec.reconciliation_enabled||(v.totalNum===expected.export_count&&same(orderIds,expected.order_ids)));
     if(!complete){state.result={error:'EXPORT_SOURCE_CHANGED',details:{stage:'export-request',reason:'list-drift',
      expected_total:expected.export_count,actual_total:v.totalNum,actual_order_id_count:v.orderTaskList.length}};
      throw Error('ORDER_PULSE_SCOPE_GUARD');}
     valid=valid&&m.path==='/tmsapi/exportOrder'&&current&&q.source===1&&q.ischecked===false&&
       same([...q.idsList||[]].map(String).sort(),currentIds)&&q.idsList.length===currentIds.length;
     if(valid){state.exportedCount=v.totalNum;state.exportedOrderIds=orderIds;
      state.reconciliationRequired=state.reconciliationRequired||v.totalNum!==expected.export_count;}
    }
    if(expected.kind!=='list')valid=valid&&same(q.orderValueArr,state.orderColumns)&&same(q.itemValueArr,state.itemColumns)&&!q.ifMerge;
    if(!valid){state.result={error:'REQUEST_SCOPE_MISMATCH',details:{path:m.path,kind:expected.kind,keys:Object.keys(q),
      page:q.page,pageSize:q.pageSize,platformId:q.platformId,keytype2:q.keytype2,orderStatus:q.orderStatus,
      range:q[current?'orderTimes':'timeStr'],shop_count:q.shopIdsList?.length,stockSku_empty:q.stockSku==='',
      ids_count:q.idsList?.length,defaults:Object.fromEntries(['itemType','isSyncErp','itemCount','orderId','keyword','keytype','idtype'].map(k=>[k,q[k]==null?q[k]:['orderId','keyword'].includes(k)?'nonempty':q[k]]))}};throw Error('ORDER_PULSE_SCOPE_GUARD');}
    state.expected=null;
    state.inflight.add(this);
    const started=Date.now();
    this.addEventListener('loadend',()=>{
     state.inflight.delete(this);
     if(window.__pulseExport!==state)return;
     if(this.status!==200){state.result={error:({401:'AUTH_REQUIRED',403:'PERMISSION_DENIED',429:'RATE_LIMITED'})[this.status]||'EXPORT_HTTP_FAILED',retry_after:this.getResponseHeader('Retry-After')};return;}
     try{
      const r=JSON.parse(this.responseText), empty=String(r.code)==='2'&&r.msg==='没有满足条件的数据'&&r.totalNum===0;
      if(String(r.code)!=='200'&&!empty){const code=String(r.code),message=String(r.msg||''),cooldown=code==='1000'&&message==='2分钟内不允许多次下载,请稍后再试',processing=code==='1000'&&message==='在上次处理未完结之前,不要频繁操作',limited=cooldown||processing;state.result={error:limited?'RATE_LIMITED':({'401':'AUTH_REQUIRED','403':'PERMISSION_DENIED','429':'RATE_LIMITED'})[code]||'EXPORT_BUSINESS_FAILED',retry_after:limited?'120':undefined};return;}
      if(expected.kind==='list'){
       const rows=empty?[]:r.datas;
       if(!Array.isArray(rows))throw Error('SOURCE_CHANGED');
       state.result={total:r.totalNum,loaded:rows.length,page:q.page,page_size:q.pageSize,
        order_ids:rows.map(x=>{if(typeof x.orderId!=='string')throw Error('EXPORT_NUMERIC_ID');return x.orderId;}),
        list_check:rows.map(x=>({order_number:x.orderId,shop_name:x.shopName,paid_at:x.orderTime,
          amount_rmb:x.orderFee,items:(x.itemList||[]).map(i=>({stock_sku:i.stockSku,quantity:i.productNum}))})),
        request:{partition:spec.partition,range:spec.range,platform_id:q.platformId,shop_count:q.shopIdsList.length,page_size:q.pageSize},elapsed_ms:Date.now()-started};
      }else{
       const url=new URL(r.pop);if(url.origin!=='https://tms-cos.mabangerp.com'||!url.pathname.endsWith('.xls'))throw Error('EXPORT_URL_INVALID');
       state.result={url:url.href,request:{partition:spec.partition,range:spec.range,shop_count:q.shopIdsList?.length,
         selected_count:q.idsList?.length,source:q.source,type:q.type,order_columns:q.orderValueArr,item_columns:q.itemValueArr},elapsed_ms:Date.now()-started};
      }
     }catch(e){state.result={error:e.message.startsWith('EXPORT_')?e.message:'SOURCE_CHANGED'};}
    },{once:true});
   }
   return send.call(this,body);
  };
 },spec);installed=true;
 stage='page-navigation';
 const url=origin+'/#/'+routes[spec.partition];
 if(await page.url()!==url)await page.goto(url);
 stage='page-ready';
 await page.waitForFunction(()=>{try{return window.__pulseExport.vm().shopList.length>0}catch{return false}},undefined,{timeout:10000});
 stage='identity';
 await page.evaluate(()=>window.__pulseExport.identity());
 if(spec.action==='query'){
  stage='query-submit';
  if(spec.partition==='current'&&!await page.evaluate(()=>!!document.querySelector('.el-drawer__container.el-drawer__open')))await page.click('button:has-text("高级查询")');
  await page.evaluate(spec=>{
   const state=window.__pulseExport,v=state.vm(),current=spec.partition==='current';
   const filters=current?{skutype:'5',orderStatus:0,orderId:null,keyword:null,keytype:'1',stockSku:'',originalSku:'',companyId:null,itemType:0,countryCodes:[],hasGoods:0,orderby:'orderTime',itemCount:0,idtype:'1',shopIdsList:spec.shop_ids.map(Number),orderTimes:spec.range,isSyncLogistics:-1,myLogisticsChannelIdList:null,platformid:104,warehouseId:'',if_purchased:null,orderBy:3,ordertype:null,isProcessStock:0,customLabelIdsList:[],orderWeight1:null,orderWeight2:null,volume1:null,volume2:null,sizetype:'1',size1:null,size2:null,itemCount1:null,itemCount2:null,itemCount3:null,itemCount4:null,shopManager:null}
    :{orderStatus:5,idtype:'1',myLogisticsChannelIdList:[],orderId:null,keyword:null,keytype:'1',keytype2:'1',stockSku:'',stockName:'',itemType:0,countryCodes:[],orderby:'orderTime',itemCount:0,countryCode:null,shopIdsList:spec.shop_ids.map(Number),orderTime:spec.range,isSyncErp:0,isScan:null,is_online:-1,ordertype:null,status:null,platformOrderStatusList:[],customLabelIdsList:[]};
   if(Object.keys(filters).some(k=>!(k in v.search)))throw Error('SOURCE_CHANGED');
   Object.assign(v.search,filters);
   v.pageSize=spec.page_size;v.page=1;v.checkTaskId=[];
   state.result=null;state.expected={kind:'list'};
   const area=current?document.querySelector('.demo-drawer'):document;
   const button=[...area.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='搜索');
   if(!button)throw Error('SOURCE_CHANGED');button.click();
  },spec);
  stage='query-response';
  try{await page.waitForFunction(()=>!!window.__pulseExport?.result,undefined,{timeout:60000})}
  catch{throw Error('EXPORT_TIMEOUT')}
  result=await page.evaluate(()=>window.__pulseExport.result);
 }else if(spec.action==='export'){
  stage='export-precheck';
  const precheck=await page.evaluate(spec=>{
   const v=window.__pulseExport.vm();
   const ids=v.orderTaskList.map(x=>x.orderId).sort();
   const idsMatch=JSON.stringify(ids)===JSON.stringify([...spec.expected_order_ids].sort());
   return {valid:v.totalNum===spec.expected_total&&v.page===1&&v.pageSize===spec.page_size&&idsMatch,
    actual_total:v.totalNum,actual_page:v.page,actual_page_size:v.pageSize,
    actual_order_ids:ids,actual_order_id_count:ids.length,
    expected_order_id_count:spec.expected_order_ids.length,ids_match:idsMatch};
  },spec);
  const current=spec.partition==='current';
  const safeLiveState=Number.isInteger(precheck.actual_total)&&precheck.actual_total>=0&&
   precheck.actual_total<=spec.max_export_rows&&precheck.actual_page===1&&precheck.actual_page_size===spec.page_size&&
   (!current||precheck.actual_order_id_count===precheck.actual_total);
  if(!precheck.valid&&(!spec.reconciliation_enabled||!safeLiveState)){
   result={error:'EXPORT_SOURCE_CHANGED',details:{stage:'export-precheck',reason:'list-drift',
    expected_total:spec.expected_total,actual_total:precheck.actual_total,expected_page:1,actual_page:precheck.actual_page,
    expected_page_size:spec.page_size,actual_page_size:precheck.actual_page_size,
    expected_order_id_count:precheck.expected_order_id_count,actual_order_id_count:precheck.actual_order_id_count,
    ids_match:precheck.ids_match}};
   throw Error('EXPORT_SOURCE_CHANGED');
  }
  const exportContext={exported_count:precheck.actual_total,
   exported_order_ids:current?precheck.actual_order_ids:spec.expected_order_ids,
   reconciliation_required:!precheck.valid};
  // Let Vue remove the drawer before opening the normal batch menu.
  stage='export-menu';
  if(await page.evaluate(()=>!!document.querySelector('.el-drawer__container.el-drawer__open')))await page.press('.demo-drawer','Escape');
  await page.waitForFunction(()=>!document.querySelector('.el-drawer__container.el-drawer__open'),undefined,{timeout:3000});
  if(spec.partition==='current')await page.evaluate(()=>{
   const v=window.__pulseExport.vm();
   const box=[...document.querySelectorAll('li.text-center input[type="checkbox"]')].find(x=>x.getClientRects().length);
   if(!box)throw Error('SOURCE_CHANGED');if(box.checked)box.click();box.click();
   if(v.checkTaskId.length!==v.totalNum)throw Error('EXPORT_SELECTION_INCOMPLETE');
  });
  const menu=await page.evaluate(()=>[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='批量操作')?.getAttribute('aria-controls'));
  if(!menu)throw Error('SOURCE_CHANGED');
  await page.hover('button[aria-controls="'+menu+'"]');
  await page.evaluate(({menu,current})=>{
   const text=current?'批量导出':'批量导出满足搜索条件的订单';
   const item=[...document.querySelectorAll('#'+menu+' li')].find(x=>x.innerText.trim()===text);
   if(!item)throw Error('SOURCE_CHANGED');item.click();
  },{menu,current:spec.partition==='current'});
  stage='export-dialog';
  await page.waitForFunction(()=>[...document.querySelectorAll('.el-dialog__wrapper')].some(x=>x.getClientRects().length&&x.innerText.includes('导出')),undefined,{timeout:8000});
  await page.evaluate(()=>{const v=window.__pulseExport.vm();v.ExportOrderForm.templateId=null;v.ExportOrderForm.orderValueArr=[];v.ExportOrderForm.itemValueArr=[];});
  await page.evaluate(()=>{
   const input=[...document.querySelectorAll('.el-dialog__wrapper input[placeholder="请选择"]')].find(x=>x.getClientRects().length);
   if(!input)throw Error('EXPORT_TEMPLATE_MISSING');input.click();
  });
  await page.waitForFunction(()=>[...document.querySelectorAll('.el-select-dropdown__item')].some(x=>x.getClientRects().length&&x.innerText.trim()==='1'),undefined,{timeout:6000});
  await page.evaluate(()=>[...document.querySelectorAll('.el-select-dropdown__item')].find(x=>x.getClientRects().length&&x.innerText.trim()==='1').click());
  await page.waitForFunction(()=>window.__pulseExport.vm().ExportOrderForm.orderValueArr?.length>0,undefined,{timeout:5000});
  // Discover runtime field definitions. Set only already observed keys; do not
  // save or alter server template 1. Original/FX fields are evidence candidates.
  const fields=await page.evaluate(()=>{
   const v=window.__pulseExport.vm();return {definitions:v.excelOrderColumns,form:v.ExportOrderForm};
  });
  const baseOrders=['orderFee','orderTime','shopName','orderId','originalOrderId','trackNumber'];
  const baseItems=['stockSku','platformSku','productNum','stockName'];
  if(JSON.stringify(fields.form.orderValueArr)!==JSON.stringify(baseOrders)||JSON.stringify(fields.form.itemValueArr)!==JSON.stringify(baseItems))throw Error('EXPORT_TEMPLATE_CHANGED');
  // Key-to-label contract comes from actual field definitions; a mismatch stops.
  const definitions=Array.isArray(fields.definitions)?fields.definitions:[];
  const extra=['currencyId','platformid','itemTotalOrigin','currencyRate'];
  const labels={currencyId:'币种',platformid:'平台类型',itemTotalOrigin:'原始订单金额',currencyRate:'执行汇率'};
  for(const key of extra)if(!definitions.some(d=>d.value===key&&d.name===labels[key]))throw Error('EXPORT_FIELD_UNVERIFIED');
  await page.evaluate(({orderColumns,itemColumns})=>{
   const state=window.__pulseExport,v=state.vm();
   state.orderColumns=orderColumns;state.itemColumns=itemColumns;
   v.ExportOrderForm.orderValueArr=[...orderColumns];v.ExportOrderForm.itemValueArr=[...itemColumns];
   v.ExportOrderForm.ifSave=false;v.ExportOrderForm.ifMerge=false;
  },{orderColumns:[...baseOrders,...extra],itemColumns:baseItems});
  // Suppression must be installed in the SAME page operation as the export click.
  stage='export-submit';
  await page.evaluate(({spec,exportContext})=>{
   const state=window.__pulseExport;state.result=null;state.captured=null;state.suppressed=0;
   state.exportedCount=exportContext.exported_count;state.exportedOrderIds=exportContext.exported_order_ids;
   state.reconciliationRequired=exportContext.reconciliation_required;
   state.expected={kind:spec.partition==='current'?'id-export':'condition-export',export_count:exportContext.exported_count,
    order_ids:exportContext.exported_order_ids};
   const nativeOpen=window.open;state.restoreDownload=()=>{window.open=nativeOpen;};
   window.open=function(url,...args){
    if(typeof url==='string'){let u;try{u=new URL(url,location.href)}catch{}
     if(u?.origin==='https://tms-cos.mabangerp.com'&&u.pathname.endsWith('.xls')){state.captured=u.href;state.suppressed++;return null;}}
    state.result={error:'EXPORT_URL_INVALID'};return null;
   };
   const b=[...document.querySelectorAll('.el-dialog__wrapper button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='导出');
   if(!b)throw Error('SOURCE_CHANGED');b.click();
  },{spec,exportContext});
  stage='export-response';
  await page.waitForFunction(()=>{const s=window.__pulseExport;return s?.result&&(s.result.error||s.captured);},undefined,{timeout:60000});
  result=await page.evaluate(()=>{const s=window.__pulseExport;return {...s.result,suppressed_downloads:s.suppressed,
   capture_matches:s.captured===s.result?.url,exported_count:s.exportedCount,
   exported_order_ids:s.exportedOrderIds,reconciliation_required:s.reconciliationRequired};});
  if(!result.error&&(!result.capture_matches||result.suppressed_downloads!==1))throw Error('EXPORT_DOWNLOAD_NOT_CONTROLLED');
  result.field_definitions=definitions.filter(d=>extra.includes(d.value));
 }
 }
}catch(e){
 const known=/^(EXPORT_[A-Z_]+|WAITING_USER|WAITING_CONNECTION|AUTH_REQUIRED|PERMISSION_DENIED|RATE_LIMITED|SOURCE_CHANGED|ACCOUNT_SCOPE_CHANGED|CATALOG_CHANGED|BROWSER_BUSY|REQUEST_SCOPE_MISMATCH)$/;
 if(!result?.error)result={error:known.test(e.message)?e.message:'EXPORT_TRANSPORT_FAILED',details:{stage}};
 else if(!result.details)result.details={stage};
}finally{
 if(installed){
  try{
   const entry=(await listTaskSpaces()).find(s=>s.id===spec.space_id);
   if(entry?.ownership==='agent'){
    await page.evaluate(close=>{
     window.__pulseExport?.restore();
     if(close){const b=[...document.querySelectorAll('.el-dialog__wrapper button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='取消');b?.click();}
    },spec.action==='export');
    // Export pages can hold hundreds of reactive rows. Reset the dedicated tab
    // before a source-enforced cooldown so the next isolated action starts from
    // a clean document instead of retaining a high-memory DOM for two minutes.
    if(spec.action==='export'||result?.error==='EXPORT_TIMEOUT')await page.reload();
   }
   else if(!result?.error)result={error:'WAITING_USER'};
  }catch{if(!result?.error)result={error:'EXPORT_UI_CLEANUP_FAILED'};}
 }
}
console.log('ORDER_PULSE_EXPORT:'+JSON.stringify(result||{error:'EXPORT_TRANSPORT_FAILED'}));
