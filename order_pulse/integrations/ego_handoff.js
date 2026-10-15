// Release only the exact Space acquired by this operation. Never claim to release.
const s=INPUT;
const e=(await listTaskSpaces()).find(e=>e.id===s.space_id&&e.name===s.expected_name&&e.profileId===s.profile_id&&e.profileName===s.profile_name);
let released=false;
if(e?.ownership==='agent') {
  const task=await taskSpace(e.id);
  await task.handOff();
  released=true;
}
console.log('ORDER_PULSE_HANDOFF:'+JSON.stringify({released}));
