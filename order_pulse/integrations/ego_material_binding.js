const spaces = await listTaskSpaces();
const identity = s => ({space_id:s.id, expected_name:s.name, profile_id:s.profileId, profile_name:s.profileName});
if (!INPUT.selection) {
  console.log('ORDER_PULSE_MATERIAL_BINDING:' + JSON.stringify({spaces:spaces
    .filter(s=>(s.recentTabTitles||[]).includes('Order Pulse')).map(identity)}));
} else {
  const b = INPUT.selection;
  const unique = spaces.filter(s=>s.name===b.expected_name && s.profileId===b.profile_id && s.profileName===b.profile_name);
  if (unique.length!==1 || unique[0].id!==b.space_id) throw Error('Selected Space changed');
  const s=unique[0];
  const t=s.ownership==='user'?await claimTaskSpace(s.id):s.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(s.id):await taskSpace(s.id);
  if(t.spaceId!==s.id)throw Error('Selected Space changed');
  const tabs=await t.tabs();
  const url=tab=>{try{return new URL(tab.url);}catch{return null;}};
  const workbenches=tabs.filter(tab=>{const u=url(tab);return u?.origin==='http://127.0.0.1:8797'&&u.pathname==='/'&&['#kalodata','#aov-monitor',''].includes(u.hash);});
  if(workbenches.length!==1 || tabs.some(tab=>url(tab)?.origin==='http://127.0.0.1:8786'))throw Error('Materials workbench required');
  const sourceTabs={
    tms_tab:tabs.filter(tab=>{const u=url(tab);return u?.origin==='https://tms.mabangerp.com'&&u.hash.startsWith('#/myorder');}),
    miaoshou_tab:tabs.filter(tab=>{const u=url(tab);return u?.origin==='https://erp.91miaoshou.com'&&u.pathname==='/order/package/all';}),
    kalodata_tab:tabs.filter(tab=>{const u=url(tab);return u?.origin==='https://www.kalodata.com'&&u.pathname==='/product/detail';})
  };
  if(sourceTabs.tms_tab.length!==1 || sourceTabs.miaoshou_tab.length!==1 || !sourceTabs.kalodata_tab.length)throw Error('Source pages required');
  const labels={};
  for(const [kind,options] of Object.entries(sourceTabs)) {
    const tab=options.find(tab=>tab.label===INPUT.previous?.tabs?.[kind]) || options.find(tab=>tab.label) || options[0];
    const p=tab.label?t.page(tab.label):await t.adopt(tab.page);
    labels[kind]=p.label;
  }
  // Recheck identity after adoption; never follow a concurrent rename or profile change.
  const final=(await listTaskSpaces()).filter(e=>e.id===s.id&&e.name===s.name&&e.profileId===s.profileId&&e.profileName===s.profileName);
  if(final.length!==1)throw Error('Selected Space changed');
  console.log('ORDER_PULSE_MATERIAL_BINDING:'+JSON.stringify({binding:{...identity(s),purpose:'material_aov',auto_takeover:true,tabs:labels}}));
}
