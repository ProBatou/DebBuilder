export const views = [
  ['overview','Overview','◫'],['packages','Packages','▣'],['recipes','Recipes','▤'],
  ['runs','Runs','▥'],['system','System','◇'],['settings','Settings','⚙'],
];
export const scenarios = [
  ['normal','Normal'],['blocker','Action required'],['running','Running'],
  ['failed','Failed'],['empty','Empty'],['recovery','Recovery blocked'],
];
export const packageRows = [
  {name:'zoraxy', source:'GitHub Release asset', version:'3.1.4-1', published:'3.1.3-1', status:'Ready to publish', tone:'ready'},
  {name:'pocket-id', source:'GitHub Release asset', version:'1.8.2-1', published:'1.8.1-1', status:'Validation needed', tone:'warning'},
  {name:'archive-agent', source:'GitHub source archive', version:'5.0.0-3', published:'—', status:'Build failed', tone:'danger'},
  {name:'debbuilder', source:'System-managed self-build', version:'1.0.0', published:'1.0.0', status:'Up to date', tone:'success'},
];
export const runStages = ['Source','Detection','Dependencies','Build','Staging','Package','Validation','Publication'];
export const fixtureText = {
  normal: {headline:'All essential services are available', detail:'Two packages need a next action. The repository is accepting publication.', run:'Ready to publish', tone:'ready'},
  blocker: {headline:'A package needs attention', detail:'The service working directory is missing from the install plan. Confirm the proposed directory before Test.', run:'Action required', tone:'danger'},
  running: {headline:'A build is running', detail:'Zoraxy is staging its GitHub Release asset. Logs are updating.', run:'Running', tone:'info'},
  failed: {headline:'Validation failed', detail:'The offline validation attempt could not start the service. Review its diagnostic and adjust the plan.', run:'Failed', tone:'danger'},
  empty: {headline:'No packages yet', detail:'Choose a GitHub repository or Release asset to create your first package.', run:'No runs', tone:'neutral'},
  recovery: {headline:'New builds are blocked by recovery', detail:'An interrupted Run has unresolved command containment. Existing history remains available; admission stays closed.', run:'Recovery blocked', tone:'danger'},
};
