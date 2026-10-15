// Run only through ego-browser nodejs; follows the visible Export button.
const fs=await import('node:fs/promises');const path=await import('node:path');const {randomUUID}=await import('node:crypto');const {execFileSync}=await import('node:child_process');
const options=JSON.parse(await fs.readFile('/tmp/order-pulse-spike-options.json','utf8'));const root=options.project_root;
if (!options.python_path || !path.isAbsolute(options.python_path)) throw new Error('Set the isolated Python runtime path');
const page=(await taskSpace(options.space_id ?? 1)).page(options.page_label ?? 'p1');
const dir=path.join('/tmp/order-pulse',randomUUID());await fs.mkdir(dir,{recursive:true,mode:0o700});
const started=Date.now();let report;
try{
 const expected=await page.evaluate(()=>{const v=window.__opSpikeVue;return v.orderTaskList.filter(r=>window.__opSpikeAllFiltered||v.checkTaskId.includes(r.id)).map(r=>({orderId:String(r.orderId),platformId:r.platformId,orderTime:r.orderTime,shopName:r.shopName,orderFee:r.orderFee,originalOrderId:String(r.originalOrderId||''),items:(r.itemList||[]).map(i=>({stockSku:i.stockSku,platformSku:i.platformSku,productNum:i.productNum,platformQuantity:i.platformQuantity}))}));});
 const scope=await page.evaluate(()=>({all_filtered:!!window.__opSpikeAllFiltered,total:window.__opSpikeVue.totalNum,source_window:window.__opSpikeVue.search.orderTime||window.__opSpikeVue.search.orderTimes,shopNames:window.__opSpikeVue.shopList.filter(x=>x.platformId===104).map(x=>x.name)}));
 await fs.writeFile(path.join(dir,'expected.json'),JSON.stringify({rows:expected,scope}),{mode:0o600});
 const previous=await page.evaluate(()=>window.__opExportEvidence?.length||0);
 if(!options.existing_export){
  await page.evaluate(()=>{window.__opCaptureDownload=true;window.__opExportDownload=null;const nativeOpen=window.open;window.__opDownloadRestore=()=>{window.open=nativeOpen;};window.open=function(url,...rest){if(typeof url==='string'){const u=new URL(url,location.href);if(u.origin==='https://tms-cos.mabangerp.com'&&u.pathname.endsWith('.xls')){window.__opCapturedFileUrl=url;window.__opSuppressedPopupDownloads=(window.__opSuppressedPopupDownloads||0)+1;return null;}}return nativeOpen.call(this,url,...rest);};const button=[...document.querySelectorAll('.el-dialog__wrapper button')].find(x=>x.getClientRects().length&&x.innerText.trim()==='导出');if(!button)throw new Error('Visible export dialog required');button.click();});
  await page.waitForFunction(n=>(window.__opExportEvidence||[]).slice(n).some(r=>r.url.endsWith('/exportOrder')||r.request_keys?.includes('type')),previous,{timeout:60000});
 }
 const url=await page.evaluate(()=>window.__opExportDownload);
 if(!url)throw new Error('No observed download URL returned by export');
 if(new URL(url).origin!=='https://tms-cos.mabangerp.com')throw new Error('Download origin changed; inspect before allowing it');
 const response=await fetch(url,{signal:AbortSignal.timeout(60000),redirect:'error'});
 if(!response.ok)throw new Error(`Download HTTP ${response.status}`);
 const chunks=[];let totalBytes=0;const reader=response.body.getReader();
 try{while(true){const {done,value}=await reader.read();if(done)break;totalBytes+=value.length;if(totalBytes>50000000){await reader.cancel();throw new Error('Spike file exceeds bounded 50MB test limit');}chunks.push(value);}}finally{reader.releaseLock();}
 const bytes=Buffer.concat(chunks);
 await fs.writeFile(path.join(dir,'export.xls'),bytes,{mode:0o600});
 const data=JSON.parse(execFileSync(options.python_path,[path.join(root,'tools/export_spike/inspect_xls.py'),path.join(dir,'export.xls'),path.join(dir,'expected.json')],{encoding:'utf8'}));
 const evidence={test:options.test_name||'one_selected',at:new Date().toISOString(),elapsed_ms:Date.now()-started,filename:new URL(url).pathname.split('/').pop(),download_http_status:response.status,download_mime:response.headers.get('content-type'),download_content_length:response.headers.get('content-length'),requests:await page.evaluate(()=>(window.__opExportEvidence||[]).map(r=>({...r,request:{...r.request,...(r.request?.shopIdsList?.length?{shopIdsList:{count:r.request.shopIdsList.length,scope:'authorized TikTok only'}}:{})}}))),selected_platforms:[...new Set(expected.map(x=>x.platformId))],ui_long_tasks_ms:await page.evaluate(()=>window.__opSpikeLongTasks||[]),list_state:await page.evaluate(()=>({total:window.__opSpikeVue.totalNum,pageSize:window.__opSpikeVue.pageSize,loaded:window.__opSpikeVue.orderTaskList.length,page:window.__opSpikeVue.page,selected:window.__opSpikeVue.checkTaskId.length,template:window.__opSpikeVue.ExportOrderForm})),xls:data};
 evidence.suppressed_native_downloads=await page.evaluate(()=>window.__opSuppressedPopupDownloads||0);report=evidence;
 await fs.writeFile(path.join(root,'docs/export-spike',`${evidence.test}.json`),JSON.stringify(evidence,null,2)+'\n');console.log(evidence);
}finally{
 await fs.rm(dir,{recursive:true,force:true});const removed=!(await fs.stat(dir).catch(()=>null));
 await page.evaluate(()=>{window.__opDownloadRestore?.();window.__opExportRestore?.();});
 if(report){report.temporary_cleanup=removed;report.page_hooks_restored=true;await fs.writeFile(path.join(root,'docs/export-spike',`${report.test}.json`),JSON.stringify(report,null,2)+'\n');}
 console.log({temporary_cleanup:removed,page_hooks_restored:true});
}
