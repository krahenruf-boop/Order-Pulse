// Only the saved identity may be claimed. Idle maintenance never reads a page.
const entries = await listTaskSpaces();
const b = INPUT.binding;
// Numeric IDs are session-local. Require the saved logical identity to be unique;
// never follow an old ID that was reassigned to a different Space after restart.
const matches = entries.filter(e => e.name === b.expected_name &&
  e.profileId === b.profile_id && e.profileName === b.profile_name);
const candidate = matches.length === 1 ? matches[0] : null;
const entry = candidate && (candidate.id === b.space_id || candidate.taskId === b.expected_name) ? candidate : null;
let result = {state:'BINDING_REQUIRED'};
if (entry && b.auto_takeover === true) {
  if (entry.ownership === 'agent') result = {state:'CONTROLLED'};
  else if (['user','agentDelegatedToUser'].includes(entry.ownership)) {
    let allowed = INPUT.reason === 'task' || (INPUT.reason === 'balance' && b.purpose === 'balance_kalodata');
    if (INPUT.reason === 'idle' && b.purpose === 'material_aov') {
      // Recheck immediately before claiming: input during metadata lookup resets the grace period.
      try {
        const {execFileSync} = await import('node:child_process');
        const idle = JSON.parse(execFileSync(INPUT.python, [INPUT.idle_script], {encoding:'utf8',timeout:2000}));
        allowed = typeof idle === 'number' && Number.isFinite(idle) && idle >= 300;
      } catch { allowed = false; }
    }
    if (!allowed) result = {state:'USER_ACTIVE'};
    else {
      const task = entry.ownership === 'agentDelegatedToUser' ? await takeOverTaskSpace(entry.id) : await claimTaskSpace(entry.id);
      result = {state:task.spaceId === entry.id ? 'CONTROLLED' : 'BINDING_REQUIRED'};
    }
  } else result = {state:'UNAVAILABLE'};
}
if (entry && ['CONTROLLED','USER_ACTIVE'].includes(result.state)) result.browser = {
  space_id:entry.id, space_name:entry.name, profile_id:entry.profileId, profile_name:entry.profileName
};
console.log('ORDER_PULSE_CONTROL:' + JSON.stringify(result));
