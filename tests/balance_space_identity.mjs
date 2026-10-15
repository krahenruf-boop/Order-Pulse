import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../order_pulse/integrations/ego_balance.js',import.meta.url),'utf8');
const tail=source.slice(source.indexOf('function resolveBoundBalanceSpace'));
const name='Order Pulse · Balance + Kalodata';
const spec={space_id:1,space_name:name,expected_profile_id:'Default',expected_profile_name:'昊',purpose:'balance_kalodata',auto_takeover:true,page_label:'p1',job:{action:'inspect'}};
const dedicated=(id,more={})=>({id,name,profileId:'Default',profileName:'昊',taskId:name,ownership:'agent',...more});
async function run(spaces,{input=spec,tabs,returnedId}={}){
 const calls=[],evaluated=[];let result;
 const link={url:'https://tt.linkme.live/client/dashboard',page:{label:'p2',evaluate:async()=>{evaluated.push('linkme');return {ok:true};}}};
 const task={spaceId:returnedId??spaces.find(x=>x.name===name)?.id,tabs:async()=>tabs??[link],adopt:async(page,options)=>{calls.push(['adopt',options]);return page;}};
 const env={INPUT:input,URL,runJsonPageJob:()=>{},listTaskSpaces:async()=>spaces,taskSpace:async id=>{calls.push(['resume',id]);return task;},claimTaskSpace:async id=>{calls.push(['claim',id]);return task;},takeOverTaskSpace:async id=>{calls.push(['takeOver',id]);return task;},console:{log:s=>{result=JSON.parse(s.split('ORDER_PULSE_BALANCE:')[1]);}}};
 await vm.runInNewContext('(async()=>{'+tail+'})()',env);
 return {result,calls,evaluated};
}
const foreign={...dedicated(1),name:'Space-GitHub',taskId:'Space-GitHub'};
let r=await run([foreign,dedicated(0)]);assert.equal(r.result.ok,true);assert.equal(r.result.browser.space_id,0);assert.deepEqual(r.calls[0],['resume',0]);
r=await run([dedicated(0)],{input:{...spec,space_id:0}});assert.equal(r.result.ok,true);
for(const spaces of [[foreign],[dedicated(0,{profileName:'another user'})],[dedicated(0),dedicated(2)],[dedicated(0,{taskId:'different task'})]]){
 r=await run(spaces);assert.equal(r.result.ok,false);assert.equal(r.calls.length,0,'must not touch unrelated/ambiguous spaces');
}
r=await run([dedicated(0)],{input:{...spec,expected_profile_id:null}});assert.equal(r.result.ok,false);assert.equal(r.calls.length,0);
r=await run([dedicated(0,{ownership:'user'})],{input:{...spec,auto_takeover:false}});assert.equal(r.result.error_code,'WAITING_USER');assert.equal(r.calls.length,0);
r=await run([dedicated(0)],{returnedId:2});assert.equal(r.result.error_code,'SPACE_MISMATCH');assert.equal(r.evaluated.length,0);
let wrong=0,right=0;
const wrongTab={label:'p1',url:'https://github.com/',page:{label:'p1',evaluate:async()=>{wrong++;return {ok:true};}}};
const rightTab={url:'https://tt.linkme.live/client/dashboard',page:{label:'p3',evaluate:async()=>{right++;return {ok:true};}}};
r=await run([dedicated(0)],{tabs:[wrongTab,rightTab]});assert.equal(r.result.ok,true);assert.equal(wrong,0);assert.equal(right,1);assert.equal(r.result.browser.page_label,'p3');assert.deepEqual(JSON.parse(JSON.stringify(r.calls[1])),['adopt',{}]);
r=await run([dedicated(0)],{tabs:[wrongTab,rightTab,{...rightTab}]});assert.equal(r.result.ok,false);assert.equal(right,1);
console.log('PASS: zero/renumbered IDs, unrelated/ambiguous/profile protections, user control, returned identity, stale labels, ambiguous tabs');
