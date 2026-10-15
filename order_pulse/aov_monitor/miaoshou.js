// Read only normal search responses. No API replay, cookies or buyer fields leave the page.
function sameOrderPage(a,b){
 return Array.isArray(a)&&Array.isArray(b)&&a.length===b.length&&a.every((row,i)=>['package_id','platform_order_id','shop_id'].every(k=>typeof row[k]==='string'&&row[k]===b[i][k]));
}
async function useMiaoshouPageSize(page,size=100){
 const selector='.jx-pagination__sizes .jx-select__wrapper';
 await page.waitForSelector(selector,{timeout:10000});
 const current=await page.evaluate(()=>document.querySelector('.jx-pagination__sizes')?.innerText.trim());
 if(current===size+'条/页')return;
 await page.click(selector);
 await page.click('.jx-select-dropdown__item:has-text("'+size+'条/页")');
 await page.waitForFunction(size=>document.querySelector('.jx-pagination__sizes')?.innerText.trim()===size+'条/页',size,{timeout:5000});
}
const spec=INPUT;let page,installed=false,result,stage='binding';
try{
 const e=(await listTaskSpaces()).find(x=>x.id===spec.space_id);
 if(!e||e.ownership!=='agent')throw Error('WAITING_USER');
 const task=await taskSpace(spec.space_id),tabs=await task.tabs();
 const choices=tabs.filter(t=>t.url.startsWith('https://erp.91miaoshou.com/'));
 if(choices.length!==1)throw Error('MIAOSHOU_PAGE_REQUIRED');
 page=choices[0].label?task.page(choices[0].label):await task.adopt(choices[0].page);
 stage='open';await page.goto('https://erp.91miaoshou.com/order/package/all?appPackageTab=all');await page.reload();
 await page.waitForSelector('button.J_queryFormSearch',{timeout:15000});
 stage='page-size';await useMiaoshouPageSize(page,100);
 stage='observer';await page.evaluate(spec=>{
  if(window.__aovMiaoshou)throw Error('BROWSER_BUSY');
  const P=XMLHttpRequest.prototype,open=P.open,send=P.send,meta=new WeakMap();
  const s={result:null,expected:1,armed:false,inflight:new Set(),closed:false};window.__aovMiaoshou=s;
  s.restore=()=>{clearTimeout(s.timer);s.closed=true;P.open=open;P.send=send;for(const x of s.inflight)if(x.readyState!==4)x.abort();delete window.__aovMiaoshou;document.querySelectorAll('[data-aov-field]').forEach(e=>delete e.dataset.aovField)};
  s.timer=setTimeout(s.restore,180000);
  for(const el of document.querySelectorAll('.jx-form-item')){const l=el.querySelector('label')?.innerText||'';if(l.includes('付款时间'))el.querySelectorAll('input').forEach((i,n)=>i.dataset.aovField='date'+n);if(l.includes('店铺名/别名/ID'))el.querySelector('input').dataset.aovField='shop'}
  P.open=function(m,u,...r){meta.set(this,{method:m,path:new URL(u,location.href).pathname});return open.call(this,m,u,...r)};
  P.send=function(body){const m=meta.get(this);if(s.armed&&m?.path==='/api/order/package/render_list/searchOrderPackageList'){
   const q=Object.fromEntries(new URLSearchParams(body));
   const defaults={pageSize:'100',goodsSkuOuterIdRp:'ss',platformOuterSkuIdRp:'ss',purchaseLogisticsKeywordRp:'eq',logisticsKeywordRp:'eq',platformOrderSnsRp:'eq',appPackageNosRp:'eq',appPackageNos:'',purchaseOrderSnRp:'eq',platformItemNumRp:'eq',warehouseShelfCellCodeRp:'ss',priceType:'profit',remarkRp:'ss',skuSubNameRp:'ss',packageWeighingWeightRp:'g',itemTitleRp:'ss',consigneeZipCodeRp:'ss',sourceItemIdsRp:'eq',platformOrderSns:'',gmtLastDeliveryFrom:'',gmtLastDeliveryTo:'',orderTagsRp:'includeAll',logisticsGroupType:'cascader',source:'orderHistory',appPackageTab:'all',sortField:'gmtOrderStart',sortType:'desc',waitProcessTab:'all',supplierProcessStatus:'all',isLogisticsCompanyGroupMode:'1'};
   const expected={...defaults,page:String(s.expected),gmtPayFrom:spec.range[0],gmtPayTo:spec.range[1],shopKeyword:spec.shop_name};
   if(m.method.toUpperCase()!=='POST'||Object.keys(q).some(k=>!(k in expected))||Object.entries(expected).some(([k,v])=>(q[k]??'')!==v)){s.filter_diff={observed_dates:{start:q.gmtPayFrom,end:q.gmtPayTo},missing:Object.entries(expected).filter(([k,v])=>(q[k]??'')!==v).map(([k])=>k),extra:Object.keys(q).filter(k=>!(k in expected))};s.result={error:'MIAOSHOU_FILTER_UNVERIFIED'};throw Error('AOV_SCOPE')}
   s.inflight.add(this);this.addEventListener('loadend',()=>{s.inflight.delete(this);if(s.closed)return;try{if(this.status!==200)throw Error('MIAOSHOU_SOURCE_FAILED');
    const r=JSON.parse(this.responseText);if(!Array.isArray(r.packageList)||!Number.isSafeInteger(r.total)||r.total<0||Number(r.page)!==s.expected||Number(r.pageSize)!==100)throw Error('MIAOSHOU_SCHEMA_CHANGED');
    const rows=r.packageList.map(o=>{const a=o.orderInfo,d=o.orderPackageAmountDetail;if(!a||!d||typeof o.platformOrderSn!=='string'||typeof o.shopId!=='string')throw Error('MIAOSHOU_ID_UNVERIFIED');return {platform_order_id:o.platformOrderSn,shop_id:o.shopId,shop_name:o.shopName,platform:o.platform,paid_at:a.gmtPay,status:a.platformOrderStatus,refund_status:a.appRefundStatus,refund_amount:a.refundAmount,pay_amount:a.payAmount,currency:a.currency,order_amount:a.orderAmount,exchange_rate:a.exchangeRate,cny_total:d.CNYOrderAmount,split:o.isSplit,skus:Object.values(o.items||{}).map(i=>i.platformOuterSkuId),package_id:o.opOrderPackageId};});
    s.result={rows,total:r.total,page:Number(r.page)};
   }catch(e){s.result={error:/^MIAOSHOU_/.test(e.message)?e.message:'MIAOSHOU_SCHEMA_CHANGED'}}},{once:true});
  }return send.call(this,body)};
 },spec);installed=true;
 stage='filters';
 // Apply the controlled range as one UI update: editing the start/end separately
 // can reject an intermediate range and silently restore the previous dates.
 await page.evaluate(range=>{
  const target=document.querySelector('[data-aov-field="date0"]'),root=[...document.querySelectorAll('*')].find(e=>e.__vue_app__);let found=null;const seen=new Set();
  function owns(v,d=0){if(!v||d>30)return false;if(v.el?.nodeType===1&&v.el.contains(target))return true;if(v.component&&owns(v.component.subTree,d+1))return true;return Array.isArray(v.children)&&v.children.some(x=>typeof x==='object'&&owns(x,d+1))}
  function walk(v,d=0){if(!v||d>60||seen.has(v)||found)return;seen.add(v);const c=v.component;if(c){if(c.type.name==='ProDatePicker'&&owns(c.subTree))found=c;walk(c.subTree,d+1)}if(Array.isArray(v.children))for(const x of v.children)if(typeof x==='object')walk(x,d+1)}walk(root?._vnode);
  const update=found?.vnode.props?.['onUpdate:modelValue'];if(typeof update!=='function')throw Error('MIAOSHOU_DATE_CONTROL_SCHEMA_CHANGED');update([...range]);
 },spec.range);
 await page.waitForFunction(range=>{const item=[...document.querySelectorAll('.jx-form-item')].find(e=>(e.querySelector('label')?.innerText||'').includes('付款时间'));return range.every((v,n)=>item?.querySelectorAll('input')[n]?.value===v)},spec.range,{timeout:5000});
 await page.evaluate(()=>{const item=[...document.querySelectorAll('.jx-form-item')].find(e=>(e.querySelector('label')?.innerText||'').includes('店铺名/别名/ID'));if(item?.querySelector('input'))item.querySelector('input').dataset.aovField='shop'});
 await page.fill('[data-aov-field="shop"]',spec.shop_name);
 stage='pages';const pages=[];let total=null,seen=new Set();
 for(let n=1;n<=300;n++){
  await page.evaluate(n=>{const s=window.__aovMiaoshou;s.result=null;s.expected=n;s.armed=true},n);
  if(n===1)await page.click('button.J_queryFormSearch');else await page.click('button.btn-next');
  await page.waitForFunction(()=>!!window.__aovMiaoshou?.result,undefined,{timeout:15000});
  const v=await page.evaluate(()=>window.__aovMiaoshou.result);if(v.error)throw Error(v.error);
  if(v.total>1600&&spec.orders_only)throw Error('MIAOSHOU_SLICE_REQUIRED');
  if(total!==null&&total!==v.total){const e=Error('MIAOSHOU_COVERAGE_CHANGED');e.details={reason:'TOTAL_CHANGED',first_total:total,last_total:v.total};throw e}total=v.total;
  if(v.page!==n||(n*100<total&&v.rows.length!==100))throw Error('MIAOSHOU_PAGINATION_INCOMPLETE');
  for(const row of v.rows){if(seen.has(row.package_id))throw Error('MIAOSHOU_PAGE_REPEATED');seen.add(row.package_id)}pages.push(v);
  if(seen.size>=total){if(seen.size!==total)throw Error('MIAOSHOU_PAGINATION_INCOMPLETE');break}
  if(n===300)throw Error('MIAOSHOU_LIMIT_REACHED');
 }
 // Recheck frozen range; do not silently accept list drift.
 await page.evaluate(()=>{window.__aovMiaoshou.expected=1;window.__aovMiaoshou.result=null});await page.click('button.J_queryFormSearch');await page.waitForFunction(()=>!!window.__aovMiaoshou?.result,undefined,{timeout:15000});const last=await page.evaluate(()=>window.__aovMiaoshou.result);
 if(last.error)throw Error(last.error);
 if(last.total!==total||!sameOrderPage(last.rows,pages[0].rows)){const e=Error('MIAOSHOU_COVERAGE_CHANGED');e.details={reason:last.total!==total?'TOTAL_CHANGED':'PAGE_IDENTITIES_CHANGED',first_total:total,last_total:last.total};throw e}
 await page.evaluate(()=>window.__aovMiaoshou.restore());installed=false;
 let refunds=null;if(!spec.orders_only){stage='shop-health';await verifyMiaoshouShop(page,spec);stage='refunds';refunds=await readMiaoshouRefunds(page,spec)}
 result={coverage:'COMPLETE',rows:pages.flatMap(p=>p.rows),pages:pages.length,page_size:100,total,refunds,sync_health:'VERIFIED'};
 await page.goto('https://erp.91miaoshou.com/order/package/all?appPackageTab=all');
}catch(e){const filter_diff=installed?await page.evaluate(()=>window.__aovMiaoshou?.filter_diff).catch(()=>null):null;result={coverage:'INCOMPLETE',stage,filter_diff,error_kind:e.name,diagnostic:e.details,error:/^(MIAOSHOU_|WAITING_|BROWSER_)/.test(e.message)?e.message:'MIAOSHOU_SOURCE_FAILED'}}
finally{if(installed)try{const entry=(await listTaskSpaces()).find(x=>x.id===spec.space_id);if(entry?.ownership==='agent')await page.evaluate(()=>window.__aovMiaoshou?.restore());else result={coverage:'INCOMPLETE',error:'WAITING_USER'}}catch{result={coverage:'INCOMPLETE',error:'MIAOSHOU_CLEANUP_FAILED'}}}
console.log('AOV_SOURCE:'+JSON.stringify(result));
