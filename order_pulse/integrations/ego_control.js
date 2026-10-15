// Only the saved identity may be claimed. Idle maintenance never reads a page.
const entries = await listTaskSpaces();
const b = INPUT.binding;
const entry = entries.find(e => e.id === b.space_id && e.name === b.expected_name &&
  e.profileId === b.profile_id && e.profileName === b.profile_name);
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
console.log('ORDER_PULSE_CONTROL:' + JSON.stringify(result));
