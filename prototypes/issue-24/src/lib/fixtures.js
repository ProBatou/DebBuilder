export const views = [
  ['overview','◫'],['packages','▣'],['recipes','▤'],['runs','▥'],['system','◇'],['settings','⚙'],
];
export const scenarios = ['normal','blocker','running','failed','empty','recovery','one','three','manyActions','manyPackages','manyRuns','long'];
export const packageRows = [
  {id:'zoraxy',source:'GitHub Release asset',built:'3.1.4-1',published:'3.1.3-1',status:'readyPublish',tone:'success'},
  {id:'pocket-id',source:'GitHub Release asset',built:'1.8.2-1',published:'1.8.1-1',status:'validationNeeded',tone:'warning'},
  {id:'archive-agent',source:'GitHub source archive',built:'5.0.0-3',published:'—',status:'buildFailed',tone:'danger'},
  {id:'debbuilder',source:'System-managed self-build',built:'1.0.0',published:'1.0.0',status:'upToDate',tone:'success'},
  {id:'maintainerr',source:'GitHub source archive',built:'3.29.0-1',published:'3.28.0-1',status:'validationNeeded',tone:'warning'},
  {id:'seerr',source:'GitHub repository',built:'2.0.0-1',published:'1.9.0-1',status:'readyPublish',tone:'success'},
];
// Represents the repository manager's inventory, which can differ from DebBuilder-managed packages.
export const repositoryInventory = [
  {id:'zoraxy',version:'3.1.4-1',architecture:'amd64'},
  {id:'pocket-id',version:'1.8.2-1',architecture:'amd64'},
  {id:'debbuilder',version:'1.0.0',architecture:'amd64'},
  {id:'seerr',version:'1.9.0-1',architecture:'amd64'},
  {id:'stalwart',version:'0.16.20-2',architecture:'amd64'},
];
export function packagesFor(scenario) {
  if (scenario === 'empty') return [];
  if (scenario !== 'manyPackages' && scenario !== 'long') return packageRows;
  return [...packageRows,...Array.from({length:22},(_,i)=>({id:`service-${String(i+1).padStart(2,'0')}`,source:'GitHub Release asset',built:`1.${i+1}.0-1`,published:`1.${i}.0-1`,status:i%3===0?'readyPublish':i%3===1?'validationNeeded':'upToDate',tone:i%3===1?'warning':'success'}))];
}
export const actionRows = [
  {id:'pocket-id',status:'validationNeeded',reason:'reasonValidate',action:'actionValidate',version:'1.8.2-1',tone:'warning'},
  {id:'zoraxy',status:'readyPublish',reason:'reasonPublish',action:'actionPublish',version:'3.1.4-1',tone:'success'},
  {id:'archive-agent',status:'buildFailed',reason:'reasonBuild',action:'actionReview',tone:'danger'},
  {id:'maintainerr',status:'validationNeeded',reason:'reasonValidate',action:'actionValidate',version:'3.29.0-1',tone:'warning'},
  {id:'seerr',status:'readyPublish',reason:'reasonPublish',action:'actionPublish',version:'2.0.0-1',tone:'success'},
  {id:'service-01',status:'buildFailed',reason:'reasonBuild',action:'actionReview',tone:'danger'},
  {id:'service-02',status:'validationNeeded',reason:'reasonValidate',action:'actionValidate',version:'1.2.0-1',tone:'warning'},
  {id:'service-03',status:'readyPublish',reason:'reasonPublish',action:'actionPublish',version:'1.3.0-1',tone:'success'},
];
export function actionsFor(scenario) {
  if (scenario === 'empty') return [];
  if (scenario === 'one') return actionRows.slice(0,1);
  if (scenario === 'three' || scenario === 'normal') return actionRows.slice(0,3);
  if (scenario === 'recovery') return [{id:'recovery',status:'recoveryBlocked',reason:'reasonRecovery',action:'actionReview',tone:'danger'},...actionRows];
  if (scenario === 'blocker') return [{id:'zoraxy',status:'buildFailed',reason:'reasonBuild',action:'actionReview',tone:'danger'},...actionRows];
  return actionRows;
}
export const runs = [
  {id:'ui-24-validated',pkg:'zoraxy',type:'build',status:'validated',version:'3.1.4-1',at:'2026-09-25T09:42:00Z'},
  {id:'ui-24-running',pkg:'pocket-id',type:'build',status:'running',version:'1.8.2-1',at:'2026-09-25T09:38:00Z'},
  {id:'ui-24-queued',pkg:'maintainerr',type:'build',status:'queued',version:'3.29.0-1',at:'2026-09-25T09:35:00Z'},
  {id:'ui-24-prepared',pkg:'seerr',type:'test',status:'prepared',version:'2.0.0-1',at:'2026-09-25T09:21:00Z'},
  {id:'ui-24-failed',pkg:'archive-agent',type:'validation',status:'failed',version:'5.0.0-3',at:'2026-09-25T08:48:00Z'},
  {id:'ui-24-cancelled',pkg:'service-01',type:'build',status:'cancelled',version:'1.1.0-1',at:'2026-09-25T08:35:00Z'},
  {id:'ui-24-recovery',pkg:'service-02',type:'build',status:'recovery',version:'1.2.0-1',at:'2026-09-25T08:02:00Z'},
  {id:'ui-24-published',pkg:'debbuilder',type:'publication',status:'published',version:'1.0.0',at:'2026-09-24T17:00:00Z'},
];
export function runsFor(scenario) {
  if (scenario==='empty') return [];
  if (scenario!=='manyRuns' && scenario!=='long') return runs;
  return [...runs,...Array.from({length:22},(_,i)=>({id:`ui-24-extra-${i+1}`,pkg:`service-${String(i+1).padStart(2,'0')}`,type:i%3===0?'build':i%3===1?'validation':'publication',status:['completed','failed','prepared','queued'][i%4],version:`1.${i+1}.0-1`,at:new Date(Date.UTC(2026,8,23,12,i)).toISOString()}))];
}
export const recipeRows = [
  {id:'zoraxy',source:'zoraxy/zoraxy',kind:'releaseAsset'},
  {id:'pocket-id',source:'pocket-id/pocket-id',kind:'releaseAsset'},
  {id:'maintainerr',source:'Maintainerr/Maintainerr',kind:'sourceArchive'},
  {id:'seerr',source:'seerr-team/seerr',kind:'repositorySource'},
  {id:'archive-agent',source:'example/archive-agent',kind:'sourceArchive'},
  {id:'worker-agent',source:'example/worker-agent',kind:'repositorySource'},
];
export const stageKeys = ['recipes.source','recipes.detection','runs.dependenciesResolved','runs.stage','recipes.install','packages.built','packages.validation','packages.publication'];
