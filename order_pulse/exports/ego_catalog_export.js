// Product Catalog export through normal TMS UI actions. No request replay.
const spec=INPUT,origin='https://tms.mabangerp.com';
let task,page,installed=false,originalUrl,result;
const known=/^(WAITING_USER|WAITING_CONNECTION|AUTH_REQUIRED|ACCOUNT_SCOPE_CHANGED|BROWSER_BUSY|CATALOG_SOURCE_CHANGED|CATALOG_REQUEST_SCOPE|CATALOG_EXPORT_FAILED|CATALOG_EXPORT_INCOMPLETE|CATALOG_DOWNLOAD_NOT_CONTROLLED)$/;
try{
 const entry=(await listTaskSpaces()).find(s=>s.id===spec.space_id);
 if(!entry||entry.ownership!=='agent')throw Error('WAITING_USER');
 task=await taskSpace(spec.space_id);const tab=(await task.tabs()).find(t=>t.label===spec.page_label);
 if(!tab||!tab.url.startsWith(origin+'/#/'))throw Error('WAITING_CONNECTION');
 if(tab.url.includes('/#/login'))throw Error('AUTH_REQUIRED');
 page=task.page(spec.page_label);originalUrl=await page.url();
 await page.evaluate(spec=>{
  if(window.__pulseCatalog||window.__pulseExport||window.__tmsMonitor)throw Error('BROWSER_BUSY');
  const state={expected:null,pending:null,captured:null,batches:[],requests:[],suppressed:0,error:null};window.__pulseCatalog=state;
  const id=v=>typeof v==='string'?v:Number.isSafeInteger(v)?String(v):null;
  state.account=()=>{const e=document.querySelector('#app')?.__vue__?.$store?.state?.employee;return e?{employee_id:id(e.id),supplier_id:id(e.supplierId),customer_id:id(e.customerId)}:null};
  state.identity=()=>{const a=state.account();if(!a||Object.keys(spec.account).some(k=>a[k]!==spec.account[k]))throw Error('ACCOUNT_SCOPE_CHANGED')};
  state.productVm=()=>{const input=[...document.querySelectorAll('input')].find(i=>i.value==='SKU精确搜索');let v=input?.closest('.el-input')?.__vue__;for(let i=0;v&&i<12;i++,v=v.$parent)if(Array.isArray(v.stockWarehouseList)&&v.ExportStockwarehouseForm)return v;throw Error('CATALOG_SOURCE_CHANGED')};
  state.comboVm=()=>{const input=[...document.querySelectorAll('input')].find(i=>i.value==='组合SKU');let v=input?.closest('.el-input')?.__vue__;for(let i=0;v&&i<12;i++,v=v.$parent)if(Array.isArray(v.comboSkuDataList)&&v.$options?.methods?.exportComboSku)return v;throw Error('CATALOG_SOURCE_CHANGED')};
  state.reconcile=()=>{const p=state.pending,c=state.captured;if(!p||!c)return;if(c!==p.url){state.error='CATALOG_DOWNLOAD_NOT_CONTROLLED';return}state.suppressed++;state.batches.push({...p,suppressed_downloads:1});state.pending=null;state.captured=null};
  const P=XMLHttpRequest.prototype,open=P.open,send=P.send,meta=new WeakMap(),nativeOpen=window.open;
  state.restore=()=>{clearTimeout(state.timer);P.open=open;P.send=send;window.open=nativeOpen;if(window.__pulseCatalog===state)delete window.__pulseCatalog};
  state.timer=setTimeout(state.restore,Math.min(230000,spec.operation_timeout_ms||230000));
  P.open=function(method,url,...rest){meta.set(this,{method,path:new URL(url,location.href).pathname});return open.call(this,method,url,...rest)};
  P.send=function(body){const m=meta.get(this);if(m&&['/tmsapi/exportStockwarehouse','/tmsapi/exportComboSku'].includes(m.path)){
    const expected=state.expected;if(!expected){state.error='CATALOG_REQUEST_SCOPE';throw Error('ORDER_PULSE_CATALOG_SCOPE')}
    let q;try{q=JSON.parse(body)}catch{state.error='CATALOG_SOURCE_CHANGED';throw Error('ORDER_PULSE_CATALOG_SCOPE')}
    state.identity();const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);let valid=m.method.toUpperCase()==='POST';
    if(expected.kind==='product'){
      const keys=new Set(['startNum','pageSize','idsList','stockValueArr','ischecked','isCombo','warehouseId','classId','orderBys','status','gridproperty','keyword','keytype']);
      valid=valid&&m.path==='/tmsapi/exportStockwarehouse'&&q.isCombo===1&&q.status===1&&q.pageSize===500&&q.ischecked===false&&
       same([...q.idsList||[]].map(String).sort(),[...expected.ids].sort())&&same(q.stockValueArr,spec.product_fields)&&Object.keys(q).every(k=>keys.has(k));
    }else valid=valid&&m.path==='/tmsapi/exportComboSku'&&same(Object.keys(q).sort(),['idsList'])&&same([...q.idsList||[]].map(String).sort(),[...expected.ids].sort());
    if(!valid){state.error='CATALOG_REQUEST_SCOPE';throw Error('ORDER_PULSE_CATALOG_SCOPE')}
    state.requests.push({kind:expected.kind,path:m.path,selected_count:expected.ids.length,page:expected.page});
    this.addEventListener('loadend',()=>{try{
      if(this.status!==200){state.error='CATALOG_EXPORT_FAILED';return}
      const r=JSON.parse(this.responseText);if(String(r.code)!=='200'||!r.pop){state.error='CATALOG_EXPORT_FAILED';return}
      const u=new URL(r.pop);if(u.origin!=='https://tms-cos.mabangerp.com'||!u.pathname.endsWith('.xls')){state.error='CATALOG_EXPORT_FAILED';return}
      state.pending={url:u.href,kind:expected.kind,expected_rows:expected.ids.length,page:expected.page,total:expected.total};state.reconcile();
    }catch{state.error='CATALOG_EXPORT_FAILED'}},{once:true});state.expected=null;
   }return send.call(this,body)};
  window.open=function(url,...args){try{const u=new URL(url,location.href);if(u.origin==='https://tms-cos.mabangerp.com'&&u.pathname.endsWith('.xls')&&!state.captured){state.captured=u.href;state.reconcile();return null}}catch{}state.error='CATALOG_DOWNLOAD_NOT_CONTROLLED';return null};
 },spec);installed=true;
 const owned=async()=>{const x=(await listTaskSpaces()).find(s=>s.id===spec.space_id);if(x?.ownership!=='agent')throw Error('WAITING_USER')};
 const waitBatch=async before=>{await page.waitForFunction(n=>window.__pulseCatalog?.error||window.__pulseCatalog?.batches.length>n,before,{timeout:60000});const error=await page.evaluate(()=>window.__pulseCatalog?.error);if(error)throw Error(error);const ok=await page.evaluate(n=>window.__pulseCatalog.batches.length===n+1&&!window.__pulseCatalog.pending,before);if(!ok)throw Error('CATALOG_EXPORT_FAILED')};
 const clickSearch=async()=>{await page.evaluate(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='搜索');if(!b)throw Error('CATALOG_SOURCE_CHANGED');b.click()})};

 await owned();await page.goto(origin+'/#/myproductlist');
 await page.waitForFunction(()=>{try{return !!window.__pulseCatalog.productVm()}catch{return false}},undefined,{timeout:10000});
 await page.evaluate(()=>{const s=window.__pulseCatalog,v=s.productVm();s.identity();Object.assign(v.Search,{warehouseId:'',keyword:'',keytype:'7',orderBys:'2',gridproperty:-1,classId:null,status:1,provider:'',if_produce:'-1',stockQuantitytype:-1,remarkLike:null,providerId:''});v.pageSize=500;v.pageIndex=1;v.checkStockwarehouseId=[];});
 await clickSearch();await page.waitForFunction(()=>{const v=window.__pulseCatalog.productVm();return v.pageSize===500&&Number.isInteger(v.totalNum)&&v.totalNum>0&&v.stockWarehouseList.length===Math.min(500,v.totalNum)},undefined,{timeout:15000});
 const productTotal=await page.evaluate(()=>window.__pulseCatalog.productVm().totalNum),productPages=Math.ceil(productTotal/500);
 for(let n=1;n<=productPages;n++){
  await owned();if(n>1){await page.evaluate(()=>document.querySelector('.el-pagination .btn-next:not([disabled])')?.click());await page.waitForFunction(({page,expected})=>{const v=window.__pulseCatalog.productVm();return v.pageIndex===page&&v.stockWarehouseList.length===expected},{page:n,expected:Math.min(500,productTotal-(n-1)*500)},{timeout:15000})}
  const count=await page.evaluate(()=>window.__pulseCatalog.productVm().stockWarehouseList.length);const expected=Math.min(500,productTotal-(n-1)*500);if(count!==expected)throw Error('CATALOG_EXPORT_INCOMPLETE');
  await page.evaluate(()=>{const s=window.__pulseCatalog,v=s.productVm(),box=[...document.querySelectorAll('input[type=checkbox]')].find(x=>x.getClientRects().length);if(!box)throw Error('CATALOG_SOURCE_CHANGED');if(box.checked)box.click();box.click()});
  await page.waitForFunction(()=>{const v=window.__pulseCatalog.productVm();return v.checkStockwarehouseId.length===v.stockWarehouseList.length},undefined,{timeout:5000});
  const menu=await page.evaluate(()=>[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='批量操作')?.getAttribute('aria-controls'));if(!menu)throw Error('CATALOG_SOURCE_CHANGED');await page.hover('button[aria-controls="'+menu+'"]');await page.evaluate(id=>{const x=[...document.querySelectorAll('#'+id+' li')].find(e=>e.innerText.trim()==='批量导出');if(!x)throw Error('CATALOG_SOURCE_CHANGED');x.click()},menu);
  await page.waitForFunction(()=>[...document.querySelectorAll('.el-dialog__wrapper')].some(x=>x.getClientRects().length&&x.innerText.startsWith('批量导出商品')),undefined,{timeout:6000});
  const before=await page.evaluate(({n,total,fields})=>{const s=window.__pulseCatalog,v=s.productVm();v.ExportStockwarehouseForm.stockValueArr=[...fields];v.ExportStockwarehouseForm.ifSave=false;const ids=v.exportStockwarehouseObj.idsList.map(String);if(ids.length!==v.stockWarehouseList.length)throw Error('CATALOG_EXPORT_INCOMPLETE');s.expected={kind:'product',ids,page:n,total};return s.batches.length},{n,total:productTotal,fields:spec.product_fields});
  await page.evaluate(()=>{const d=[...document.querySelectorAll('.el-dialog__wrapper')].find(x=>x.getClientRects().length&&x.innerText.startsWith('批量导出商品'));const b=[...d.querySelectorAll('button')].find(x=>x.innerText.trim()==='导出');if(!b)throw Error('CATALOG_SOURCE_CHANGED');b.click()});await waitBatch(before);
 }

 await owned();await page.goto(origin+'/#/myComboSku');await page.waitForFunction(()=>{try{return !!window.__pulseCatalog.comboVm()}catch{return false}},undefined,{timeout:10000});
 await page.evaluate(()=>{const s=window.__pulseCatalog,v=s.comboVm();s.identity();Object.assign(v.Search,{keyword:'',keytype:'1',orderBys:'1',status:1});v.page=1;v.checkComboSkuId=[];v.handleSizeChange(500)});
 await page.waitForFunction(()=>{const v=window.__pulseCatalog.comboVm();return v.pageSize===500&&Number.isInteger(v.totalNum)&&v.totalNum>0&&v.comboSkuDataList.length===Math.min(500,v.totalNum)},undefined,{timeout:15000});
 const comboTotal=await page.evaluate(()=>window.__pulseCatalog.comboVm().totalNum),comboPages=Math.ceil(comboTotal/500);
 for(let n=1;n<=comboPages;n++){
  await owned();if(n>1){await page.evaluate(()=>document.querySelector('.el-pagination .btn-next:not([disabled])')?.click());await page.waitForFunction(({page,expected})=>{const v=window.__pulseCatalog.comboVm();return v.page===page&&v.comboSkuDataList.length===expected},{page:n,expected:Math.min(500,comboTotal-(n-1)*500)},{timeout:15000})}
  const count=await page.evaluate(()=>window.__pulseCatalog.comboVm().comboSkuDataList.length),expected=Math.min(500,comboTotal-(n-1)*500);if(count!==expected)throw Error('CATALOG_EXPORT_INCOMPLETE');
  await page.evaluate(()=>{const s=window.__pulseCatalog,v=s.comboVm(),box=[...document.querySelectorAll('input[type=checkbox]')].find(x=>x.getClientRects().length);if(!box)throw Error('CATALOG_SOURCE_CHANGED');if(box.checked)box.click();box.click()});
  await page.waitForFunction(()=>{const v=window.__pulseCatalog.comboVm();return v.checkComboSkuId.length===v.comboSkuDataList.length},undefined,{timeout:5000});
  const before=await page.evaluate(({n,total})=>{const s=window.__pulseCatalog,v=s.comboVm();s.expected={kind:'combo',ids:v.checkComboSkuId.map(String),page:n,total};return s.batches.length},{n,total:comboTotal});
  const menu=await page.evaluate(()=>[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='批量操作')?.getAttribute('aria-controls'));if(!menu)throw Error('CATALOG_SOURCE_CHANGED');await page.hover('button[aria-controls="'+menu+'"]');await page.evaluate(id=>{const x=[...document.querySelectorAll('#'+id+' li')].find(e=>e.innerText.trim()==='批量导出');if(!x)throw Error('CATALOG_SOURCE_CHANGED');x.click()},menu);await waitBatch(before);
 }
 result=await page.evaluate(({productTotal,comboTotal})=>{const s=window.__pulseCatalog;return {batches:s.batches,requests:s.requests,suppressed_downloads:s.suppressed,product_total:productTotal,product_pages:Math.ceil(productTotal/500),combo_total:comboTotal,combo_pages:Math.ceil(comboTotal/500)}} ,{productTotal,comboTotal});
 if(result.suppressed_downloads!==result.batches.length)throw Error('CATALOG_DOWNLOAD_NOT_CONTROLLED');
}catch(e){result={error:known.test(e.message)?e.message:'CATALOG_EXPORT_FAILED'}}
finally{
 if(installed){try{const entry=(await listTaskSpaces()).find(s=>s.id===spec.space_id);if(entry?.ownership==='agent'){await page.evaluate(()=>window.__pulseCatalog?.restore());if(originalUrl&&await page.url()!==originalUrl)await page.goto(originalUrl)}else if(!result?.error)result={error:'WAITING_USER'}}catch{if(!result?.error)result={error:'CATALOG_EXPORT_FAILED'}}}
}
console.log('ORDER_PULSE_CATALOG:'+JSON.stringify(result||{error:'CATALOG_EXPORT_FAILED'}));
