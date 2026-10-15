// Normal UI only. Refund history is separate from the order-list refund flags.
async function useMiaoshouPageSize(page,size=100){
 const selector='.jx-pagination__sizes .jx-select__wrapper';
 await page.waitForSelector(selector,{timeout:10000});
 const current=await page.evaluate(()=>document.querySelector('.jx-pagination__sizes')?.innerText.trim());
 if(current===size+'条/页')return;
 await page.click(selector);
 await page.click('.jx-select-dropdown__item:has-text("'+size+'条/页")');
 await page.waitForFunction(size=>document.querySelector('.jx-pagination__sizes')?.innerText.trim()===size+'条/页',size,{timeout:5000});
}
async function scopeMiaoshouRefundPayment(page,range){
 if(!range)return; // Legacy receipts still use their original complete history.
 await page.evaluate(range=>{
  const target=document.querySelector('input[placeholder="开始时间"]'),root=[...document.querySelectorAll('*')].find(e=>e.__vue_app__);let found=null;const seen=new Set();
  function owns(v,d=0){if(!v||d>30)return false;if(v.el?.nodeType===1&&v.el.contains(target))return true;if(v.component&&owns(v.component.subTree,d+1))return true;return Array.isArray(v.children)&&v.children.some(x=>typeof x==='object'&&owns(x,d+1))}
  function walk(v,d=0){if(!v||d>60||seen.has(v)||found)return;seen.add(v);const c=v.component;if(c){if(c.type.name==='ProDatePicker'&&owns(c.subTree))found=c;walk(c.subTree,d+1)}if(Array.isArray(v.children))for(const x of v.children)if(typeof x==='object')walk(x,d+1)}walk(root?._vnode);
  const update=found?.vnode.props?.['onUpdate:modelValue'];if(typeof update!=='function')throw Error('MIAOSHOU_REFUND_DATE_CONTROL_CHANGED');update([...range]);
 },range);
 await page.waitForFunction(range=>{const inputs=['开始时间','结束时间'].map(x=>document.querySelector('input[placeholder="'+x+'"]'));return range.every((v,n)=>inputs[n]?.value===v)},range,{timeout:5000});
 // Apply the date range first: the grouped picker restores the time type.
 await page.click('.jx-form-item:has(input[placeholder="开始时间"]) .jx-select__wrapper');
 await page.evaluate(()=>{const e=[...document.querySelectorAll('[role="option"]')].find(x=>x.getClientRects().length&&x.innerText.trim()==='付款时间');if(!e)throw Error('MIAOSHOU_REFUND_PAY_FILTER_REQUIRED');e.dataset.aovPayTime='1'});
 await page.click('[data-aov-pay-time="1"]');
 await page.waitForFunction(()=>document.querySelector('.gmt-date-select')?.innerText==='付款时间',undefined,{timeout:5000});
}
async function readMiaoshouRefunds(page, spec) {
  let installed = false,stage='open';
  try {
    const already=(await page.url()).startsWith('https://erp.91miaoshou.com/purchase/after_sale_order');
    await page.goto('https://erp.91miaoshou.com/purchase/after_sale_order');
    if(already)await page.reload();
    await page.waitForSelector('button.J_queryFormSearch', {timeout: 15000});
    stage='platform';await page.waitForSelector('label:has(input[value="tiktok"])',{timeout:15000});
    await page.click('label:has(input[value="tiktok"])');
    await page.waitForFunction(()=>document.querySelector('input[value="tiktok"]')?.checked,undefined,{timeout:10000});
    if(!spec.shared){
    stage='shop';await page.fill('.shop-selector .jx-cascader__search-input', spec.shop_name);
    await page.waitForFunction(name => [...document.querySelectorAll('.jx-cascader__suggestion-item')].filter(e=>e.getClientRects().length&&e.innerText.trim()===name).length===1, spec.shop_name, {timeout:10000});
    await page.evaluate(name=>{const e=[...document.querySelectorAll('.jx-cascader__suggestion-item')].find(e=>e.getClientRects().length&&e.innerText.trim()===name);e.dataset.aovRefundShop='1';}, spec.shop_name);
    await page.click('[data-aov-refund-shop="1"]');
    }
    stage='page-size';await useMiaoshouPageSize(page,100);
    stage='payment-range';await scopeMiaoshouRefundPayment(page,spec.range);
    await page.evaluate(spec => {
      const P = XMLHttpRequest.prototype, open = P.open, send = P.send, paths = new WeakMap();
      const s = {result: null, expected: 1, sign: 'afterSignedReturnOrder', closed: false};
      window.__aovRefunds = s;
      s.restore = () => { clearTimeout(s.timer); s.closed = true; P.open = open; P.send = send; delete window.__aovRefunds; };
      s.timer = setTimeout(s.restore, 150000);
      P.open = function(method, url, ...rest) { paths.set(this, new URL(url, location.href).pathname); return open.call(this, method, url, ...rest); };
      P.send = function(body) {
        const path = paths.get(this), before = s.sign==='beforeSignedReturnOrder';
        if (path === '/api/order/after_sale/searchOrderReturnList' || path === '/api/order/after_sale/searchOrderCancelList') {
          const expected = {
            pageSize:'100', page:String(s.expected), platform:'tiktok', tab:'refunding', signStatus:s.sign,
            afterSaleType:'all', secondaryTab:'all', platformReturnSns:'', timeSort:'gmtReturnCreateTime_desc',
            distributeTab:'', purchaseOrderStatus:'', gmtStartFrom:spec.range?.[0]||'', gmtStartTo:spec.range?.[1]||'', platformItemIds:'',
            purchaseOrderSn:'', purchaseOrderBuyer:'', trackingNos:'', orderGmtType:spec.range?'pay':'start', paymentMethod:'',
            purchaseLogisticsKeyword:'', purchaseLogisticsKeywordRp:'eq', platformOrderSnsRp:'eq',
            platformReturnSnsRp:'eq', purchaseOrderSnRp:'eq', trackingNosRp:'eq', distributeNumRp:'eq',
            distributeNumType:'returnTrackingNumbers', hasOrderPendingMatter:'', hasOrderSellerNote:'',
            hasOrderAppNote:'', afterSaleReasons:'', platformOrderSns:'',
            reverseType:'all', subTab:'all', returnReasons:''
          };
          if(!spec.shared)expected['checkedShops[0]']=spec.shop_id;
          if (before) { delete expected.afterSaleReasons; delete expected.reverseType; delete expected.subTab; delete expected.returnReasons; expected.returnOrderType='beforeSignedReturnOrder'; }
          const q = Object.fromEntries(new URLSearchParams(body));
          if (path !== '/api/order/after_sale/'+(before?'searchOrderCancelList':'searchOrderReturnList') || Object.keys(q).some(k => !(k in expected)) || Object.entries(expected).some(([k,v]) => q[k] !== v)) {
            s.result = {error:'MIAOSHOU_REFUND_FILTER_UNVERIFIED'};
            throw Error('AOV_REFUND_SCOPE');
          }
          this.addEventListener('loadend', () => {
            if (s.closed) return;
            try {
              if (this.status !== 200) throw Error('MIAOSHOU_REFUND_SOURCE_FAILED');
              const v = JSON.parse(this.responseText), total = Number(v.total);
              const items=before?v.orderCancelList:v.orderReturnList;
              if (!Array.isArray(items) || !Number.isSafeInteger(total) || total < 0) throw Error('MIAOSHOU_REFUND_SCHEMA_CHANGED');
              if(spec.range&&items.some(x=>typeof x.gmtPay!=='string'||x.gmtPay<spec.range[0]||x.gmtPay>spec.range[1]))throw Error('MIAOSHOU_REFUND_PAYMENT_UNVERIFIED');
              const rows = items.map(x => ({
                id:x.opOrderReturnId, platform_order_id:x.platformOrderSn, shop_id:x.shopId,
                kind:before?'cancellation':'refund',status:before?x.appOrderStatus:x.status, platform_status:x.platformReturnStatus, refund_amount:x.refundAmount,
                pay_amount:x.payAmount, currency:x.currency, reverse_type:x.reverseType
              }));
              if (rows.some(x => typeof x.id !== 'string' || typeof x.platform_order_id !== 'string' || typeof x.shop_id !== 'string' || (!spec.shared && x.shop_id !== spec.shop_id))) throw Error('MIAOSHOU_REFUND_ID_UNVERIFIED');
              s.result = {total, rows};
            } catch(e) { s.result = {error:/^MIAOSHOU_/.test(e.message) ? e.message : 'MIAOSHOU_REFUND_SCHEMA_CHANGED'}; }
          }, {once:true});
        }
        return send.call(this, body);
      };
    }, spec);
    installed = true;
    const result = [], seen = new Map();let requests=0;
    const totals={};
    for (const sign of ['afterSignedReturnOrder','beforeSignedReturnOrder']) {
      stage='sign-'+sign;await page.evaluate(sign => { const s=window.__aovRefunds; s.sign=sign; s.expected=1; s.result=null; }, sign);
      if (sign === 'beforeSignedReturnOrder') {
        await page.waitForSelector('label:has(input[value="beforeSignedReturnOrder"])',{timeout:10000});
        await page.click('label:has(input[value="beforeSignedReturnOrder"])');
        await page.waitForFunction(() => !!window.__aovRefunds?.result, undefined, {timeout:15000});
      }
      const pages = []; let total = null, count = 0;
      for (let n=1;n<=150;n++) {
        stage='pages-'+sign;
        if(!(n===1&&sign==='beforeSignedReturnOrder')){
          await page.evaluate(n => { const s=window.__aovRefunds; s.expected=n; s.result=null; }, n);
          await page.click(n===1 ? 'button.J_queryFormSearch' : 'button.btn-next');
        }
        requests++;
        await page.waitForFunction(() => !!window.__aovRefunds?.result, undefined, {timeout:15000});
        const v = await page.evaluate(() => window.__aovRefunds.result);
        if (v.error) throw Error(v.error);
        if (total !== null && total !== v.total) throw Error('MIAOSHOU_REFUND_COVERAGE_CHANGED');
        total = v.total; count += v.rows.length;
        if ((n*100<total && v.rows.length!==100) || count>total) throw Error('MIAOSHOU_REFUND_PAGINATION_INCOMPLETE');
        for (const r of v.rows) {
          const identity=r.kind+':'+r.id;
          if (seen.has(identity)) throw Error('MIAOSHOU_REFUND_PAGE_REPEATED');
          seen.set(identity,r); result.push(r);
        }
        pages.push(v);
        if (count===total) break;
        if (n===150) throw Error('MIAOSHOU_REFUND_LIMIT_REACHED');
      }
      stage='recheck-'+sign;await page.evaluate(() => {window.__aovRefunds.expected=1; window.__aovRefunds.result=null;});
      await page.click('button.J_queryFormSearch');
      await page.waitForFunction(() => !!window.__aovRefunds?.result, undefined, {timeout:15000});
      const last=await page.evaluate(() => window.__aovRefunds.result);
      if (JSON.stringify(last)!==JSON.stringify(pages[0])) throw Error('MIAOSHOU_REFUND_COVERAGE_CHANGED');
      requests++;totals[sign]=total;
    }
    await page.evaluate(metrics=>{window.__aovRefundMetrics=metrics},{pages:requests,page_size:100,totals,payment_range:spec.range||null});
    return spec.shared?result.filter(r=>spec.shop_ids.includes(r.shop_id)):result;
  } catch(e){e.aovStage=stage;throw e;} finally { if (installed) await page.evaluate(() => window.__aovRefunds?.restore()).catch(()=>{}); }
}

