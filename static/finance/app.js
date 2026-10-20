import {createBalance} from './balance.js';
import {createApiMonitor} from './api-monitor.js';
const $=s=>document.querySelector(s);let csrf='',timer;
async function api(path,data){const r=await fetch('/api'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});const value=await r.json();if(!r.ok)throw Error(value.error||'操作未完成');return value}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(timer);timer=setTimeout(()=>$('#toast').hidden=true,5000)}
const modules={balance:createBalance({api,toast}),'api-monitor':createApiMonitor({api,toast})},mounted=new Set();
const route=()=>Object.hasOwn(modules,location.hash.slice(1))?location.hash.slice(1):'balance';
async function navigate(){const current=route();if(location.hash!=='#'+current)history.replaceState(null,'','#'+current);for(const id of Object.keys(modules)){$('#'+id).hidden=id!==current;$('#nav-'+id).toggleAttribute('aria-current',id===current)}if(!mounted.has(current)){modules[current].mount($('#'+current));mounted.add(current)}else await modules[current].load()}
$('#settings-open').onclick=()=>Promise.resolve(modules[route()].openSettings()).catch(e=>toast(e.message));
async function refresh(){const state=await api('/state');csrf=state.csrf;$('#service-notice').hidden=true;$('#connection').className='status success';$('#connection').replaceChildren(document.createElement('i'),document.createTextNode('财务后台运行中'))}
try{await refresh();await navigate()}catch(e){toast(e.message)}
window.addEventListener('hashchange',()=>navigate().catch(e=>toast(e.message)));
setInterval(()=>refresh().catch(()=>{$('#service-notice').hidden=false;$('#service-notice').textContent='财务后台暂不可连接，已有数据仍保留。'}),30000);
