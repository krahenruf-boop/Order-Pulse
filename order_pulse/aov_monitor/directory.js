// Only operational directory metadata leaves the page. No credential fields.
const spec=INPUT,task=await taskSpace(spec.space_id);
const tabs=await task.tabs(),tab=tabs.find(t=>t.url.startsWith('https://erp.91miaoshou.com/'));
if(!tab)throw Error('MIAOSHOU_PAGE_REQUIRED');
const page=tab.label?task.page(tab.label):await task.adopt(tab.page);
await page.goto('https://erp.91miaoshou.com/auth_shop/index?platform=tiktok');
await page.waitForSelector('input[placeholder="店铺名称/别名/ID"]',{timeout:15000});
await page.fill('input[placeholder="店铺名称/别名/ID"]','');await page.click('button:has-text("搜索")');
function read(){
 const root=[...document.querySelectorAll('*')].find(e=>e.__vue_app__);let rows=null;const seen=new Set();
 function walk(v,d=0){if(!v||d>50||seen.has(v)||rows)return;seen.add(v);const c=v.component;if(c){if(c.type.name==='ProTable'&&Array.isArray(c.props?.data))rows=c.props.data;walk(c.subTree,d+1)}if(Array.isArray(v.children))for(const x of v.children)if(typeof x==='object')walk(x,d+1)}walk(root?._vnode);
 if(!rows)return null;
 const totalText=document.querySelector('.jx-pagination__total')?.innerText||'';
 return {rows:rows.map(s=>({id:String(s.shopId),platform_shop_id:typeof s.platformShopId==='string'?s.platformShopId:null,name:s.shopNick||s.platformShopName,platform:s.platform,country:s.siteName,status:s.status,auth_status:s.authStatus})),total:Number(totalText.match(/\d+/)?.[0]),nextDisabled:document.querySelector('button.btn-next')?.disabled??true};
}
const result=[],ids=new Set();let total=null;
for(let n=0;n<100;n++){
 await page.waitForFunction(read,undefined,{timeout:15000});const v=await page.evaluate(read);
 if(!Number.isSafeInteger(v.total)||v.total<0||v.rows.some(s=>!s.id||s.platform!=='tiktok'||!s.name))throw Error('SHOP_DIRECTORY_SCHEMA_CHANGED');
 if(total!==null&&total!==v.total)throw Error('SHOP_DIRECTORY_CHANGED');total=v.total;
 for(const s of v.rows){if(ids.has(s.id))throw Error('SHOP_DIRECTORY_PAGE_REPEATED');ids.add(s.id);result.push(s)}
 if(v.nextDisabled)break;
 const before=v.rows.map(s=>s.id).join(',');await page.click('button.btn-next');
 await page.waitForFunction(({fn,before})=>{const v=eval('('+fn+')')();return v&&v.rows.map(s=>s.id).join(',')!==before},{fn:String(read),before},{timeout:15000});
}
if(result.length!==total)throw Error('SHOP_DIRECTORY_INCOMPLETE');
console.log('AOV_DIRECTORY:'+JSON.stringify(result));
await page.goto('https://erp.91miaoshou.com/order/package/all?appPackageTab=all');
