import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../order_pulse/kalodata/extension/background.js',import.meta.url),'utf8').replace("import { observeDOM } from './observer.js';",'const observeDOM=()=>{};');
const calls=[];let active=99,nextId=20,failDetail=false;
const url='https://www.kalodata.com/product/detail?id=1&region=VN';
const tabs=new Map([[10,{id:10,url,windowId:7}]]);
const chrome={tabs:{
 query:async()=>[tabs.get(10)],
 update:async(id,options)=>{calls.push(['activate',id,options.active]);if(options.active)active=id;},
 reload:async id=>{calls.push(['reload',id]);},
 get:async id=>tabs.get(id),
 create:async options=>{calls.push(['create',options]);const tab={id:nextId++,windowId:7,...options};tabs.set(tab.id,tab);if(options.active)active=tab.id;return tab;},
 remove:async id=>{calls.push(['remove',id]);},
},storage:{local:{set:async()=>{}}},scripting:{executeScript:async({target,world,args})=>{
 if(failDetail&&args?.[0]==='resolve_observed_link')throw Error('document closed');
 calls.push(['inject',target.tabId]);return [{result:world?{success:true,data:{bound:true}}:true}];
}}};
const context=vm.createContext({chrome,URL,Date,setTimeout});
vm.runInContext(source.slice(0,source.indexOf('chrome.runtime.onMessage')),context);
await vm.runInContext('execute({operation:"bind_product",payload:{product_url:'+JSON.stringify(url)+'}},{})',context);
assert.deepEqual(calls[0],['reload',10],'existing product refreshes in the background');
assert.equal(calls.filter(x=>x[0]==='reload').length,1,'one refresh per user task, not an endless retry');
assert.equal(calls.filter(x=>x[0]==='activate').length,0);assert.equal(active,99);
calls.length=0;
await vm.runInContext('execute({operation:"bind_product",payload:{product_url:"https://www.kalodata.com/product/detail?id=2&region=VN"}},{})',context).catch(()=>{});
assert.equal(calls[0][0],'create');assert.equal(calls[0][1].active,false);assert.equal(active,99);
for(const failure of [false,true]){
 calls.length=0;failDetail=failure;
 vm.runInContext('context={page_size:10};snapshot={run:"run",page:2,ids:["9"]};',context);
 const task=vm.runInContext('execute({operation:"resolve_observed_link",payload:{run_id:"run",source_page:2,video_id:"9",observed_detail_url:"https://www.kalodata.com/video/detail?id=9"}},{tabId:10})',context);
 if(failure)await assert.rejects(task,/document closed/);else await task;
 assert.equal(calls.find(x=>x[0]==='create')[1].active,false);
 assert.equal(calls.find(x=>x[0]==='create')[1].windowId,7);
 assert.equal(calls.filter(x=>x[0]==='remove').length,1,'owned detail tab closes on success and failure');
 assert.equal(calls.filter(x=>x[0]==='activate').length,0,'detail cleanup must not activate the product');
 assert.equal(active,99,'workbench stays in the foreground throughout extraction');
}
console.log('PASS background refresh, product creation, detail extraction and cleanup');
