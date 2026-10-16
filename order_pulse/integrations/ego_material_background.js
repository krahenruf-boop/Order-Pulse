// Keep normal DOM rendering alive in Kalodata tabs without activating a tab.
// This lease never navigates, reads site data, or claims a user-owned Space.
const fs = await import('node:fs/promises');
const b = INPUT.binding, sessions = new Map();
let task;
const owned = async () => {
  const matches = (await listTaskSpaces()).filter(e => e.name === b.expected_name &&
    e.profileId === b.profile_id && e.profileName === b.profile_name);
  const e = matches.length === 1 ? matches[0] : null;
  return e && e.ownership === 'agent' && (e.id === b.space_id || e.taskId === b.expected_name) ? e : null;
};
const emulate = async (sessionId, enabled) => task.cdp('Target.sendMessageToTarget', {
  sessionId, message:JSON.stringify({id:enabled ? 1 : 2, method:'Emulation.setFocusEmulationEnabled', params:{enabled}})
});
async function syncTabs() {
  const tabs = await task.tabs();
  for (const tab of tabs) {
    let u;try { u = new URL(tab.url); } catch { continue; }
    if (u.origin !== 'https://www.kalodata.com' || !['/product/detail','/video/detail'].includes(u.pathname) ||
        sessions.has(tab.targetId)) continue;
    if (sessions.size >= 20) throw Error('PAGE_LIMIT');
    try {
      const {sessionId} = await task.cdp('Target.attachToTarget', {targetId:tab.targetId, flatten:false});
      sessions.set(tab.targetId,sessionId);
      await emulate(sessionId,true);
    } catch (error) {
      // A detail tab can close between the inventory and attachment.
      if ((await task.tabs()).some(t => t.targetId === tab.targetId)) throw error;
    }
  }
  // Detail tabs are short-lived. Remove closed sessions from the local lease.
  const live = new Set(tabs.map(t => t.targetId));
  for (const [id, session] of sessions) if (!live.has(id)) {
    await emulate(session,false).catch(()=>{});
    await task.cdp('Target.detachFromTarget',{sessionId:session}).catch(()=>{});
    sessions.delete(id);
  }
}
try {
  const e = await owned();
  if (!e) throw Error('BINDING_REQUIRED');
  task = await taskSpace(e.id);
  await syncTabs();
  await fs.writeFile(INPUT.ready,'READY',{mode:0o600});
  const until = Date.now() + 1900000;
  while (Date.now() < until && await fs.stat(INPUT.lease).then(()=>true,()=>false)) {
    try { process.kill(INPUT.owner_pid,0); } catch { break; }
    if (!await owned()) break;
    await syncTabs();
    await new Promise(r=>setTimeout(r,250));
  }
} catch {
  await fs.writeFile(INPUT.ready,'UNAVAILABLE',{mode:0o600}).catch(()=>{});
} finally {
  for (const session of sessions.values()) {
    await emulate(session,false).catch(()=>{});
    await task.cdp('Target.detachFromTarget',{sessionId:session}).catch(()=>{});
  }
}
