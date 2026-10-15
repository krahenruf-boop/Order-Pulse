// Migrated from the production-verified Linkme JSON collector.
// This adapter uses the already logged-in page in the explicitly bound ego Space.
// It may claim only the exact persisted dedicated Space id. It never discovers
// another Space, creates a Space, reads Cookie values, or changes funds.
/** Self-contained: ego lite serializes this function into the page's MAIN world. */
async function runJsonPageJob(job) {
  const lock = "__tiktokOpsCollectionLockV1";
  const origin = "https://tt.linkme.live";
  const fail = (error, extra = {}) => ({ok: false, error, ...extra});
  if (location.origin !== origin || location.pathname.replace(/\/$/, "") !== "/client/dashboard") return fail("目标网址不正确，请打开充值系统资产列表。");
  if (!document.body || document.querySelector('input[type="password"]')) return fail("充值系统可能尚未登录，请在当前 ego lite 个人资料中完成登录。");
  if (!["inspect", "collect"].includes(job?.action)) return fail("不支持此页面任务。");
  if (!/^[A-Z]{3}$/.test(job.currency || "")) return fail("JSON 采集需要已经核实的三位账户币种配置，请先核对币种。");
  if (window[lock]) return fail("页面正在进行另一项采集，请等待完成后重试。");

  // Preserve every JSON number's source text. Numeric IDs remain private number
  // tokens and therefore can never be mistaken for source-provided string IDs.
  const numeric = Symbol("json-number-source");
  let sourceSupported = false;
  JSON.parse("[1234567890123456789]", (key, value, context) => {
    if (key === "0") sourceSupported = context?.source === "1234567890123456789";
    return value;
  });
  if (!sourceSupported) return fail("当前 ego lite 不支持无损 JSON 数字读取，请更新 ego lite 后重试；本轮未读取余额。");
  const numberToken = (value) => Boolean(value && typeof value === "object" && value[numeric] === true);
  const decode = (text) => JSON.parse(text, (key, value, context) => {
    if (typeof value !== "number") return value;
    if (!context || typeof context.source !== "string") throw new Error("无损数字来源缺失");
    return {[numeric]: true, raw: context.source};
  });
  const record = (value) => Boolean(value && typeof value === "object" && !Array.isArray(value) && !numberToken(value));
  const integer = (value, label) => {
    if (!numberToken(value) || !/^(0|[1-9]\d*)$/.test(value.raw)) throw new Error(`JSON 数据校验失败：${label}不是整数。`);
    const result = Number(value.raw);
    if (!Number.isSafeInteger(result)) throw new Error(`JSON 数据校验失败：${label}超出安全范围。`);
    return result;
  };
  const decimal = (value, label) => {
    if (value === null) return null;
    const raw = numberToken(value) ? value.raw : typeof value === "string" ? value : "";
    if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(raw) || raw.length > 100) throw new Error(`JSON 数据校验失败：${label}不是有效金额。`);
    return raw;
  };
  const typeOf = (value) => numberToken(value) ? "number" : value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
  const dataError = (message, retryable = false) => Object.assign(new Error(message), {consistencyRetry: retryable});
  const started = Date.now(), deadline = started + 100000;
  const nativeFetch = window.fetch;
  let requests = 0;
  const retrySeconds = (value) => {
    const text = typeof value === "string" ? value.trim() : "";
    const seconds = /^\d+$/.test(text) ? Number(text) : text ? Math.ceil((Date.parse(text) - Date.now()) / 1000) : NaN;
    return Number.isFinite(seconds) ? Math.max(0, Math.min(86400, seconds)) : 60;
  };
  const readPage = async (page) => {
    if (Date.now() >= deadline) throw dataError("JSON 采集超过整轮 100 秒上限，请检查网络后重试。");
    const url = new URL("/client/dashboard", origin);
    const params = {asset_view: "agency", auth_filter: "all", assignment_filter: "all", direct_client_manager_id: "all", new_customer_type: "all", status: "all", page: String(page), page_size: "20"};
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), Math.min(15000, deadline - Date.now()));
    try {
      requests++;
      // The browser carries its existing same-origin session. Never read Cookie,
      // copy session credentials, submit forms, export reports or call funds APIs.
      const response = await nativeFetch.call(window, url.href, {method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: {Accept: "application/json", "X-TTAPI-SPA": "1"}, signal: controller.signal});
      if (response.status === 429) {
        const seconds = retrySeconds(response.headers.get("Retry-After"));
        throw Object.assign(dataError(`JSON 采集受充值系统限流（HTTP 429），请等待${seconds}秒后重试。`), {api_http_status: 429, api_retry_after_seconds: seconds});
      }
      if (response.status === 401 || response.status === 403) throw Object.assign(dataError(`充值系统拒绝读取资产列表（HTTP ${response.status}），请检查当前 ego lite 登录状态和权限。`), {api_http_status: response.status});
      if (response.status !== 200) throw Object.assign(dataError(`充值系统资产列表请求失败（HTTP ${response.status}），请稍后重试。`), {api_http_status: response.status});
      const responseUrl = new URL(response.url);
      if (responseUrl.origin !== origin || responseUrl.pathname !== "/client/dashboard") throw dataError("JSON 采集响应网址不符合资产列表，请检查当前 ego lite 登录状态。");
      const contentType = (response.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
      if (!/^application\/(?:json|[a-z0-9!#$&^_.+-]+\+json)$/.test(contentType)) throw dataError("充值系统没有返回 JSON 类型的资产数据，请检查当前 ego lite 登录状态。");
      const reader = response.body?.getReader();
      if (!reader) throw dataError("JSON 采集响应为空，请检查充值系统登录状态。");
      const decoder = new TextDecoder("utf-8", {fatal: true});
      let text = "", bytes = 0;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          bytes += part.value.byteLength;
          if (bytes > 8 * 1024 * 1024) { controller.abort(); throw dataError("JSON 单页响应超过 8 MB 安全上限，已停止读取。"); }
          text += decoder.decode(part.value, {stream: true});
        }
        text += decoder.decode();
      } finally { reader.releaseLock(); }
      let envelope;
      try { envelope = decode(text); }
      catch { throw dataError("充值系统没有返回可无损解析的资产 JSON，请确认仍已登录；本轮没有更新余额。"); }
      finally { text = ""; }
      const data = envelope?.data;
      if (envelope?.path !== "/client/dashboard" || envelope?.template !== "client_dashboard.html" || !record(data)) throw dataError("JSON 响应不符合资产列表，请确认当前 ego lite 登录状态后重试。");
      if (data.AssetView !== "agency" || data.AssetStatusFilter !== "all") throw dataError("JSON 返回资产范围不是代理商及全部状态，本轮已停止读取。");
      const total = integer(data.TotalCount, "账户总数"), actualPage = integer(data.Page, "页码"), pageSize = integer(data.PageSize, "每页数量"), pages = integer(data.TotalPages, "总页数");
      const source = typeof data.AssetBalanceLastUpdatedAt === "string" ? data.AssetBalanceLastUpdatedAt.trim() : "";
      if (total <= 0 || total > 100000 || actualPage !== page || pageSize !== 20 || pages !== Math.ceil(total / 20) || page > pages || !Array.isArray(data.MyAccounts) || data.MyAccounts.length !== Math.min(20, total - (page - 1) * 20)) throw dataError("JSON 分页数量、范围或页码无法一致校验，本轮未更新余额。", true);
      if (!/^\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]+\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}|UTC|GMT))?$/.test(source)) throw dataError("JSON 缺少可核验的来源余额更新时间，请检查充值系统。");
      return {total, page: actualPage, pageSize, pages, source, rows: data.MyAccounts};
    } catch (error) {
      if (controller.signal.aborted) throw dataError(Date.now() >= deadline ? "JSON 采集超过整轮 100 秒上限，请检查网络后重试。" : "JSON 资产列表请求超过 15 秒，请检查网络后重试。");
      if (error instanceof TypeError) throw dataError("JSON 资产列表网络请求失败或被重定向，请检查当前 ego lite 登录状态和网络。");
      throw error;
    } finally { clearTimeout(timeout); }
  };
  const schema = (rows) => {
    const types = new Map(), samples = {};
    for (const row of rows) {
      if (!record(row)) throw dataError("JSON 账户记录结构不正确。");
      for (const key of Object.keys(row)) {
        if (!/^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(key) || /cookie|token|nonce|password|secret|session|authorization/i.test(key)) continue;
        if (!types.has(key)) types.set(key, new Set());
        types.get(key).add(typeOf(row[key]));
        if (key === "DeliveryCountries" && Array.isArray(row[key]) && row[key].length <= 10 && row[key].every((value) => typeof value === "string" && value.length <= 80)) {
          if (!samples[key]) samples[key] = [];
          if (samples[key].length < 5 && !samples[key].some((value) => JSON.stringify(value) === JSON.stringify(row[key]))) samples[key].push([...row[key]]);
        }
        if (/status|customer.*type|new.*customer|quarter|country|currency|relation.*type|^BalanceUpdatedAt$/i.test(key) && ["string", "number", "boolean", "null"].includes(typeOf(row[key]))) {
          const sample = numberToken(row[key]) ? row[key].raw : row[key];
          if (typeof sample === "string" && sample.length > 80) continue;
          if (!samples[key]) samples[key] = [];
          if (samples[key].length < 5 && !samples[key].includes(sample)) samples[key].push(sample);
        }
      }
    }
    const identifierShapes = {};
    for (const key of ["AccountID", "BCID", "PartnerBCID"]) {
      const shapes = new Map();
      for (const row of rows) {
        const shape = {type: typeOf(row[key]), length: typeof row[key] === "string" ? row[key].length : null, digits: typeof row[key] === "string" ? /^\d+$/.test(row[key]) : null};
        const signature = JSON.stringify(shape);
        if (!shapes.has(signature)) shapes.set(signature, {...shape, count: 0});
        shapes.get(signature).count++;
      }
      identifierShapes[key] = [...shapes.values()];
    }
    return {identifier_shapes: identifierShapes, account_fields: [...types].map(([name, found]) => ({name, types: [...found].sort()})).sort((a, b) => a.name.localeCompare(b.name)), field_samples: samples};
  };
  const projectRow = (row) => {
    if (!record(row)) throw dataError("JSON 账户记录结构不正确。");
    if (row.RelationType !== "OWNER_BC") throw dataError("JSON 账户包含非 OWNER_BC 数据，本轮未更新余额。");
    if (typeof row.AccountID !== "string" || !/^\d{19}$/.test(row.AccountID)) throw dataError("JSON 资产ID必须是完整19位原始文本，数字型ID不能安全接收。");
    if (typeof row.BCID !== "string" || !/^\d*$/.test(row.BCID) || row.BCID.length > 40) throw dataError("JSON BC ID 必须是原始数字文本或明确空字符串，数字型和null不能安全接收。");
    if (Object.hasOwn(row, "PartnerBCID") && (typeof row.PartnerBCID !== "string" || !/^\d*$/.test(row.PartnerBCID) || row.PartnerBCID.length > 40)) throw dataError("JSON PartnerBCID 必须是原始数字文本或明确空字符串。");
    if (typeof row.AdvertiserShowStatus !== "string" || row.AdvertiserShowStatus.length > 1000) throw dataError("JSON 广告主状态字段缺失或格式不正确。");
    if (typeof row.AccountName !== "string" || !row.AccountName.trim() || row.AccountName.length > 2000) throw dataError("JSON 账户名称缺失或格式不正确。");
    if (!Object.hasOwn(row, "CashBalance") || !Object.hasOwn(row, "GrantBalance")) throw dataError("JSON 现金余额或赠款余额字段缺失，本轮未更新余额。");
    const result = {AccountID: row.AccountID, AccountName: row.AccountName, BCID: row.BCID, RelationType: row.RelationType,
      AdvertiserShowStatus: row.AdvertiserShowStatus, CashBalance: decimal(row.CashBalance, "现金余额"), GrantBalance: decimal(row.GrantBalance, "赠款余额")};
    // Both BC fields were observed empty in the live API. Preserve the exact
    // field values; never substitute PartnerBCID or a prior XLSX's BC ID.
    if (Object.hasOwn(row, "PartnerBCID")) result.PartnerBCID = row.PartnerBCID;
    for (const key of ["NewCustomerType", "NewCustomerYearQuarter"]) if (Object.hasOwn(row, key)) {
      if (typeof row[key] !== "string" || row[key].length > 1000) throw dataError(`JSON ${key}字段格式不正确。`);
      result[key] = row[key];
    }
    if (Object.hasOwn(row, "DeliveryCountries")) {
      if (!Array.isArray(row.DeliveryCountries) || row.DeliveryCountries.length > 100 || !row.DeliveryCountries.every((value) => typeof value === "string" && value.length <= 100)) throw dataError("JSON 投放国家字段格式不正确。");
      result.DeliveryCountries = [...row.DeliveryCountries];
    }
    // Only fields verified from the live schema are added here; never spread row.
    for (const key of ["Company", "BalanceUpdatedAt"]) if (Object.hasOwn(row, key)) {
      if (row[key] !== null && (typeof row[key] !== "string" || row[key].length > 2000)) throw dataError(`JSON ${key}字段格式不正确。`);
      result[key] = row[key];
    }
    return result;
  };
  const metadata = (first, extra = {}) => ({data_source: "json", owner_bc: true, filters_clear: true, page_count: first.total,
    page_pagination_pages: first.pages, source_updated_at_raw: first.source, refresh_mode: "snapshot", refresh_evidence: [], currency: job.currency,
    browser_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, ...extra});
  window[lock] = true;
  try {
    if (job.action === "inspect") {
      const first = await readPage(1);
      // Schema inspection must remain available when an ID's actual shape is
      // unknown. Return only types/lengths/counts, never the identifier values.
      for (const row of first.rows) {
        if (!record(row) || row.RelationType !== "OWNER_BC") throw dataError("JSON 检查包含非 OWNER_BC 或无效账户记录。");
      }
      return {ok: true, metadata: metadata(first, {inspection: schema(first.rows), request_count: requests})};
    }
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const first = await readPage(1), accounts = first.rows.map(projectRow), seen = new Set();
        const snapshots = [{page: first, rows: accounts.slice()}];
        const verifyHeader = (page) => {
          if (page.total !== first.total || page.pages !== first.pages) throw dataError("JSON 分页期间账户总数或总页数发生变化，无法形成一致快照。", true);
        };
        const addIds = (rows) => { for (const row of rows) { if (seen.has(row.AccountID)) throw dataError("JSON 多页出现重复资产ID，无法形成完整账户列表。", true); seen.add(row.AccountID); } };
        addIds(accounts);
        for (let page = 2; page <= first.pages; page++) {
          const result = await readPage(page); verifyHeader(result);
          const rows = result.rows.map(projectRow); addIds(rows); accounts.push(...rows);
          snapshots.push({page: result, rows});
        }
        // AssetBalanceLastUpdatedAt is page-local (verified live), not a global revision.
        // Re-read EVERY page, including page 1, and compare its own header and rows.
        for (const snapshot of snapshots) {
          const rechecked = await readPage(snapshot.page.page); verifyHeader(rechecked);
          if (rechecked.source !== snapshot.page.source || JSON.stringify(rechecked.rows.map(projectRow)) !== JSON.stringify(snapshot.rows))
            throw dataError(`JSON 第${snapshot.page.page}页复核时间或账户内容发生变化，无法确认整轮账户快照一致。`, true);
        }
        if (accounts.length !== first.total) throw dataError("JSON 全部账户数量与总数不一致。", true);
        // Preserve a real source timestamp, never the collection time. Per-account
        // BalanceUpdatedAt is still independently checked by the Decimal parser.
        const stamps = snapshots.map(({page}) => ({source: page.source, value: Date.parse(page.source.replace(" ", "T"))}));
        if (stamps.some(s => !Number.isFinite(s.value))) throw dataError("JSON 分页来源时间不能安全比较，本轮未更新余额。");
        const latest = stamps.reduce((a,b) => b.value > a.value ? b : a);
        return {ok: true, json_accounts: accounts, metadata: metadata(first, {
          source_updated_at_raw: latest.source, source_timestamp_scope: "max_page_source",
          page_source_updated_at: snapshots.map(({page}) => ({page: page.page, source_updated_at_raw: page.source})),
          refresh_evidence: ["json_all_pages_verified", "json_first_page_rechecked", "json_every_page_rechecked"],
          request_count: requests, consistency_attempts: attempt + 1})};
      } catch (error) { if (!error.consistencyRetry || attempt === 1) throw error; }
    }
  } catch (error) {
    const safe = /^(JSON|充值系统)/.test(error?.message || "") ? error.message : "JSON 资产采集失败，请检查当前 ego lite 登录状态和数据结构。";
    return fail(safe, {...(Number.isInteger(error.api_http_status) ? {api_http_status: error.api_http_status} : {}),
      ...(Number.isInteger(error.api_retry_after_seconds) ? {api_retry_after_seconds: error.api_retry_after_seconds} : {})});
  } finally { delete window[lock]; }
}


