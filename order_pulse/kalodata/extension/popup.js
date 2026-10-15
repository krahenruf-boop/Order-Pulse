const send=m=>chrome.runtime.sendMessage(m);
(async()=>{const s=await send({type:'status'});document.querySelector('#status').textContent=s.paired?'已配对，可在工作台开始任务':'尚未配对';})();
document.querySelector('#pair').onclick=async()=>{const s=await send({type:'pair',code:document.querySelector('#code').value});document.querySelector('#status').textContent=s.ok?'配对成功':s.error;document.querySelector('#code').value='';};
