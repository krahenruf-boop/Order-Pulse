// Exercise the shipped observer against delayed DOM refreshes, without network/browser access.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
const source=readFileSync(new URL('../order_pulse/kalodata/extension/observer.js',import.meta.url),'utf8').replace('export async function','async function');
async function run(mode){
 let clock=10000,clicked=null,selected='30D';
 const refreshed=()=>clicked!==null&&clock-clicked>=1800;
 const loading=()=>mode==='stuck' ? clicked!==null : clicked!==null&&clock-clicked>=800&&!refreshed();
 const el=(value='',attrs={})=>({innerText:value,textContent:value,children:[],className:'',parentElement:null,getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,classList:{contains:()=>false},matches:()=>false,querySelector:()=>null,querySelectorAll:()=>[]});
 const presets=['30D','180D'].map(period=>{const e=el('过去'+period.slice(0,-1)+'天');e.closest=()=>e;e.getAttribute=k=>k==='aria-selected'?String(selected===period):null;e.click=()=>{selected=period;clicked=clock;};return e;});
 const rows=()=>Array.from({length:10},(_,i)=>el('video '+(refreshed()?'new':'old')+i,{'data-row-key':String((refreshed()?200:100)+i)}));
 const pg=el();Object.defineProperty(pg,'innerText',{get:()=>refreshed()?'1 2 431':'1 2 130'});
 pg.querySelector=s=>s==='.ant-pagination-item-active'?el('1'):el('',{'aria-disabled':s==='.ant-pagination-prev'?'true':'false'});
 pg.querySelectorAll=()=>[el('1'),el(refreshed()?'431':'130')];
 const wrapper=el();wrapper.querySelector=()=>pg;
 const table=el();table.closest=()=>wrapper;table.querySelector=s=>s==='thead'?el('视频内容'):null;table.querySelectorAll=s=>s==='tbody tr[data-row-key]'?rows():s==='th[aria-sort]'?[el('成交金额',{'aria-sort':'descending'})]:[];
 const document={body:el('商品 视频与广告'),querySelector:s=>s==='.ant-spin-spinning'?(loading()?el():null):s.includes('开始日期')?{value:'2026/04/13'}:s.includes('结束日期')?{value:'2026/10/09'}:null,querySelectorAll:s=>s==='table'?[table]:s.includes('role="radio"')?(mode==='unknown'?[]:presets):s==='.ant-spin-spinning'?(loading()?[el()]:[]):[]};
 class Clock extends Date{constructor(...args){super(...(args.length?args:[Date.UTC(2026,9,10)+clock]));}static now(){return clock;}}
 const context=vm.createContext({document,location:{href:'https://www.kalodata.com/product/detail?id=1&region=TH'},URL,Date:Clock,crypto:webcrypto,TextEncoder,getComputedStyle:()=>({visibility:'visible'}),setTimeout:fn=>{clock+=150;queueMicrotask(fn);}});
 const result=await vm.runInContext(source+';observeDOM("apply_period",{period:"180D"})',context);
 return {result,clock};
}
const delayed=await run('delayed');
assert.equal(delayed.result.success,true,JSON.stringify(delayed));
assert.equal(delayed.result.data.visible_pages,431,'must not freeze the old 30D pagination while 180D refresh is pending');
assert.ok(delayed.clock>=11800,'must observe the delayed list refresh, not only the selected date button');
const unknown=await run('unknown');assert.equal(unknown.result.success,true,'unknown dates must not block a readable list');
const stuck=await run('stuck');assert.equal(stuck.result.success,false);assert.ok(['SOURCE_REFRESH_TIMEOUT','SOURCE_LIST_NOT_READY'].includes(stuck.result.error_code));
console.log('PASS delayed refresh, unknown dates, loading timeout');