// Ego's numeric Space ID can be reassigned after a browser restart. Re-identify
// only the unique, explicitly saved dedicated Space, never an arbitrary ID match.
function resolveBoundBalanceSpace(spaces,spec) {
  const exact=s=>s.name===spec.space_name&&
    (!spec.expected_profile_id||s.profileId===spec.expected_profile_id)&&
    (!spec.expected_profile_name||s.profileName===spec.expected_profile_name);
  const matches=spaces.filter(exact);
  if(matches.length!==1)return null;
  const candidate=matches[0];
  if(candidate.id===spec.space_id)return candidate;
  return spec.purpose==='balance_kalodata'&&spec.expected_profile_id&&spec.expected_profile_name&&
    candidate.taskId===spec.space_name ? candidate : null;
}
const isLinkmeTab=tab=>{
  try{const u=new URL(tab.url);return u.origin==='https://tt.linkme.live'&&u.pathname.replace(/\/$/,'')==='/client/dashboard';}
  catch{return false;}
};

const MARKER='ORDER_PULSE_BALANCE:';
let result;
try {
  const spec=INPUT;
  const spaces=await listTaskSpaces();
  const entry=resolveBoundBalanceSpace(spaces,spec);
  if(!entry) result={ok:false,error_code:'BOUND_SPACE_MISSING',error:'未找到唯一且身份一致的专用 Space，请检查已保存的绑定'};
  else if(entry.name!==spec.space_name) result={ok:false,error_code:'SPACE_MISMATCH',error:'ego lite Space 身份与已保存绑定不一致'};
  else if(spec.expected_profile_id&&entry.profileId!==spec.expected_profile_id) result={ok:false,error_code:'SPACE_MISMATCH',error:'ego lite 配置身份与已保存绑定不一致'};
  else if(spec.expected_profile_name&&entry.profileName!==spec.expected_profile_name) result={ok:false,error_code:'SPACE_MISMATCH',error:'ego lite 账号身份与已保存绑定不一致'};
  else {
    let task,takeover=false;
    if(entry.ownership==='agent') task=await taskSpace(entry.id);
    else if(['user','agentDelegatedToUser'].includes(entry.ownership)&&spec.auto_takeover===true) {
      try { task=entry.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(entry.id):await claimTaskSpace(entry.id);takeover=true; }
      catch { result={ok:false,error_code:'WAITING_BROWSER',error:'专用浏览器暂不可接管'}; }
    } else result={ok:false,error_code:'WAITING_USER',error:'专用浏览器当前由用户操作'};
    if(result) {/* preserve the bounded takeover failure */}
    else if(task.spaceId!==entry.id) result={ok:false,error_code:'SPACE_MISMATCH',error:'ego lite 返回了不同的 Space'};
    else {
    const tabs=await task.tabs();
    // A cached page label can also point elsewhere after restart. Always check
    // the observed URL inside the verified Space before evaluating any page code.
    let tab=tabs.find(t=>t.label===spec.page_label&&isLinkmeTab(t));
    if(!tab){const matches=tabs.filter(isLinkmeTab);if(matches.length===1)tab=matches[0];}
    if(!tab) result={ok:false,error_code:'WAITING_CONNECTION',error:'专用 Space 中未找到唯一的 Linkme 账户页面'};
    else {
      const options=tabs.some(t=>t.label===spec.page_label)?{}:{as:spec.page_label};
      const page=tab.label?tab.page:await task.adopt(tab.page,options);
      result=await page.evaluate(runJsonPageJob,spec.job);
      if(result?.ok) result.browser={space_id:entry.id,space_name:entry.name,profile_id:entry.profileId,profile_name:entry.profileName,ownership:'agent',auto_takeover:spec.auto_takeover===true,takeover_performed:takeover,page_label:page.label};
    }
    }
  }
} catch(error) {
  const code=['WAITING_USER','WAITING_CONNECTION','WAITING_BROWSER','BOUND_SPACE_MISSING'].includes(error?.message)?error.message:'BALANCE_BROWSER_FAILED';
  result={ok:false,error_code:code,error:code==='WAITING_USER'?'专用浏览器当前由用户操作':code==='WAITING_BROWSER'?'专用浏览器暂不可接管':'ego lite 余额读取未完成'};
}
console.log(MARKER+JSON.stringify(result));
