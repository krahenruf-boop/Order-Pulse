import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../order_pulse/api_monitor/ego_quota.js',import.meta.url),'utf8');
const name='Order Pulse · 余额 · API监控';
const binding={space_id:1,expected_name:name,profile_id:'Default',profile_name:'昊'};

async function collect({provider='elevenlabs',missing=false,login=false,navigationFailure=false,wrongRoute=false,duplicateWorkspace=false,startAtSettings=false,used='85'}={}){
 const calls=[];let result,current='',partial=false,context;
 const origin=provider==='elevenlabs'?'https://elevenlabs.io':'https://platform.deepseek.com';
 current=origin+(startAtSettings?'/app/settings':provider==='deepseek'?'/usage':'/app/subscription/creative');
 const texts={
  '/app/settings':'邮箱地址\naccount@example.test\n名字\nAccount',
  '/app/settings/workspaces':'My Workspace\n套餐 • Creator\n当前工作区',
  '/app/subscription/creative':`已用积分\n${used} 积分 / 100 积分\n将于 10月21日 续订`,
  '/profile':'用户名\nAccount\n邮箱\naccount@example.test\n手机号码\n185******96',
  '/usage':'充值余额\n¥ 7.52 CNY'
 };
 function dom(){
  context.location=new URL(current);
  context.document={readyState:'interactive',body:{innerText:login?'登录':partial?'加载中':missing?'已用积分':texts[context.location.pathname]||''},
   querySelector:()=>login?{}:null,
   querySelectorAll:()=>context.location.pathname==='/app/settings/workspaces'?[...Array(duplicateWorkspace?2:1)].map(()=>({innerText:'当前工作区',parentElement:{parentElement:{innerText:'My Workspace\n套餐 • Creator\n当前工作区'}}})):[]};
 }
 async function navigate(target,options,method){
  calls.push({method,target,options});
  if(navigationFailure)throw Error('page.goto timed out after 10000ms waiting for commit');
  current=login?origin+'/sign-in':wrongRoute?origin+'/app/home':target===origin+'/app/subscription'?target+'/creative':target;
  partial=true;dom();
  // Simulates the real defect: load never completes, while the committed
  // document can still hydrate the required account and subscription fields.
  if(options?.waitUntil!=='commit')throw Error('page.goto timed out after 15000ms waiting for load; navigation committed with document.readyState="interactive"');
 }
 const page={url:async()=>current,goto:(target,opts)=>navigate(target,opts,'goto'),reload:opts=>navigate(current,opts,'reload'),
  waitForFunction:async(fn,arg,opts)=>{
   calls.push({method:'wait',options:opts});
   let ready=fn(arg);if(!ready){partial=false;dom();ready=fn(arg);}
   if(!ready)throw Error('page.waitForFunction timed out');
  },evaluate:async(fn,arg)=>fn(arg)};
 const task={tabs:async()=>[{label:'p2',url:current}],page:()=>page,handOff:async()=>calls.push({method:'handOff'})};
 context=vm.createContext({INPUT:{binding,provider},URL,Date,
  listTaskSpaces:async()=>[{id:1,name,profileId:'Default',profileName:'昊',taskId:name,ownership:'agent'}],
  taskSpace:async()=>task,console:{log:line=>{result=JSON.parse(line.split('ORDER_PULSE_QUOTA:')[1]);}}});
 dom();await vm.runInContext('(async()=>{'+source+'})()',context);
 return {result:JSON.parse(JSON.stringify(result)),calls};
}

let r=await collect();
assert.equal(r.result.ok,true,'interactive pages must not fail on slow load assets');
assert.deepEqual(r.result.reading,{identity:'account@example.test · My Workspace',unit:'Credits',used:'85',total:'100',remaining:'15',reset_label:'10月21日 续订（平台未显示具体时刻）'});
assert.equal(r.calls.filter(c=>c.method==='goto').length,3);
assert.ok(r.calls.filter(c=>c.method==='wait').every(c=>c.options.timeout>0&&c.options.timeout<=20000));
r=await collect({startAtSettings:true});assert.equal(r.result.ok,true);assert.equal(r.calls[0].method,'reload','same URL must get fresh DOM');
r=await collect({used:'100'});assert.equal(r.result.reading.remaining,'0','a verified zero is valid');
r=await collect({provider:'deepseek'});assert.equal(r.result.ok,true);assert.equal(r.result.reading.remaining,'7.52');
for(const options of [{missing:true},{wrongRoute:true},{duplicateWorkspace:true},{navigationFailure:true}]){
 r=await collect(options);assert.equal(r.result.ok,false);assert.equal(r.result.reading,undefined,'incomplete fields must not become a zero reading');
 assert.match(r.result.error,/[\u3400-\u9fff]/);assert.doesNotMatch(r.result.error,/page\.|document\.|timed out/);
}
r=await collect({login:true});assert.equal(r.result.needs_user,true);assert.equal(r.calls.filter(c=>c.method==='handOff').length,1);assert.equal(r.calls.filter(c=>c.method==='goto').length,1);
console.log('PASS: interactive/hydrating pages, same-route refresh, redirect, zero credit, missing fields, wrong route, ambiguous workspace, navigation timeout and login handoff');
