import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import vm from 'node:vm';
const source=readFileSync(new URL('../order_pulse/kalodata/extension/observer.js',import.meta.url),'utf8').replace('export async function','async function');
const el=(value='',attrs={})=>({innerText:value,textContent:value,children:[],className:'',click:()=>{},getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,classList:{contains:()=>false},querySelector:()=>null,querySelectorAll:()=>[]});
const id='7670305790644735263';let clicks=0;
const window={open:()=>{throw Error('A real popup must not be opened');}};
const td=el('21s AD 工具箱');td.querySelectorAll=()=>[el('21s'),el(''),el('AD'),el('工具箱')];
// The tool button has its own label but is not the video's title. A blank video
// row must fall back to its normal row action if the title click emits no link.
const row=el('',{'data-row-key':id});row.querySelector=()=>td;
row.click=()=>{clicks++;window.open('https://www.kalodata.com/video/detail?id='+id);};
const pg=el('1 2 233');pg.querySelector=s=>s==='.ant-pagination-item-active'?el('2'):el('',{'aria-disabled':'false'});pg.querySelectorAll=()=>[el('1'),el('2'),el('233')];
pg.outerHTML='<ul>1 2 233</ul>';
const wrapper=el();wrapper.querySelector=()=>pg;
const table=el();table.closest=()=>wrapper;table.querySelector=s=>s==='thead'?el('视频内容'):null;table.querySelectorAll=s=>s==='tbody tr[data-row-key]'?[row]:[];
const document={body:el('视频与广告'),querySelector:()=>null,querySelectorAll:s=>s==='table'?[table]:[]};
const context=vm.createContext({window,document,URL,location:{href:'https://www.kalodata.com/product/detail?id=1&region=US'},Date,crypto:webcrypto,TextEncoder,getComputedStyle:()=>({visibility:'visible'}),setTimeout:fn=>queueMicrotask(fn)});
vm.runInContext(source,context);
const inspected=await vm.runInContext('observeDOM("inspect_ui",{_page_size:10})',context);
assert.equal(inspected.success,true,JSON.stringify(inspected));
context.payload={_page_size:10,source_page:2,context_fingerprint:inspected.data.context.context_fingerprint};
const result=await vm.runInContext('observeDOM("read_visible_page",payload)',context);
assert.equal(result.success,true,JSON.stringify(result));
assert.equal(result.data.rows[0].kalodata_video_url,'https://www.kalodata.com/video/detail?id='+id);
assert.equal(clicks,1);
console.log('PASS valid video without a description uses the verified row destination');
