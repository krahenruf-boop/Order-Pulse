const spaces=await listTaskSpaces();
if(!INPUT.selection)console.log('ORDER_PULSE_BINDING:'+JSON.stringify({spaces:spaces.filter(s=>s.name!=='Order Pulse · 素材与客单价'&&(s.recentTabTitles||[]).includes('Order Pulse')).map(s=>({space_id:s.id,expected_name:s.name,profile_id:s.profileId,profile_name:s.profileName}))}));
else{
 const b=INPUT.selection;const matches=spaces.filter(s=>s.id===b.space_id&&s.name===b.expected_name&&s.profileId===b.profile_id&&s.profileName===b.profile_name);
 if(matches.length!==1)throw Error('Selected Space changed');
 const s=matches[0],t=s.ownership==='user'?await claimTaskSpace(s.id):s.ownership==='agentDelegatedToUser'?await takeOverTaskSpace(s.id):await taskSpace(s.id);
 const tabs=(await t.tabs()).filter(x=>x.url==='https://tt.linkme.live/client/dashboard');
 if(tabs.length!==1)throw Error('Exactly one Linkme dashboard is required');
 const p=tabs[0].label?t.page(tabs[0].label):await t.adopt(tabs[0].page);
 console.log('ORDER_PULSE_BINDING:'+JSON.stringify({binding:{...b,purpose:'balance_kalodata',auto_takeover:true,tabs:{balance_tab:p.label,kalodata_tab:null}}}));
}
