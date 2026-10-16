// Exercise the shipped observer against delayed DOM refreshes, without network/browser access.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
const source=readFileSync(new URL('../order_pulse/kalodata/extension/observer.js',import.meta.url),'utf8').replace('export async function','async function');
async function run(mode){
 let clock=10000,clicked=null,selected=mode==='cold-singleton'?'180D':'30D';const clicks=[];
 let revealed=null;
 const refreshed=()=>mode==='cold-singleton'?revealed!==null&&clock-revealed>=1800:clicked!==null&&clock-clicked>=1800;
 const loading=()=>mode==='stuck' ? clicked!==null : clicked!==null&&clock-clicked>=800&&!refreshed();
 const el=(value='',attrs={})=>({innerText:value,textContent:value,children:[],className:'',parentElement:null,getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,classList:{contains:()=>false},matches:()=>false,querySelector:()=>null,querySelectorAll:()=>[]});
 const presets=['30D','180D'].map(period=>{const e=el('过去'+period.slice(0,-1)+'天');e.closest=()=>e;e.getAttribute=k=>k==='aria-selected'?String(selected===period):null;e.click=()=>{clicks.push(period);selected=period;clicked=clock;};return e;});
 const rows=()=>mode==='empty'?[]:Array.from({length:10},(_,i)=>el('video '+(refreshed()?'new':'old')+i,{'data-row-key':String((refreshed()?200:100)+i)}));
 const pg=el();Object.defineProperty(pg,'innerText',{get:()=>mode==='cold-singleton'&&!refreshed()?'1':refreshed()?'1 2 431':'1 2 130'});
 pg.querySelector=s=>s==='.ant-pagination-item-active'?el('1'):el('',{'aria-disabled':s==='.ant-pagination-prev'?'true':'false'});
 pg.querySelectorAll=()=>mode==='cold-singleton'&&!refreshed()?[el('1')]:[el('1'),el(refreshed()?'431':'130')];
 const wrapper=el();wrapper.querySelector=()=>['empty','missing-pagination'].includes(mode)?null:pg;
 const table=el();table.closest=()=>wrapper;table.querySelector=s=>s==='thead'?el('视频内容'):s==='.ant-table-placeholder'&&mode==='empty'?el('暂无查询结果，请修改筛选条件或搜索词后重试。'):null;table.querySelectorAll=s=>s==='tbody tr[data-row-key]'?rows():s==='th[aria-sort]'?[el('成交金额',{'aria-sort':'descending'})]:[];
 if(mode==='cold-singleton'){
  const heading=el('视频与广告');heading.scrollIntoView=()=>{revealed=clock;};
  const section=el();section.querySelectorAll=()=>[heading];table.parentElement=section;
 }
 const document={body:el('商品 视频与广告'),querySelector:s=>s==='.ant-spin-spinning'?(loading()?el():null):s.includes('开始日期')?{value:'2026/04/13'}:s.includes('结束日期')?{value:'2026/10/09'}:null,querySelectorAll:s=>s==='table'?[table]:s.includes('role="radio"')?(mode==='unknown'?[]:presets):s==='.ant-spin-spinning'?(loading()?[el()]:[]):[]};
 class Clock extends Date{constructor(...args){super(...(args.length?args:[Date.UTC(2026,9,10)+clock]));}static now(){return clock;}}
 const context=vm.createContext({document,location:{href:'https://www.kalodata.com/product/detail?id=1&region=TH'},URL,Date:Clock,crypto:webcrypto,TextEncoder,getComputedStyle:()=>({visibility:'visible'}),setTimeout:fn=>{clock+=150;queueMicrotask(fn);}});
 const result=await vm.runInContext(source+';observeDOM("apply_period",{period:"180D",requested_end_page:2})',context);
 return {result,clock,clicks};
}
const delayed=await run('delayed');
assert.equal(delayed.result.success,true,JSON.stringify(delayed));
assert.equal(delayed.result.data.visible_pages,431,'must not freeze the old 30D pagination while 180D refresh is pending');
assert.ok(delayed.clock>=11800,'must observe the delayed list refresh, not only the selected date button');
const unknown=await run('unknown');assert.equal(unknown.result.success,true,'unknown dates must not block a readable list');
const stuck=await run('stuck');assert.equal(stuck.result.success,false);assert.ok(['SOURCE_REFRESH_TIMEOUT','SOURCE_LIST_NOT_READY'].includes(stuck.result.error_code));
console.log('PASS delayed refresh, unknown dates, loading timeout');

const empty=await run('empty');assert.equal(empty.result.success,true,JSON.stringify(empty));assert.equal(empty.result.data.visible_total,0);assert.equal(empty.result.data.visible_pages,1);
const missing=await run('missing-pagination');assert.equal(missing.result.error_code,'PAGINATION_SCHEMA_CHANGED');
console.log('PASS verified empty versus missing pagination');
const cold=await run('cold-singleton');assert.equal(cold.result.success,true,JSON.stringify(cold));assert.equal(cold.result.data.visible_pages,431);
assert.deepEqual(cold.clicks,[],'reveal the video heading to load its count without extra date queries');
console.log('PASS lazy video count loads before page range validation');
