// Display helpers only. Canonical order and valid-link persistence belong to the service.
export const PERIODS = [['1D','昨日'],['7D','7天'],['30D','30天'],['90D','90天'],['180D','180天'],['365D','365天']];
export const ACTIVE = new Set(['created','preflighting','reading','resolving']);
export const STATE_LABELS = {created:'任务已建立',preflighting:'正在核对页面',reading:'正在读取列表',resolving:'正在读取原链接',completed:'请求范围读取完成',completed_with_errors:'页面已读完，部分链接未取得',partial:'部分完成',cancelled:'已取消',failed:'任务失败',blocked:'任务已停止，需要处理',interrupted:'上次任务已中断'};
export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
export function safeHref(value, kind, videoId) {
  try {
    const u = new URL(value);
    if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
    if (kind === 'tiktok') {
      const m = /^\/@[A-Za-z0-9._-]+\/video\/(\d+)\/?$/.exec(u.pathname);
      if (u.hostname !== 'www.tiktok.com' || !m || typeof videoId !== 'string' || m[1] !== videoId) return null;
    } else if (u.hostname !== 'www.kalodata.com' || !['/video/detail','/product/detail'].includes(u.pathname)) return null;
    return u.href;
  } catch { return null; }
}
export function validateInput(form) {
  const errors = {};
  try {
    const u = new URL(form.product_url);
    if (u.protocol !== 'https:' || u.hostname !== 'www.kalodata.com' || u.port || u.username || u.password || u.pathname !== '/product/detail' || !/^\d+$/.test(u.searchParams.get('id') || '') || u.searchParams.getAll('id').length !== 1) throw new Error();
  } catch { errors.product_url = '请输入 https://www.kalodata.com/product/detail?id=… 商品详情网址。'; }
  if (!PERIODS.some(([value]) => value === form.period)) errors.period = '请选择网站时间范围。';
  const start = String(form.start_page), end = String(form.end_page);
  if (!/^[1-9]\d*$/.test(start) || !Number.isSafeInteger(Number(start))) errors.start_page = '起始页必须是正整数。';
  if (!/^[1-9]\d*$/.test(end) || !Number.isSafeInteger(Number(end))) errors.end_page = '结束页必须是正整数。';
  if (!errors.start_page && !errors.end_page && Number(start) > Number(end)) errors.end_page = '结束页不得小于起始页；两端都包含。';
  return errors;
}
export function cooldownSeconds(capabilities, now = Date.now()) {
  const until = Date.parse(capabilities?.cooldown_until || '');
  return Number.isFinite(until) ? Math.max(0, Math.ceil((until-now)/1000)) : 0;
}
export function startAllowed(capabilities, run, busy, now = Date.now()) {
  return Boolean(capabilities?.enabled && capabilities.transport_ready && capabilities.session_ref && !busy && !ACTIVE.has(run?.state) && !cooldownSeconds(capabilities, now));
}
export function coverageText(run) {
  const counts = run.counts || {}, missing = run.missing_pages || [];
  if (run.coverage === 'empty_verified') return '已核验请求范围为空，没有可复制或导出的链接。';
  if (ACTIVE.has(run.state)) return '任务仍在运行，复制和导出在本轮结束后开放。';
  if (run.coverage !== 'requested_range_complete' || counts.unresolved_unique || counts.invalid_rows) return `仅含已成功取得的 ${counts.resolved_unique || 0} 条；${counts.unresolved_unique || 0} 个唯一视频未解析，${counts.invalid_rows || 0} 行无效。${missing.length ? `未读页：${missing.join('、')}。` : '请核对本轮覆盖范围。'}`;
  return `本次请求页区间已读完，取得 ${counts.resolved_unique || 0} 条原链接；不代表商品全部视频。`;
}
export function sortText(context) {
  return context?.observed_sort?.length ? context.observed_sort.map(x => `${x.field} ${x.direction === 'DESC' ? '↓' : '↑'}`).join('，') + ' · 页面顺序' : '排序名称未知 · 保留实际页面行序';
}