async function verifyMiaoshouShop(page, spec) {
  await page.goto('https://erp.91miaoshou.com/auth_shop/index?platform=tiktok');
  await page.waitForSelector('input[placeholder="店铺名称/别名/ID"]', {timeout:15000});
  await page.fill('input[placeholder="店铺名称/别名/ID"]', spec.shop_name);
  await page.click('button:has-text("搜索")');
  await page.waitForFunction(name => [...document.querySelectorAll('tbody tr')].some(r => [...r.querySelectorAll('td')].some(c=>c.innerText.trim()===name)), spec.shop_name, {timeout:15000});
  const verified = await page.evaluate(name => {
    const heads = [...document.querySelectorAll('thead th')].map(x=>x.innerText.trim());
    const rows = [...document.querySelectorAll('tbody tr')].filter(r=>[...r.querySelectorAll('td')].some(c=>c.innerText.trim()===name));
    if (rows.length!==1) return false;
    const cells=[...rows[0].querySelectorAll('td')].map(c=>c.innerText.trim());
    return heads.indexOf('店铺别名')>=0 && heads.indexOf('店铺状态')>=0 && heads.indexOf('授权状态')>=0 &&
      cells[heads.indexOf('店铺别名')]===name && cells[heads.indexOf('店铺状态')]==='正常' &&
      cells[heads.indexOf('授权状态')].split(/\s+/)[0]==='已授权';
  }, spec.shop_name);
  if (!verified) throw Error('MIAOSHOU_SHOP_HEALTH_UNVERIFIED');
}
