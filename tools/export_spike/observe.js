// Passive, header-free evidence. Does not create or replay any TMS request.
() => {
  window.__opExportRestore?.();
  const originalOpen=XMLHttpRequest.prototype.open, originalSend=XMLHttpRequest.prototype.send;
  const originalWindowOpen=window.open;
  window.__opSuppressedPopupDownloads=0;
  window.open=function(url,...rest){
    if(window.__opCaptureDownload && typeof url==='string' && url===window.__opExportDownload && new URL(url,location.href).origin==='https://tms-cos.mabangerp.com'){
      window.__opSuppressedPopupDownloads++;return null;
    }
    return originalWindowOpen.call(this,url,...rest);
  };
  window.__opExportEvidence=[];
  XMLHttpRequest.prototype.open=function(method,url,...rest){this.__opMeta={method,url:new URL(url,location.href).pathname};return originalOpen.call(this,method,url,...rest)};
  XMLHttpRequest.prototype.send=function(body){
    const meta=this.__opMeta;
    if(meta && /export|findOrderlist|findOrderHistorylist/i.test(meta.url)){
      const record={...meta,started_at:new Date().toISOString()};
      // Capture the URL before the application's loadend promise opens a native
      // download; observing loadend alone is too late in this site's event order.
      this.addEventListener('readystatechange',()=>{
        if(this.readyState!==4)return;
        try{const p=JSON.parse(this.responseText);if(String(p.code)==='200'&&typeof p.pop==='string'&&new URL(p.pop,location.href).origin==='https://tms-cos.mabangerp.com')window.__opExportDownload=p.pop;}catch{}
      });
      try{const p=JSON.parse(body);record.request_keys=Object.keys(p);record.request=Object.fromEntries(Object.entries(p).filter(([k])=>['type','source','orderValueArr','itemValueArr','ischecked','ifMerge','page','pageSize','platformId','platformid','orderTimes','timeStr','stockSku','originalSku','shopIdsList','keytype2','orderStatus','skutype'].includes(k)));if(p.idsList)record.selected_ids_count=p.idsList.length;}catch{}
      this.addEventListener('loadend',()=>{record.http_status=this.status;record.elapsed_ms=Date.now()-Date.parse(record.started_at);record.mime=this.getResponseHeader('content-type');try{const p=JSON.parse(this.responseText);record.response_keys=Object.keys(p);record.code=p.code;if(typeof p.pop==='string' && /^https:\/\//.test(p.pop)){const u=new URL(p.pop,location.href);record.download={origin:u.origin,path_extension:u.pathname.split('.').pop(),filename:u.pathname.split('/').pop(),query_keys:[...u.searchParams.keys()]};window.__opExportDownload=p.pop;} if(p.totalNum!==undefined)record.totalNum=p.totalNum;}catch{} window.__opExportEvidence.push(record);},{once:true});
    }
    return originalSend.call(this,body);
  };
  window.__opExportRestore=()=>{XMLHttpRequest.prototype.open=originalOpen;XMLHttpRequest.prototype.send=originalSend;window.open=originalWindowOpen;window.__opCaptureDownload=false};
  setTimeout(()=>window.__opExportRestore?.(),15*60*1000);
  return {installed:true};
}
