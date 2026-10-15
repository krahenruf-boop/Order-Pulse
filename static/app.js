import {createBalance} from './balance.js';
import {createKalodataSettings} from './kalodata-settings.js';
import {createAovMonitor} from './aov-monitor.js';
const $=s=>document.querySelector(s);
const routes=['balance','kalodata','aov-monitor'];
let csrf='',state,toastTimer,kalodataMount,kalodataLoad;
const mounted=new Set();
async function api(path,data){
 const r=await fetch('/api'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});
 const result=await r.json();if(!r.ok)throw Error(result.error||'操作未完成');return result;
}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,4000)}
const balance=createBalance({api,toast});
const kalodataSettings=createKalodataSettings({api,toast});
const aovMonitor=createAovMonitor({api,toast});
function currentRoute(){const route=location.hash.slice(1);return routes.includes(route)?route:'balance'}
function header(){
 const route=currentRoute(),module=state?.modules?.[route];
 const waiting=route==='kalodata'&&module?.status==='waiting_connection';
 $('#connection').className='status '+(waiting?'warning':'success');
 $('#connection').replaceChildren(document.createElement('i'),document.createTextNode(waiting?'等待采集插件':route==='balance'&&module?.paused?'余额监控已暂停':'后台运行中'));
}
async function navigation(){
 const route=currentRoute();if(location.hash!=='#'+route)history.replaceState(null,'','#'+route);
 for(const id of routes){$('#'+id).hidden=id!==route;$('#nav-'+id).toggleAttribute('aria-current',id===route)}
 header();
 if(route==='kalodata'){
  if(!kalodataMount){kalodataLoad??=import('./modules/kalodata/module.js');try{const m=await kalodataLoad;if(!kalodataMount)kalodataMount=m.mountKalodata($('#kalodata-root'),{csrf:()=>csrf,toast})}catch(e){kalodataLoad=null;toast('素材情报载入失败，请刷新页面')}}
 }else{
  const module=route==='balance'?balance:aovMonitor;
  if(!mounted.has(route)){module.mount($('#'+route));mounted.add(route)}else await module.load();
 }
}
$('#settings-open').onclick=()=>{const r=currentRoute();return r==='balance'?balance.openSettings():r==='kalodata'?kalodataSettings.open():aovMonitor.openSettings()};
async function refresh(){state=await api('/state');csrf=state.csrf;header();$('#service-notice').hidden=true}
try{await refresh();await navigation()}catch(e){$('#service-notice').hidden=false;$('#service-notice').textContent='后台暂不可连接，请启动 Order Pulse 后刷新。'}
window.addEventListener('hashchange',()=>navigation().catch(e=>toast(e.message)));
setInterval(()=>refresh().catch(()=>{$('#service-notice').hidden=false;$('#service-notice').textContent='后台暂不可连接，已保存的数据仍保留。'}),10000);
