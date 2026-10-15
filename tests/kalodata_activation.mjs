import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../order_pulse/kalodata/extension/background.js',import.meta.url),'utf8').replace("import { observeDOM } from './observer.js';",'const observeDOM=()=>{};');
const calls=[];
const url='https://www.kalodata.com/product/detail?id=1&region=VN';
const chrome={tabs:{
 query:async()=>[{id:10,url}],
 update:async(id,options)=>{calls.push(['activate',id,options.active]);},
 reload:async id=>{calls.push(['reload',id]);},
 get:async id=>({id,url:id===20?url.replace('id=1','id=2'):url,windowId:7}),
 create:async options=>{calls.push(['create',options]);return {id:20};},
 remove:async id=>{calls.push(['remove',id]);},
},storage:{local:{set:async()=>{}}},scripting:{executeScript:async({target,world})=>{
 calls.push(['inject',target.tabId]);return [{result:world?{success:true,data:{bound:true}}:true}];
}}};
const context=vm.createContext({chrome,URL,Date,setTimeout});
vm.runInContext(source.slice(0,source.indexOf('chrome.runtime.onMessage')),context);
await vm.runInContext('execute({operation:"bind_product",payload:{product_url:'+JSON.stringify(url)+'}},{})',context);
assert.deepEqual(calls.slice(0,2),[['activate',10,true],['reload',10]],'existing product must be foreground and fresh before injection');
assert.equal(calls.filter(x=>x[0]==='reload').length,1,'one refresh per user task, not an endless retry');
calls.length=0;
await vm.runInContext('execute({operation:"bind_product",payload:{product_url:"https://www.kalodata.com/product/detail?id=2&region=VN"}},{})',context).catch(()=>{});
assert.equal(calls[0][0],'create');assert.equal(calls[0][1].active,true);
console.log('PASS explicit-task activation and single refresh');
