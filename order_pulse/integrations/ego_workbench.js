// Return within the saved Space only. Completion must never reclaim human control.
const b = INPUT.binding;
const matches = (await listTaskSpaces()).filter(e => e.name === b.expected_name &&
  e.profileId === b.profile_id && e.profileName === b.profile_name);
const candidate = matches.length === 1 ? matches[0] : null;
const entry = candidate && (candidate.id === b.space_id || candidate.taskId === b.expected_name) ? candidate : null;
let result = {state:'BINDING_REQUIRED'};
if (entry && entry.ownership === 'agent') {
  const task = await taskSpace(entry.id);
  const tabs = await task.tabs();
  const active = tabs.find(t => t.active);
  const routes = b.purpose === 'balance_kalodata' ? ['#balance','#api-monitor'] : ['#aov-monitor','#kalodata'];
  const workbenches = tabs.filter(t => {
    try {
      const u = new URL(t.url);
      return u.origin === (INPUT.origin || 'http://127.0.0.1:8786') && u.pathname === '/' && !u.search && routes.includes(u.hash);
    } catch { return false; }
  });
  // Leave a login/challenge page visible when the site needs the user's help.
  const needsUser = active && /(?:\/|#\/)(?:login|signin|captcha|challenge)(?:[/?#]|$)/i.test(active.url);
  const preferred = workbenches.filter(t => t.url.endsWith('#' + INPUT.module));
  const target = workbenches.find(t => t.active) || (preferred.length === 1 ? preferred[0] : workbenches.length === 1 ? workbenches[0] : null);
  if (needsUser) result = {state:'WAITING_USER'};
  else if (!target) result = {state:workbenches.length ? 'AMBIGUOUS' : 'WORKBENCH_MISSING'};
  else if (target.active) result = {state:'VISIBLE'};
  else {
    // Metadata is checked again just before the focus operation.
    const current = (await listTaskSpaces()).find(e => e.id === entry.id);
    if (!current || current.ownership !== 'agent' || current.name !== entry.name ||
        current.profileId !== entry.profileId || current.profileName !== entry.profileName) result = {state:'USER_ACTIVE'};
    else {
      const page = target.label ? task.page(target.label) : await task.adopt(target.page);
      await page.cdp('Page.bringToFront', {}, {timeout:3000});
      result = {state:'VISIBLE'};
    }
  }
} else if (entry) result = {state:'USER_ACTIVE'};
console.log('ORDER_PULSE_WORKBENCH:' + JSON.stringify(result));
