// Normal form submission on the saved TMS Space. Never solve or bypass challenges.
const MARKER='ORDER_PULSE_TMS_LOGIN:';
// TMS's show-password toggle changes the input type. Use the observed field
// identity icons, not type=password, to keep the two fields distinct.
const USER_FIELD='form .el-form-item:has(.el-icon-s-custom) input.el-input__inner';
const PASSWORD_FIELD='form .el-form-item:has(.el-icon-key) input.el-input__inner';
let result={status:'UNAVAILABLE'};
function loginView() {
  if(location.origin!=='https://tms.mabangerp.com')return {kind:'WRONG_ORIGIN'};
  // Tencent leaves solved challenge text off-screen with opacity:0. It is not
  // an active verification request; inspect visible text rather than body text.
  const visible=e=>{
    if(!e||!e.getClientRects().length||!e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
    const r=e.getBoundingClientRect();return r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;
  };
  const text=[...document.querySelectorAll('body *')].filter(e=>!e.children.length&&visible(e)).map(e=>e.textContent).join('\n');
  if(/拖动.*滑块|滑动.*验证|完成.*拼图|安全验证|短信验证码|手机验证码|二次验证|两步验证|请完成验证|验证码|验证失败/.test(text))return {kind:'CHALLENGE'};
  if(/密码错误|密码不正确|账号或密码|用户名或密码|登录失败|账户.*锁定|账号.*锁定/.test(text))return {kind:'REJECTED'};
  const user=[...document.querySelectorAll('form .el-form-item:has(.el-icon-s-custom) input.el-input__inner')].filter(visible);
  const pass=[...document.querySelectorAll('form .el-form-item:has(.el-icon-key) input.el-input__inner')].filter(visible);
  const submit=[...document.querySelectorAll('form button.el-button--primary')].filter(e=>visible(e)&&e.textContent.trim()==='登录');
  if(location.hash==='#/login'&&user.length===1&&pass.length===1&&submit.length===1)return {kind:'FORM'};
  if(location.hash!=='#/login'&&!pass.length) {
    const e=document.querySelector('#app')?.__vue__?.$store?.state?.employee;
    const id=v=>typeof v==='string'?v:Number.isSafeInteger(v)?String(v):null;
    if(e)return {kind:'SESSION',account:{employee_id:id(e.id),supplier_id:id(e.supplierId),customer_id:id(e.customerId)}};
  }
  return {kind:'UNKNOWN'};
}
try {
  const b=INPUT.binding;
  const matches=(await listTaskSpaces()).filter(e=>e.name===b.expected_name&&e.profileId===b.profile_id&&e.profileName===b.profile_name);
  const e=matches.length===1?matches[0]:null;
  if(!e||!(e.id===b.space_id||e.taskId===b.expected_name)||e.ownership!=='agent')throw Error('BOUND_SPACE_REQUIRED');
  const task=await taskSpace(e.id);if(task.spaceId!==e.id)throw Error('BOUND_SPACE_REQUIRED');
  const tabs=(await task.tabs()).filter(t=>{try{return new URL(t.url).origin==='https://tms.mabangerp.com'}catch{return false}});
  if(tabs.length!==1)throw Error('TMS_TAB_AMBIGUOUS');
  const tab=tabs[0],p=tab.label?task.page(tab.label):await task.adopt(tab.page);
  let view=await p.evaluate(loginView);
  if(view.kind==='CHALLENGE')result={status:'VERIFICATION_REQUIRED'};
  else if(view.kind==='REJECTED')result={status:'CREDENTIALS_REJECTED'};
  else if(view.kind==='FORM'&&INPUT.probe_only)result={status:'READY'};
  else if(view.kind==='FORM') {
    await p.fill(USER_FIELD,INPUT.credentials.username);
    if((await p.evaluate(loginView)).kind!=='FORM')throw Error('FORM_CHANGED');
    await p.fill(PASSWORD_FIELD,INPUT.credentials.password);
    if((await p.evaluate(loginView)).kind!=='FORM')throw Error('FORM_CHANGED');
    await p.click('form button.el-button--primary',{label:'提交马帮登录'});
    // Wait for a visible challenge, validation error or normal signed-in route.
    try {await p.waitForFunction(()=>{
      const employee=document.querySelector('#app')?.__vue__?.$store?.state?.employee;
      // Vue changes the route before hydrating the signed-in employee. A route
      // change alone is not sufficient to validate the session identity.
      return location.hash!=='#/login'&&employee?.id!=null&&employee?.supplierId!=null&&employee?.customerId!=null||
        [...document.querySelectorAll('body *')].some(e=>!e.children.length&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&e.getBoundingClientRect().bottom>0&&e.getBoundingClientRect().top<innerHeight&&/拖动.*滑块|滑动.*验证|拼图|安全验证|验证码|密码错误|密码不正确|账号或密码|用户名或密码|登录失败/.test(e.textContent));
    },undefined,{timeout:20000});}catch{}
    view=await p.evaluate(loginView);
    if(view.kind==='CHALLENGE')result={status:'VERIFICATION_REQUIRED'};
    else if(view.kind==='REJECTED')result={status:'CREDENTIALS_REJECTED'};
  } else if(view.kind!=='SESSION')result={status:'SOURCE_CHANGED'};
  if(view.kind==='SESSION') {
    if(!Object.keys(INPUT.account).every(k=>INPUT.account[k]===view.account[k]))result={status:'ACCOUNT_SCOPE_CHANGED'};
    else {
      if(!INPUT.probe_only&&!(await p.url()).startsWith('https://tms.mabangerp.com/#/myorder'))await p.goto('https://tms.mabangerp.com/#/myorderlist');
      result={status:'SIGNED_IN'};
    }
  }
} catch {result={status:'UNAVAILABLE'};}
console.log(MARKER+JSON.stringify(result));
