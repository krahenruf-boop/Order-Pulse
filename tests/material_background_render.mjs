import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../order_pulse/integrations/ego_material_background.js',import.meta.url),'utf8')
 .replace("const fs = await import('node:fs/promises');",'');
const binding={space_id:4,expected_name:'Order Pulse · 素材与客单价',profile_id:'Default',profile_name:'昊'};
const entry={id:4,name:binding.expected_name,profileId:'Default',profileName:'昊',ownership:'agent',taskId:binding.expected_name};
async function run(mode){
 const commands=[],ready=[];let cycle=0,ownershipReads=0;
 const task={tabs:async()=>{
  cycle++;
  return [{targetId:'product',url:'https://www.kalodata.com/product/detail?id=1'},
   {targetId:'ui',url:'http://127.0.0.1:8786/#kalodata'},
   {targetId:'other',url:'https://erp.91miaoshou.com/'},
   ...(cycle===2?[{targetId:'detail',url:'https://www.kalodata.com/video/detail?id=2'}]:[])];
 },cdp:async(method,params)=>{commands.push({method,...params});if(method==='Target.attachToTarget')return {sessionId:'session-'+params.targetId};return {};}};
 const context=vm.createContext({INPUT:{binding,lease:'lease',ready:'ready'},URL,Date,Map,Set,
  process:{kill:()=>{if(mode==='owner-gone')throw Error('parent exited');}},fs:{writeFile:async(path,value)=>{ready.push(value);},stat:async()=>{if(cycle>=4)throw Error('released');}},
  listTaskSpaces:async()=>{ownershipReads++;return mode==='duplicate'?[entry,{...entry,id:8}]:[{...entry,ownership:mode==='handoff'&&ownershipReads>2?'user':'agent'}];},
  taskSpace:async id=>{assert.equal(id,4);return task;},setTimeout:fn=>{queueMicrotask(fn);}});
 await vm.runInContext('(async()=>{'+source+'})()',context);
 return {commands,ready};
}
const normal=await run('normal');assert.deepEqual(normal.ready,['READY']);
assert.deepEqual(normal.commands.filter(c=>c.method==='Target.attachToTarget').map(c=>c.targetId),['product','detail']);
for(const session of ['session-product','session-detail']){
 const states=normal.commands.filter(c=>c.sessionId===session&&c.message).map(c=>JSON.parse(c.message));
 assert.deepEqual(states.map(x=>x.params.enabled),[true,false]);
 assert.ok(states.every(x=>x.method==='Emulation.setFocusEmulationEnabled'));
 assert.equal(normal.commands.filter(c=>c.sessionId===session&&c.method==='Target.detachFromTarget').length,1);
}
const human=await run('handoff');assert.ok(human.commands.some(c=>c.message&&JSON.parse(c.message).params.enabled===false));
const ambiguous=await run('duplicate');assert.deepEqual(ambiguous.ready,['UNAVAILABLE']);assert.equal(ambiguous.commands.length,0);
const stopped=await run('owner-gone');assert.ok(stopped.commands.some(c=>c.message&&JSON.parse(c.message).params.enabled===false));
console.log('PASS hidden-page rendering lease, new detail tabs, cleanup, human handoff, parent exit and identity ambiguity');
