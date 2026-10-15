// Autonomous, read-only UI adapter. No private HTTP replay or credentials.
const spec=INPUT;
const origin='https://tms.mabangerp.com';
const routes={current:'myorderlist',completed:'myorderhistorylist'};
const allowedErrors=['WAITING_USER','WAITING_CONNECTION','AUTH_REQUIRED','PERMISSION_DENIED','RATE_LIMITED','SOURCE_CHANGED','QUERY_FAILED','ACCOUNT_SCOPE_CHANGED','CATALOG_CHANGED','SCOPE_TOO_LARGE','REQUEST_SCOPE_MISMATCH','INCOMPLETE_PAGINATION','SNAPSHOT_CHANGED','BROWSER_BUSY','UI_CLEANUP_FAILED'];
let page,task,installed=false,before,lastApplied,output;
try {
  const entry=(await listTaskSpaces()).find(s=>s.id===spec.space_id);
  if(!entry||entry.ownership!=='agent')throw Error('WAITING_USER');
  task=await taskSpace(spec.space_id);
  const tab=(await task.tabs()).find(t=>t.label===spec.page_label);
  if(!tab||!Object.values(routes).some(r=>tab.url===origin+'/#/'+r))throw Error('WAITING_CONNECTION');
  page=task.page(spec.page_label);
  await page.evaluate((includeItems)=>{
    if(window.__tmsMonitor||window.__tmsBQuery)throw Error('BROWSER_BUSY');
    const P=XMLHttpRequest.prototype;const originalOpen=P.open,originalSend=P.send;
    const state={expected:null,result:null,requests:0,closed:false};window.__tmsMonitor=state;
    let timer;
    state.restore=()=>{clearTimeout(timer);if(window.__tmsMonitor!==state)return;state.closed=true;P.open=originalOpen;P.send=originalSend;delete window.__tmsMonitor;};
    timer=setTimeout(state.restore,230000);
    state.vm=()=>{
      const input=location.hash==='#/myorderlist'?document.querySelector('.demo-drawer input[placeholder="SKU模糊搜索"]'):document.querySelector('input[placeholder="SKU精确搜索"]');
      let v=input?.closest('.el-input')?.__vue__;
      for(let i=0;v&&i<12;i++,v=v.$parent)if(v.$data?.search&&Array.isArray(v.$data.shopList))return v;
      throw Error('SOURCE_CHANGED');
    };
    const id=x=>typeof x==='string'?x:Number.isSafeInteger(x)?String(x):null;
    state.account=()=>{const e=document.querySelector('#app')?.__vue__?.$store?.state?.employee;return e?{employee_id:id(e.id),supplier_id:id(e.supplierId),customer_id:id(e.customerId)}:null;};
    state.catalog=()=>state.vm().shopList.filter(s=>s.platformId===104).map(s=>({shop_id:id(s.id),name:s.name,platform_id:s.platformId,company_id:id(s.companyId)}));
    state.capture=()=>{const v=state.vm();return {url:location.href,search:JSON.parse(JSON.stringify(v.search)),pageSize:v.pageSize,drawer:!!v.drawer};};
    const meta=new WeakMap();
    P.open=function(method,url,...args){meta.set(this,{method,url:new URL(url,location.href).href});return originalOpen.call(this,method,url,...args);};
    P.send=function(body){
      const m=meta.get(this);
      if(m&&['https://tms.mabangerp.com/tmsapi/findOrderlist','https://tms.mabangerp.com/tmsapi/findOrderHistorylist'].includes(m.url)){
        // Navigation or edits may auto-submit an empty/default query. Block it
        // before transmission; only our subsequent Search click gets a permit.
        const x=state.expected;
        if(!x)throw Error('TMS_PREPARING_SCOPE');
        let q;try{q=JSON.parse(body)}catch{state.result={error:'SOURCE_CHANGED'};throw Error('TMS_SCOPE_GUARD')}
        const current=x.partition==='current';
        const mandatory={page:x.page,pageSize:50,stockSku:x.sku,shopIdsList:x.shop_ids.map(Number),keytype2:current?1:'1',orderStatus:current?0:5};
        mandatory[current?'orderTimes':'timeStr']=x.range;
        if(current)Object.assign(mandatory,{platformId:104,skutype:'5'});
        const defaults=current?{if_purchased:null,isProcessStock:0,itemType:0,hasGoods:0,myLogisticsChannelIdList:[],isSyncLogistics:-1,orderBy:3,sizetype:'1',idtype:'1',orderId:null,originalSku:'',keytype:'1',keyword:null,itemCount:0,ordertype:null}
          :{itemType:0,myLogisticsChannelIdList:[],idtype:'1',orderId:null,stockName:'',keytype:'1',keyword:null,itemCount:0,ordertype:null,platformOrderStatusList:[],isSyncErp:0,warehouseId:0,if_sorting:-1};
        const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
        const shops=Array.isArray(q.shopIdsList)?q.shopIdsList.map(String).sort():[];
        const valid=m.method.toUpperCase()==='POST'&&m.url===originPath(current)&&state.account()&&Object.keys(x.account).every(k=>state.account()[k]===x.account[k])&&same(shops,[...x.shop_ids].sort())&&
          Object.entries(mandatory).every(([k,v])=>k==='shopIdsList'||same(q[k],v))&&
          Object.entries(defaults).every(([k,v])=>same(q[k],v))&&Object.keys(q).every(k=>k in mandatory||k in defaults);
        if(!valid){state.result={error:'REQUEST_SCOPE_MISMATCH'};throw Error('TMS_SCOPE_GUARD');}
        state.expected=null;state.requests++;
        this.addEventListener('loadend',()=>{
          if(state.closed)return;
          if(this.status!==200){state.result={error:({401:'AUTH_REQUIRED',403:'PERMISSION_DENIED',429:'RATE_LIMITED'})[this.status]||'QUERY_FAILED',retry_after:this.status===429?this.getResponseHeader('Retry-After'):null};return;}
          try{
            const r=JSON.parse(this.responseText);
            const fields=['id','companyId','supplierId','orderId','platformOrderId','originalOrderId','shopId','platformId','orderTime','isSplit','isUnion','stockSku'];
            const rows=Array.isArray(r.datas)?r.datas.map(row=>{
              const v=Object.fromEntries(fields.filter(k=>k in row).map(k=>[k,row[k]]));v.id=id(v.id);
              if(includeItems&&Array.isArray(row.itemList)){
                // Observed list business JSON only; exclude buyer/addresses and keep IDs lossless.
                const keys=['id','taskItemId','taskId','orderItemId','transactionId','platformOrderId','originalOrderId','stockSku','originalSku','platformSku','productNum','itemCount','platformQuantity','originTaskId','packageId','isCombo','groupId','itemId'];
                v.itemList=row.itemList.map(item=>Object.fromEntries(keys.filter(k=>k in item).map(k=>[k,(k==='id'||/Id$/.test(k))?id(item[k]):item[k]])));
              }
              return v;
            }):null;
            const empty=r.code==='2'&&r.msg==='没有满足条件的数据'&&r.totalNum===0;
            if(r.code!=='200'&&!empty){state.result={error:['401','403'].includes(r.code)?(r.code==='401'?'AUTH_REQUIRED':'PERMISSION_DENIED'):'QUERY_FAILED'};return;}
            state.result={http_status:200,business_code:r.code,total:r.totalNum,rows:empty&&rows===null?[]:rows,explicit_empty:empty,
              request:Object.fromEntries(Object.keys(mandatory).map(k=>[k,q[k]]))};
          }catch{state.result={error:'SOURCE_CHANGED'};}
        },{once:true});
      }
      return originalSend.call(this,body);
    };
    function originPath(current){return 'https://tms.mabangerp.com/tmsapi/'+(current?'findOrderlist':'findOrderHistorylist');}
  },spec.include_items===true);installed=true;
  async function open(partition){
    const url=origin+'/#/'+routes[partition];
    if(await page.url()!==url)await page.goto(url);
    if(partition==='current'){
      const visible=await page.evaluate(()=>!!document.querySelector('.demo-drawer')?.getClientRects().length);
      if(!visible)await page.click('button:has-text("高级查询")');
    }
    await page.waitForFunction(()=>{try{return window.__tmsMonitor.vm().shopList.length>0}catch{return false}},undefined,{timeout:8000});
  }
  // Capture only search UI state, never the order detail model or employee data.
  const originalDrawer=await page.evaluate(()=>!!document.querySelector('.demo-drawer')?.getClientRects().length);
  await open(tab.url.endsWith('/myorderlist')?'current':'completed');
  before=await page.evaluate(()=>window.__tmsMonitor.capture());before.drawer=originalDrawer;
  await open('current');
  const identity=await page.evaluate(()=>({account:window.__tmsMonitor.account(),stores:window.__tmsMonitor.catalog()}));
  if(!identity.account||!identity.stores.length)throw Error('AUTH_REQUIRED');
  if(spec.mode==='inspect')output=identity;
  else {
    if(Object.keys(spec.account).some(k=>identity.account[k]!==spec.account[k]))throw Error('ACCOUNT_SCOPE_CHANGED');
    if(spec.scope.shop_ids.some(id=>!identity.stores.some(s=>s.shop_id===id&&s.company_id===spec.account.customer_id)))throw Error('CATALOG_CHANGED');
    const batches=[];
    for(const partition of spec.scope.partitions){
      if(!routes[partition])throw Error('SOURCE_CHANGED');
      await open(partition);
      for(const sku of spec.scope.skus){
        // These are observed reactive UI filter fields. The normal Search button
        // builds the request; the guard checks its exact observed parameter set.
        await page.evaluate(({partition,sku,range,shops})=>{
          const s=window.__tmsMonitor,v=s.vm();s.expected=null;
          const current=partition==='current';
          const filters=current?{skutype:'5',orderStatus:0,orderId:null,keyword:null,keytype:'1',stockSku:sku,originalSku:'',companyId:null,itemType:0,countryCodes:[],hasGoods:0,orderby:'orderTime',itemCount:0,idtype:'1',shopIdsList:shops.map(Number),orderTimes:range,isSyncLogistics:-1,myLogisticsChannelIdList:null,platformid:104,warehouseId:'',if_purchased:null,orderBy:3,ordertype:null,isProcessStock:0,customLabelIdsList:[],orderWeight1:null,orderWeight2:null,volume1:null,volume2:null,sizetype:'1',size1:null,size2:null,itemCount1:null,itemCount2:null,itemCount3:null,itemCount4:null,shopManager:null}
          :{orderStatus:5,idtype:'1',myLogisticsChannelIdList:[],orderId:null,keyword:null,keytype:'1',keytype2:'1',stockSku:sku,stockName:'',itemType:0,countryCodes:[],orderby:'orderTime',itemCount:0,countryCode:null,shopIdsList:shops.map(Number),orderTime:range,isSyncErp:0,isScan:null,is_online:-1,ordertype:null,status:null,platformOrderStatusList:[],customLabelIdsList:[]};
          if(Object.keys(filters).some(k=>!(k in v.search)))throw Error('SOURCE_CHANGED');
          Object.assign(v.search,filters);v.pageSize=50;
        },{partition,sku,range:spec.scope.source_ranges[partition],shops:spec.scope.shop_ids});
        lastApplied=await page.evaluate(()=>window.__tmsMonitor.capture());
        async function request(n,first=false){
          const expected={partition,sku,page:n,range:spec.scope.source_ranges[partition],shop_ids:spec.scope.shop_ids,account:spec.account};
          await page.evaluate(x=>{window.__tmsMonitor.result=null;window.__tmsMonitor.expected=x;},expected);
          if(first){
            await page.evaluate(()=>{const current=location.hash==='#/myorderlist';const scope=current?document.querySelector('.demo-drawer'):document;const button=[...scope.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.textContent.trim()==='搜索');if(!button)throw Error('SOURCE_CHANGED');button.click();});
          }else{
            if(partition==='current'){
              const visible=await page.evaluate(()=>!!document.querySelector('.demo-drawer')?.getClientRects().length);
              if(visible){await page.press('.demo-drawer','Escape');await page.waitForFunction(()=>!document.querySelector('.demo-drawer')?.getClientRects().length,undefined,{timeout:3000});}
            }
            await page.click('.el-pagination .btn-next');
          }
          await page.waitForFunction(()=>!!window.__tmsMonitor?.result,undefined,{timeout:18000});
          const result=await page.evaluate(()=>window.__tmsMonitor.result);
          if(result.error){output=result;throw Error(result.error);}
          return result;
        }
        const pages=[await request(1,true)];const total=pages[0].total;
        if(!Number.isInteger(total)||total<0||total>1000)throw Error('SCOPE_TOO_LARGE');
        for(let n=2;n<=Math.ceil(total/50);n++)pages.push(await request(n));
        if(partition==='current'){
          const visible=await page.evaluate(()=>!!document.querySelector('.demo-drawer')?.getClientRects().length);
          if(visible){await page.press('.demo-drawer','Escape');await page.waitForFunction(()=>!document.querySelector('.demo-drawer')?.getClientRects().length,undefined,{timeout:3000});}
        }
        const end=await page.evaluate(()=>[...document.querySelectorAll('.el-pagination .btn-next')].find(x=>x.getClientRects().length)?.disabled===true);
        await open(partition);
        const recheck=await request(1,true);
        batches.push({partition,sku,pages,end_confirmed:end,recheck_total:recheck.total,recheck_rows:recheck.rows});
        lastApplied=await page.evaluate(()=>window.__tmsMonitor.capture());
      }
    }
    output={account:identity.account,batches};
  }
}catch(e){
  let guarded;
  if(e.message==='TMS_SCOPE_GUARD')guarded={error:'REQUEST_SCOPE_MISMATCH'};
  output=output?.error?output:guarded||{error:allowedErrors.includes(e.message)?e.message:'QUERY_FAILED'};
}
finally{
  if(installed){
    try{
      const current=(await listTaskSpaces()).find(s=>s.id===spec.space_id);
      if(current?.ownership==='agent'){
        const state=await page.evaluate(()=>window.__tmsMonitor?.capture());
        // Restore our filters only if no subsequent UI edit has changed them.
        if(before&&(!lastApplied||JSON.stringify(state?.search)===JSON.stringify(lastApplied.search))){
          await page.evaluate(()=>{window.__tmsMonitor.expected=null;});
          if(await page.url()!==before.url)await page.goto(before.url);
          if(before.url.endsWith('/myorderlist')){
            const visible=await page.evaluate(()=>!!document.querySelector('.demo-drawer')?.getClientRects().length);
            if(!visible)await page.click('button:has-text("高级查询")');
          }
          await page.waitForFunction(()=>{try{return !!window.__tmsMonitor.vm()}catch{return false}},undefined,{timeout:3000});
          await page.evaluate(x=>{const v=window.__tmsMonitor.vm();Object.assign(v.search,x.search);v.pageSize=x.pageSize;},before);
          if(before.url.endsWith('/myorderlist')&&!before.drawer)await page.press('.demo-drawer','Escape');
        }
        await page.evaluate(()=>window.__tmsMonitor?.restore());
      }else if(!output?.error){output={error:'WAITING_USER'};}
    }catch{
      // The page-owned expiry still removes the guard. Never reclaim control.
      if(!output?.error)output={error:'UI_CLEANUP_FAILED'};
    }
  }
}
console.log('TMS_MONITOR_RESULT:'+JSON.stringify(output||{error:'QUERY_FAILED'}));
