// Only visible account/billing DOM. Never reads cookies, tokens or API keys.
const b=INPUT.binding, provider=INPUT.provider;
let result,needsUser=false;
try {
 const found=(await listTaskSpaces()).filter(s=>s.name===b.expected_name&&s.profileId===b.profile_id&&s.profileName===b.profile_name);
 if(found.length!==1||(found[0].id!==b.space_id&&found[0].taskId!==b.expected_name))throw Error('浏览器空间身份无法核验');
 const s=found[0],t=s.ownership==='user'?await claimTaskSpace(s.id):s.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(s.id):await taskSpace(s.id);
 const origin=provider==='deepseek'?'https://platform.deepseek.com':'https://elevenlabs.io';
 const tabs=(await t.tabs()).filter(x=>{try{return new URL(x.url).origin===origin}catch{return false}});
 if(tabs.length!==1)throw Error('请在财务空间保留唯一的供应商已登录页面');
 const p=tabs[0].label?t.page(tabs[0].label):await t.adopt(tabs[0].page);
 async function go(path,marker){
   await p.goto(origin+path);
   await p.waitForFunction(s=>document.body?.innerText.includes(s)||!!document.querySelector('input[type="password"]')||/(?:login|sign-in|sign-up)/i.test(location.pathname),marker,{timeout:15000});
   const text=await p.evaluate(()=>document.body.innerText);
   if(/(?:login|sign-in|sign-up)/i.test(await p.url())||await p.evaluate(()=>!!document.querySelector('input[type="password"]')||/Verify you are human|安全验证|请输入验证码/.test(document.body.innerText))) {needsUser=true;await t.handOff();throw Error('登录已失效，请完成供应商登录或验证码，再回工作台点击手动刷新');}
   return text;
 }
 if(provider==='deepseek'){
   await go('/profile','手机号码');
   await p.waitForFunction(()=>/邮箱\s*[^\n]+@[^\n]+/.test(document.body.innerText)&&/手机号码\s*[0-9*]{5,}/.test(document.body.innerText),undefined,{timeout:15000});
   const profile=await p.evaluate(()=>document.body.innerText);
   const email=profile.match(/邮箱\s*([^\n]+)/)?.[1]?.trim(),phone=profile.match(/手机号码\s*([^\n]+)/)?.[1]?.trim();
   const name=profile.match(/用户名\s*([^\n]+)/)?.[1]?.trim();
   if(!email||!phone||!name)throw Error('DeepSeek 账户身份字段缺失');
   await go('/usage','充值余额');
   await p.waitForFunction(()=>/充值余额[\s\S]{0,140}?[¥$]\s*([\d,]+(?:\.\d+)?)\s*(CNY|USD)/.test(document.body.innerText),undefined,{timeout:15000});
   const text=await p.evaluate(()=>document.body.innerText);
   const m=text.match(/充值余额[\s\S]{0,140}?[¥$]\s*([\d,]+(?:\.\d+)?)\s*(CNY|USD)/);
   if(!m)throw Error('DeepSeek 余额字段缺失');
   result={ok:true,reading:{identity:[name,email,phone].join(' · '),remaining:m[1].replaceAll(',',''),unit:m[2]}};
 }else{
   const account=await go('/app/settings','邮箱地址');
   const email=account.match(/邮箱地址\s*([^\n]+@[^\n]+)/)?.[1]?.trim();
   const spaces=await go('/app/settings/workspaces','当前工作区');
   // This account was verified to have a single workspace. Multiple candidates
   // require an explicit new adapter binding instead of guessing a name.
   const workspace=await p.evaluate(()=>{const rows=[...document.querySelectorAll('button')].filter(b=>b.innerText.trim()==='当前工作区');return rows.length===1?rows[0].parentElement.parentElement.innerText.trim().split('\n')[0]:null;});
   if(!email||!workspace||(spaces.match(/套餐\s*•/g)||[]).length!==1)throw Error('ElevenLabs 账户或唯一工作区无法核验');
   await go('/app/subscription','已用积分');
   await p.waitForFunction(()=>/已用积分\s*[\d,]+\s*积分\s*\/\s*[\d,]+\s*积分/.test(document.body.innerText),undefined,{timeout:15000});
   const text=await p.evaluate(()=>document.body.innerText);
   const m=text.match(/已用积分\s*([\d,]+)\s*积分\s*\/\s*([\d,]+)\s*积分/);
   const reset=text.match(/将于\s*([^\n]+?)\s*续订/);
   if(!m||!reset)throw Error('ElevenLabs 当期积分或续订日期字段缺失');
   const used=Number(m[1].replaceAll(',','')),total=Number(m[2].replaceAll(',',''));
   if(!Number.isSafeInteger(used)||!Number.isSafeInteger(total))throw Error('ElevenLabs 额度格式变化');
   result={ok:true,reading:{identity:email+' · '+workspace,unit:'Credits',used:String(used),total:String(total),remaining:String(Math.max(0,total-used)),reset_label:reset[1].trim()+' 续订（平台未显示具体时刻）'}};
 }
}catch(e){result={ok:false,error:e.message||'额度读取失败',needs_user:needsUser};}
console.log('ORDER_PULSE_QUOTA:'+JSON.stringify(result));
