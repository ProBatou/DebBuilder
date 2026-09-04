// --- Admin console layer: Package / Recipe / Execution navigation ---
const adminState = {packages: [], executions: [], selectedPackage: null, selectedExecution: null, executionAction: null, logPollTimer: null, logOffset: 0, logVerbosity: 'normal', logAutoScroll: true, logFollowing: false};
function badge(status){ const value=status||'unknown'; return `<span class="badge ${esc(value)}">${esc(STATUS_LABELS[value]||value)}</span>`; }
function packageLabelForExecution(e){ if(e.package) return e.package; const match = adminState.packages.find(p => e.id && e.id.includes(p.name)); return match ? match.name : (e.action || 'Operation'); }
function switchView(name){ closeMobileNav(); closeLogDetail(); if(name!=='settings'&&typeof flushSettingsAutosave==='function') flushSettingsAutosave().catch(()=>{}); if(name!=='logs') stopLogPolling(); document.querySelectorAll('.nav-link').forEach(b=>b.classList.toggle('active', b.dataset.view===name)); document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active', v.id==='view-'+name)); if(name==='packages') loadPackages(); if(name==='logs') loadExecutions(); if(name==='settings') loadSettings(); if(name==='dashboard') loadDashboard(); }
function dashboardLifecycleState(p){
  return lifecycleState(p);
}
function metricCard(value,label,detail='',tone=''){
  return `<div class="metric dashboard-metric ${tone}"><strong>${esc(value)}</strong><span>${esc(label)}</span>${detail?`<em>${esc(detail)}</em>`:''}</div>`;
}
function compactPackageRow(p){
  const v=p.version||{}; const state=dashboardLifecycleState(p);
  return `<div class="dashboard-package-row" role="button" onclick="switchView('packages'); openPackage('${esc(p.name)}')"><div class="dashboard-package-identity"><strong>${esc(p.name)}</strong><span>${esc(sourceLabel(p))}</span></div><div class="dashboard-package-version"><span>Published</span><strong>${esc(v.published||p.apt_version||'—')}</strong></div><div class="dashboard-package-version"><span>Source</span><strong>${esc(v.source||p.upstream_version||'—')}</strong></div><div class="dashboard-package-state">${badge(state)}</div></div>`;
}
function renderRepoState(settings, packages){
  const apt=(settings||{}).apt||{}; const archs=[...new Set(packages.map(p=>p.architecture||'all'))].sort();
  const healthy=!!(apt.repository&&apt.distribution&&apt.component);
  const repository=String(apt.repository||'Not configured').replace(/^https?:\/\//,'').replace(/\/$/,'');
  return `<div class="dashboard-repo-summary"><h3>APT repository</h3><span>${esc(repository)} · ${esc(apt.distribution||'—')} · ${esc(apt.component||'—')} · ${esc(archs.join(', ')||apt.architecture||'—')}</span>${statusBadge(healthy?'CONFIGURED':'CHECK',healthy?'active':'warning')}</div>`;
}
async function loadDashboard(){
  const [dash,pkgData,settingsData]=await Promise.all([getJson('/api/dashboard'),getJson('/api/packages'),getJson('/api/settings')]);
  const d=dash.dashboard||{}; const packages=pkgData.packages||[]; const counts={total:d.packages||0,update_available:d.updates||0,publication_available:d.ready_to_publish||0,failed:d.package_errors||0,github:d.github_sources||0,local:d.local_sources||0,with_recipe:d.linked_recipes||0};
  $('dashboardMetrics').innerHTML=[
    metricCard(counts.total,'Tracked packages',`${counts.github} GitHub · ${counts.local} local`),
    metricCard(counts.update_available,'Source updates','source > published',counts.update_available?'warning':''),
    metricCard(counts.publication_available,'Ready to publish','verified build not published',counts.publication_available?'success':''),
    metricCard(counts.failed + (d.errors||0),'Alerts / errors','packages or executions',counts.failed||d.errors?'danger':''),
  ].join('');
  if($('dashboardRepoState')) $('dashboardRepoState').innerHTML=renderRepoState(settingsData.settings, packages);
  const priority=packages.slice().sort((a,b)=>{
    const rank={build_failed:0,validation_failed:0,publication_failed:0,failed:0,validation_needed:1,validating:1,ready_to_publish:2,publishing:2,update_available:3,publication_available:3,build_available:4,not_published:4,recipe_missing:5,published:9,up_to_date:9,ready:9,unknown:8};
    return (rank[dashboardLifecycleState(a)]??7)-(rank[dashboardLifecycleState(b)]??7) || a.name.localeCompare(b.name);
  }).slice(0,12);
  if($('dashboardPackageFlow')) $('dashboardPackageFlow').innerHTML=priority.map(compactPackageRow).join('') || '<p class="muted">No tracked packages.</p>';
  $('latestOperations').classList.add('latest-ops');
  $('latestOperations').innerHTML=(d.latest_operations||[]).map(e=>`<div class="item" role="button" onclick="switchView('logs'); openExecution('${esc(e.id)}')"><div class="item-title"><span>${esc(packageLabelForExecution(e))} · ${esc(e.action||'build')}</span>${badge(e.lifecycle_status||e.status)}</div><div class="item-meta">Build ${esc(e.build_status||e.status)} · ${esc(STATUS_LABELS[e.lifecycle_status]||e.lifecycle_status||e.status)} · ${esc(e.id)} · ${fmtTime(e.updated)}</div></div>`).join('') || '<p class="muted">No recent operation.</p>';
}
function lifecycleState(p){ return p.lifecycle_display_status || p.lifecycle_state || p.status || 'unknown'; }
function sourceLabel(p){ const src=p.source||{}; if(src.repository) return `${src.type || 'github'} · ${src.repository}`; return src.type || 'local'; }
function sourceRefLabel(p){ const src=p.source||{}; return src.release || src.tag || src.branch || src.commit || src.default_branch || '—'; }
function packageMatches(p){ const q=($('packageSearch')?.value||'').toLowerCase(); const f=$('packageFilter')?.value||'all'; const text=[p.name,p.apt_version,p.upstream_version,p.architecture,p.recipe,(p.source||{}).repository,lifecycleState(p)].join(' ').toLowerCase(); return (!q||text.includes(q)) && (f==='all'||p.status===f||lifecycleState(p)===f); }
function packageByName(name){ return adminState.packages.find(p=>p.name===name); }
function packageVersionLabel(p){ return p.apt_version || (p.version||{}).published || 'not published'; }
function recipeIdFromPackageName(name){ return name.replace(/[^a-zA-Z0-9_.+-]/g,'-'); }
function renderPackageOptions(){
  if($('packageOptions')) $('packageOptions').innerHTML=adminState.packages.map(p=>`<option value="${esc(p.name)}">${esc(packageVersionLabel(p))}</option>`).join('');
  const select=$('newRecipePackage');
  if(!select) return;
  const previous=select.value;
  const available=adminState.packages.filter(p=>!p.recipe);
  select.innerHTML=available.map(p=>`<option value="${esc(p.name)}">${esc(p.name)} · ${esc(packageVersionLabel(p))}</option>`).join('') || '<option value="">No package available without a recipe</option>';
  select.disabled=available.length===0;
  if(previous && available.some(p=>p.name===previous)) select.value=previous;
  if(!select.value && available.length) select.selectedIndex=0;
  syncNewRecipeFromPackage();
}
async function loadPackages(){ const data=await getJson('/api/packages'); adminState.packages=data.packages||[]; renderPackageOptions(); renderPackages(); }
function scheduleBackgroundPackageRefresh(){ [2500,8000].forEach(delay=>setTimeout(()=>loadPackages().catch(()=>{}),delay)); }
function actionButtons(p){
  const name=esc(p.name); const state=lifecycleState(p); const buttons=[];
  if(p.recipe) buttons.push(`<button class="btn-primary" onclick="openLinkedRecipe('${esc(p.recipe)}')">Open recipe</button>`);
  else buttons.push(`<button class="btn-primary" onclick="createRecipeForPackage('${name}')">Create recipe</button>`);
  if(p.recipe) buttons.push(`<button class="btn-warning" onclick="buildPackage('${name}',true)">Test</button>`);
  if(p.recipe && BUILDABLE_PACKAGE_STATES.has(state)) buttons.push(`<button class="btn-success" onclick="buildPackage('${name}',false)">Build</button>`);
  if((p.build||{}).last_artifact) buttons.push(`<button class="btn-primary" onclick="verifyPackageDeb('${name}')">Verify</button>`);
  if((state==='validation_needed'||state==='validation_failed')&&(p.build||{}).latest_run_id) buttons.push(`<button class="btn-primary" onclick="validatePackage('${name}')">${state==='validation_failed'?'Revalidate':'Validate'}</button>`);
  else if((p.build||{}).latest_run_id&&(p.build||{}).last_artifact) buttons.push(`<button class="btn-primary" onclick="validatePackage('${name}')">Revalidate</button>`);
  if(state==='ready_to_publish'||state==='publication_failed'||state==='publication_available') buttons.push(`<button class="btn-danger" onclick="publishPackage('${name}')">Publish</button>`);
  return buttons.join('');
}
function renderPackages(){
  const rows=adminState.packages.filter(packageMatches);
  if($('packageCount')) $('packageCount').textContent=`${rows.length} shown`;
  $('packageList').innerHTML=rows.map(p=>{
    const v=p.version||{};
    return `<article class="package-row lifecycle" onclick="openPackage('${esc(p.name)}')"><div><div class="package-name">${esc(p.name)}</div><div class="package-sub">${esc(sourceLabel(p))}${p.recipe ? ` · recipe ${esc(p.recipe)}` : ''}</div></div><div>${badge(lifecycleState(p))}</div><div><div class="package-sub">Published version</div><strong>${esc(v.published||p.apt_version||'Not published')}</strong></div><div><div class="package-sub">Available version</div><strong>${esc(v.source||p.upstream_version||'Unknown')}</strong><div class="package-sub">Ref ${esc(sourceRefLabel(p))}</div></div><div><div class="package-sub">Built / arch</div><strong>${esc(v.candidate||'Never built')} · ${esc(p.architecture||'all')}</strong><div class="package-sub">Latest run: ${esc((p.build||{}).latest_status||'none')}</div></div></article>`;
  }).join('') || '<p class="muted">No package.</p>';
}
function closePackageDrawer(){ $('packageDrawer')?.classList.remove('open'); $('packageDrawer')?.setAttribute('aria-hidden','true'); }
async function openPackage(name){
  adminState.selectedPackage=name;
  const p=adminState.packages.find(packageRow=>packageRow.name===name);
  if(!p) throw new Error('Package not found in the loaded list');
  const src=p.source||{}; const v=p.version||{}; const b=p.build||{}; const repo=p.repository||{};
  if($('packageDrawerTitle')) $('packageDrawerTitle').textContent=p.name;
  $('packageDrawer')?.classList.add('open'); $('packageDrawer')?.setAttribute('aria-hidden','false');
  const validation=p.validation||{}; const publication=p.publication||{}; const artifactName=(b.last_artifact||'').split('/').pop(); const artifact=[artifactName,b.artifact_source,b.artifact_sha256].filter(Boolean).join(' · ');
  $('packageDetail').innerHTML=`<div class="package-lifecycle-panel"><section><h3>General</h3><dl><dt>Linked recipe</dt><dd>${esc(p.recipe||'None')}</dd><dt>Description</dt><dd>${esc(p.description||'Not set')}</dd><dt>Architecture</dt><dd>${esc(p.architecture||'all')}</dd><dt>Dependencies</dt><dd>${esc(p.depends||'None declared')}</dd></dl></section><section><h3>Source</h3><dl><dt>Type</dt><dd>${esc(src.type||'Unknown')}</dd><dt>Repository</dt><dd>${esc(src.repository||'Not set')}</dd><dt>Strategy</dt><dd>${esc(p.tracking||p.version_strategy||v.strategy||'Not set')}</dd><dt>Resolved ref</dt><dd>${esc(sourceRefLabel(p))}</dd><dt>Latest resolved release</dt><dd>${esc(src.latest_release||'Not fetched')}</dd></dl></section><section><h3>Versions</h3><dl><dt>Available upstream version</dt><dd>${esc(v.source||p.upstream_version||'Unknown')}</dd><dt>Latest built version</dt><dd>${esc(v.candidate||'None')}</dd><dt>Published version</dt><dd>${esc(v.published||p.apt_version||'Not published')}</dd><dt>Current lifecycle</dt><dd>${badge(lifecycleState(p))}</dd></dl></section><section><h3>Build</h3><dl><dt>Method</dt><dd>${esc(b.method||'Not set')}</dd><dt>Latest run status</dt><dd>${esc(b.latest_status||'No real run')}</dd><dt>Latest run</dt><dd>${esc(b.latest_run_id||'None')}</dd><dt>Artifact run</dt><dd>${esc(b.last_build_id||'None')}</dd><dt>Latest artifact</dt><dd>${esc(artifact||'None')}</dd></dl></section><section><h3>Validation & publication</h3><dl><dt>Current run validation</dt><dd>${validation.status?`${esc(validation.status)} · ${esc(validation.finished_at||validation.started_at||'')}`:'Not run'}</dd><dt>Current run publication</dt><dd>${publication.status?`${esc(publication.status)} · ${esc(publication.finished_at||publication.requested_at||'')}`:'Not run'}</dd><dt>Repository version</dt><dd>${esc(v.published||p.apt_version||'None')} remains published</dd></dl></section><section><h3>APT repository</h3><dl><dt>Repository</dt><dd>${esc(repo.url||'Not configured')}</dd><dt>Distribution</dt><dd>${esc(repo.distribution||'Not configured')}</dd><dt>Component</dt><dd>${esc(repo.component||'Not configured')}</dd><dt>Architectures</dt><dd>${esc((repo.architectures||[p.architecture||'all']).join(', '))}</dd><dt>Publication</dt><dd>${repo.published ? 'Published' : 'Not published'}</dd></dl></section></div><div class="actions package-actions">${actionButtons(p)}</div><h3>History</h3>${(p.history||[]).map(e=>`<div class="item" onclick="switchView('logs'); closePackageDrawer(); openExecution('${esc(e.id)}')"><div class="item-title"><span>${esc(e.id)} · ${esc(e.action)}</span>${badge(e.lifecycle_status||e.status)}</div><div class="item-meta">${fmtTime(e.updated)}</div></div>`).join('') || '<p class="muted">No linked history.</p>'}<div class="danger-zone"><button class="danger" onclick="deletePackageUi('${esc(p.name)}')">Delete from DebBuilder</button><p class="muted">Does not delete the package from the APT repository.</p></div>`;
}
async function createPackageUi(){ const name=prompt('Debian package name?'); if(!name) return; const repo=prompt('Optional GitHub repository (owner/repo)?',''); const body={name, architecture:'all', source: repo?{type:'github', repository:repo}:{type:'manual'}}; await postJson('/api/packages', body); await loadPackages(); await openPackage(name); }
async function openLinkedRecipe(recipe){ closePackageDrawer(); switchView('recipes'); await refreshWorkflows(); $('workflowSelect').value=recipe; await loadSelectedWorkflow(); }
async function createRecipeForPackage(name){ closePackageDrawer(); switchView('recipes'); await loadPackages(); newRecipeUi(name); }
async function deletePackageUi(name){ if(!confirm(`Delete ${name} from DebBuilder?\nThe package will NOT be removed from the APT repository.`)) return; const r=await fetch('/api/packages/'+encodeURIComponent(name), {method:'DELETE'}); const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText); closePackageDrawer(); await loadPackages(); }
async function verifyPackageDeb(name){ const deb=prompt('Path to the .deb to verify', ''); if(!deb) return; const res=await postJson('/api/packages/'+encodeURIComponent(name)+'/verify-deb', {deb}); alert(`.deb verification: ${res.verification.ok ? 'OK' : 'ERROR'}\nPackage: ${res.verification.package}\nVersion: ${res.verification.version}\nArchitecture: ${res.verification.architecture}`); }
async function validatePackage(name){
  const p=adminState.packages.find(row=>row.name===name); const runId=p?.build?.latest_run_id;
  if(!runId) throw new Error('No successful Build Run is ready to validate');
  const res=await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/validate`,{});
  alert(`Validation: ${res.validation.status}${res.validation.error ? `\n${res.validation.error.message}` : ''}`); await loadPackages(); await openPackage(name);
}
async function publishPackage(name){
  const p=adminState.packages.find(row=>row.name===name); const build=p?.build||{}; const version=(p?.version||{}).candidate||'';
  const runId=build.latest_run_id||build.last_build_id;
  if(!runId||!version) throw new Error('No validated Build Run is ready to publish');
  const confirmation=`publish:${name}:${version}`;
  if(!confirm(`Publish the validated artifact ${name} ${version} to APT?\n\nRequired confirmation: ${confirmation}`)) return;
  const res=await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/publish`,{confirm:confirmation});
  alert(`Publication: ${res.publication.status}${res.publication.error ? `\n${res.publication.error.message}` : ''}`); await loadPackages();
}
async function buildPackage(name,dryRun=true){
  const p=adminState.packages.find(x=>x.name===name); if(!p||!p.recipe){ alert('No linked recipe.'); return; }
  if(!dryRun && !confirm(`Really build ${name} and validate the resulting package?`)) return;
  const wf=await getJson('/api/workflows/'+encodeURIComponent(p.recipe));
  try {
    const run=await postJson('/api/run',{workflow:wf,dry_run:dryRun});
    alert(dryRun ? `Test finished: ${run.run_id} · code ${run.returncode}` : `Build finished: ${run.run_id} · status ${run.status || run.returncode}`);
    await Promise.all([loadExecutions(),loadPackages()]);
    await openPackage(name);
  } catch(error) {
    alert(`${dryRun ? 'Test' : 'Build'} failed: ${error.message}`);
  }
}
async function duplicateRecipe(id){ const wf=await getJson('/api/workflows/'+encodeURIComponent(id)); const name=prompt('Copy name', id+'-copy'); if(!name)return; wf.name=name; const newId=wf.name.replace(/[^a-zA-Z0-9_.+-]/g,'-'); await postJson('/api/workflows/'+newId,{workflow:wf}); await refreshWorkflows(); $('workflowSelect').value=newId; await loadSelectedWorkflow(); }
function syncNewRecipeFromPackage(packageName){
  const select=$('newRecipePackage');
  const selectedName=packageName || select?.value || '';
  const pkg=packageByName(selectedName);
  if(select && packageName && Array.from(select.options).some(option=>option.value===packageName)) select.value=packageName;
  if($('newRecipeName')) $('newRecipeName').value=selectedName;
  if($('newRecipeGithub')) $('newRecipeGithub').value=pkg?.source?.repository || '';
}
function newRecipeUi(packageName=''){
  renderPackageOptions();
  syncNewRecipeFromPackage(packageName);
  $('newRecipeDialog')?.showModal();
  ($('newRecipePackage')?.disabled ? $('cancelNewRecipe') : $('newRecipeGithub'))?.focus();
}
async function createRecipeFromDialog(){
  const packageName=$('newRecipePackage').value.trim();
  if(!packageName){ alert('Create a package first, then attach a recipe to it.'); return; }
  const workflow={name:packageName,package_name:packageName,github_repository:$('newRecipeGithub').value.trim(),version_tracking:$('newRecipeTracking').value,version_source:$('newRecipeVersionSource').value,active:true,steps:[]};
  if (workflow.version_tracking !== 'latest_release') workflow.source={repository:workflow.github_repository,tracking:workflow.version_tracking,ref:$('newRecipeSourceRef').value.trim(),version:{source:workflow.version_source}};
  if (workflow.version_source === 'regex') workflow.version_expression = $('newRecipeVersionExpression').value.trim();
  currentRecipeId=recipeIdFromPackageName(packageName); renderWorkflow(workflow); $('recipeTitle').textContent=workflow.name; $('newRecipeDialog').close(); switchView('recipes'); autosaveRevision += 1; await saveRecipeNow(); await Promise.all([refreshWorkflows(),loadPackages()]); $('workflowSelect').value=currentRecipeId;
}
async function loadExecutions(){ const data=await getJson('/api/executions'); adminState.executions=data.executions||[]; renderExecutions(); }
function executionIsSelected(id){ return adminState.selectedExecution?.id===id; }
function shortExecutionId(id){ const value=String(id||''); return value.length>18?`${value.slice(0,13)}...${value.slice(-4)}`:value; }
function renderExecutions(){ const q=($('logSearch')?.value||'').toLowerCase(); const st=$('logStatus')?.value||'all'; const rows=adminState.executions.filter(e=>(st==='all'||e.status===st||e.lifecycle_status===st)&&(!q||JSON.stringify(e).toLowerCase().includes(q))); $('executionList').innerHTML=rows.map(e=>`<div class="item execution-item ${executionIsSelected(e.id)?'active':''}" role="option" tabindex="0" aria-selected="${executionIsSelected(e.id)?'true':'false'}" data-execution-id="${esc(e.id)}" onclick="openExecution('${esc(e.id)}')"><div class="execution-item-body"><div class="item-title"><span>${esc(packageLabelForExecution(e))} · ${esc(e.action||'run')}</span>${badge(e.lifecycle_status||e.status)}</div><div class="item-meta">${fmtTime(e.updated)} · ${esc(shortExecutionId(e.id))}</div></div></div>`).join('') || '<p class="muted logs-empty-message">No logs available.</p>'; document.querySelector('.logs-layout')?.classList.toggle('logs-empty', adminState.executions.length===0); }

function executionIsLive(e){ return ['pending','running'].includes(e?.status) || ['running'].includes((e?.validations||[]).slice(-1)[0]?.status) || ['running'].includes((e?.publications||[]).slice(-1)[0]?.status); }
function stopLogPolling(){ if(adminState.logPollTimer) clearTimeout(adminState.logPollTimer); adminState.logPollTimer=null; adminState.logFollowing=false; updateLogLiveBadge(); }
function logIsNearBottom(){ const node=$('executionDetail'); return !node || node.scrollHeight-node.scrollTop-node.clientHeight<24; }
function middleTruncate(value, limit=28){
  const text=String(value ?? '');
  if(text.length<=limit) return text;
  const head=Math.max(8, Math.ceil((limit-1)/2));
  const tail=Math.max(6, limit-1-head);
  return `${text.slice(0,head)}…${text.slice(-tail)}`;
}
function metaValueHtml(key,value){
  const text=String(value ?? '—');
  const longKeys=new Set(['Run ID','Source','Resolved ref','Artifact','SHA-256']);
  const isLong=longKeys.has(key)||text.length>32;
  if(!isLong) return `<strong class="meta-value">${esc(text)}</strong>`;
  return `<button type="button" class="meta-value meta-copy-value" title="${esc(text)}" data-copy-value="${esc(text)}">${esc(middleTruncate(text,key==='SHA-256'?22:30))}</button>`;
}
async function copyTextValue(value){
  if(navigator.clipboard?.writeText){ await navigator.clipboard.writeText(value); return; }
  const input=document.createElement('textarea');
  input.value=value; input.style.position='fixed'; input.style.opacity='0';
  document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
}
function updateLogLiveBadge(){
  const node=$('btnLogLiveBadge'); if(!node) return;
  const active=adminState.selectedExecution && adminState.logFollowing && executionIsLive(adminState.selectedExecution);
  node.hidden=!active;
  if(!active) return;
  node.textContent=adminState.logAutoScroll?'● Live':'↓ Jump to latest';
  node.classList.toggle('paused', !adminState.logAutoScroll);
}
function setLogAutoScroll(enabled){ adminState.logAutoScroll=!!enabled; updateLogLiveBadge(); }
function executionCanValidateAgain(execution){
  return execution?.mode==='build' && execution.status==='success' && !!execution.artifact?.path;
}
function updateExecutionValidationButton(execution){
  const button=$('btnRevalidateExecution'); if(!button) return;
  const pending=adminState.executionAction?.id===execution?.id&&adminState.executionAction.type==='validation';
  const validation=(execution?.validations||[]).slice(-1)[0]||{};
  button.hidden=!executionCanValidateAgain(execution);
  button.disabled=!!pending;
  button.textContent=pending?'Validating…':(validation.status?'Revalidate':'Validate');
}
async function loadExecutionLog(id,{reset=false}={}){
  const shouldStick=adminState.logAutoScroll || logIsNearBottom();
  if(reset){ adminState.logOffset=0; if($('executionDetail')) $('executionDetail').textContent=''; }
  const response=await fetch(`/api/executions/${encodeURIComponent(id)}/logs?verbosity=${encodeURIComponent(adminState.logVerbosity)}&after=${adminState.logOffset}`);
  const payload=await response.json();
  if(!response.ok) throw new Error(payload.error||response.statusText);
  const log=payload.log||{};
  if(log.text){ $('executionDetail').textContent += log.text; if(shouldStick){ setLogAutoScroll(true); $('executionDetail').scrollTop=$('executionDetail').scrollHeight; } else setLogAutoScroll(false); }
  adminState.logOffset=log.offset||0;
  return log;
}
async function pollOpenExecution(){
  const selected=adminState.selectedExecution;
  if(!selected) return;
  try{
    const detail=(await getJson('/api/executions/'+encodeURIComponent(selected.id))).execution;
    adminState.selectedExecution=detail;
    renderExecutions();
    renderOpenExecution(detail,{preserveLog:true});
    const log=await loadExecutionLog(detail.id);
    if(executionIsLive(detail)&&!log.complete){ adminState.logFollowing=true; updateLogLiveBadge(); adminState.logPollTimer=setTimeout(pollOpenExecution,1500); }
    else stopLogPolling();
  }catch(_error){ stopLogPolling(); }
}
async function deleteExecutionLog(id){
  const row=adminState.executions.find(item=>item.id===id)||adminState.selectedExecution||{id};
  if(!confirm(`Delete log/history for this execution?\n\nPackage: ${packageLabelForExecution(row)}\nRun ID: ${row.id}\nDate: ${fmtTime(row.updated||row.created_at_epoch)}\n\nThis does not delete any Recipe, package, published APT entry, or build artifact.`)) return;
  const response=await fetch(`/api/executions/${encodeURIComponent(id)}/logs`,{method:'DELETE'});
  const payload=await response.json();
  if(!response.ok) throw new Error(payload.error||response.statusText);
  stopLogPolling();
  await loadExecutions();
  if(adminState.selectedExecution?.id===id) await openExecution(id);
}
function changeLogVerbosity(value){
  const shouldStick=adminState.logAutoScroll || logIsNearBottom();
  adminState.logVerbosity=['compact','normal','verbose','raw'].includes(value)?value:'normal';
  setLogAutoScroll(shouldStick);
  if(adminState.selectedExecution) loadExecutionLog(adminState.selectedExecution.id,{reset:true}).catch(error=>alert(error.message));
}
function handleLogScroll(){ if(!adminState.selectedExecution || !executionIsLive(adminState.selectedExecution)) return; setLogAutoScroll(logIsNearBottom()); }
function resumeLiveLog(){ setLogAutoScroll(true); const node=$('executionDetail'); if(node) node.scrollTop=node.scrollHeight; }

function lifecycleStatusText(status) {
  return ({not_run:'Not run',running:'Running',success:'Success',failed:'Failed',prepared:'Prepared'})[status]||status||'Not run';
}

function lifecycleSymbol(status) {
  return ({success:'✓',failed:'✕',running:'◌',not_run:'—',prepared:'—'})[status]||'—';
}

function lifecycleFailureDetails(entry, label) {
  if(entry.status!=='failed') return '';
  const error=entry.error||{};
  return `<details class="lifecycle-error"><summary>${esc(label)} details</summary><pre>${esc(JSON.stringify(error,null,2))}</pre></details>`;
}

function renderExecutionLifecycle(execution) {
  const node=$('executionLifecycle'); if(!node) return;
  const pending=adminState.executionAction?.id===execution.id?adminState.executionAction.type:'';
  const model=executionLifecycleModel(execution,pending);
  const validateButton=model.canValidate?`<button type="button" class="btn-primary" data-execution-action="validate" onclick="validateExecution('${esc(execution.id)}')" ${model.validationDisabled?'disabled':''}>${model.validationDisabled?'Validating…':(model.validation.status?'Revalidate':'Validate')}</button>`:'';
  const publishButton=model.canPublish?`<button type="button" class="btn-danger" data-execution-action="publish" onclick="publishExecution('${esc(execution.id)}')" ${model.publicationDisabled?'disabled':''}>${model.publicationDisabled?'Publishing…':'Publish'}</button>`:'';
  const validationFailure=model.validationStatus==='failed'?lifecycleFailureDetails(model.validation,'Validation'):'';
  const publicationFailure=model.publicationStatus==='failed'?lifecycleFailureDetails(model.publication,'Publication'):'';
  node.innerHTML=`<div class="lifecycle-row"><strong>Current lifecycle</strong><span>${badge(execution.lifecycle_status||'unknown')}</span></div><div class="lifecycle-row ${esc(model.buildStatus)}"><strong>Build</strong><span>${lifecycleSymbol(model.buildStatus)} ${esc(lifecycleStatusText(model.buildStatus))}</span></div><div class="lifecycle-row ${esc(model.validationStatus)}"><strong>Validation</strong><span>${lifecycleSymbol(model.validationStatus)} ${esc(lifecycleStatusText(model.validationStatus))}</span>${validateButton}${validationFailure}</div><div class="lifecycle-row ${esc(model.publicationStatus)}"><strong>Publication</strong><span>${lifecycleSymbol(model.publicationStatus)} ${esc(model.publicationStatus==='success'?'Published':lifecycleStatusText(model.publicationStatus))}</span>${publishButton}${publicationFailure}</div>`;
  node.hidden=false;
}

async function postLifecycleJson(url, body) {
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const payload=await response.json();
  const recordedLifecycleFailure=response.status===422&&(payload.validation||payload.publication);
  if(!response.ok&&!recordedLifecycleFailure){const error=payload.error;throw new Error(error?.message||error||response.statusText);}
  return payload;
}

async function refreshExecutionLifecycle(id) {
  await Promise.all([loadExecutions(),loadPackages()]);
  await openExecution(id);
}

async function validateExecution(id) {
  if(adminState.executionAction) return;
  adminState.executionAction={id,type:'validation'};
  if(adminState.selectedExecution?.id===id) updateExecutionValidationButton(adminState.selectedExecution);
  try { await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/validate`,{}); }
  catch(error) { alert(`Validation could not start: ${error.message}`); }
  finally { adminState.executionAction=null; await refreshExecutionLifecycle(id); }
}

async function publishExecution(id) {
  if(adminState.executionAction) return;
  const execution=(await getJson('/api/executions/'+encodeURIComponent(id))).execution;
  const inspection=execution.artifact?.inspection||{};
  const version=typeof execution.version==='object'?execution.version.debian:execution.version;
  const packageName=inspection.package||execution.package||execution.recipe_id;
  const packageVersion=inspection.version||version;
  const confirmation=`publish:${packageName}:${packageVersion}`;
  if(!confirm(`Publish the validated artifact ${packageName} ${packageVersion} to APT?\n\nRequired confirmation: ${confirmation}`)) return;
  adminState.executionAction={id,type:'publication'};
  if(adminState.selectedExecution?.id===id) renderExecutionLifecycle(adminState.selectedExecution);
  try { await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/publish`,{confirm:confirmation}); }
  catch(error) { alert(`Publication could not start: ${error.message}`); }
  finally { adminState.executionAction=null; await refreshExecutionLifecycle(id); }
}

function renderOpenExecution(e,{preserveLog=false}={}){
  const artifact=e.artifact||{}; const validation=(e.validations||[]).slice(-1)[0]||{}; const publication=(e.publications||[]).slice(-1)[0]||{};
  const source=(e.steps||[]).find(step=>step.name==='source')?.details||{}; const staging=(e.steps||[]).find(step=>step.name==='staging')?.details||{};
  const version=typeof e.version==='object'?e.version:{debian:e.version};
  const meta=[['Run ID','#'+e.id],['Package',e.package||e.recipe_id||'—'],['Recipe',e.recipe_id||'—'],['Mode',e.mode||e.action||'—'],['Source',source.repository||'—'],['Resolved ref',source.ref||source.tag||'—'],['Upstream',version.upstream||'—'],['Debian version',version.debian||'—'],['Build status',e.status],['Date',fmtTime(e.updated||e.created_at_epoch)],['Artifact',(artifact.path||'').split('/').pop()||'—'],['Size',artifact.size||'—'],['SHA-256',artifact.sha256||'—'],['Validation',validation.status||'Not run'],['Publication',publication.status||'Not run']];
  const symbols={pending:'○',running:'◌',success:'✓',failed:'✕',skipped:'–'}; const buildStep=(e.steps||[]).find(step=>step.name==='build');
  if($('executionMeta')) $('executionMeta').innerHTML=meta.map(([k,v])=>`<div class="meta-cell"><span>${esc(k)}</span>${metaValueHtml(k,v)}</div>`).join('');
  if(validation.profile&&$('executionMeta')){const node=(validation.checks||[]).find(check=>check.name==='toolchain_node');$('executionMeta').insertAdjacentHTML('beforeend',`<div class="meta-cell"><span>Validation backend</span>${metaValueHtml('Validation backend',validation.backend?.runtime||'—')}</div><div class="meta-cell"><span>Profile</span>${metaValueHtml('Profile',validation.profile.name||'—')}</div><div class="meta-cell"><span>Node</span>${metaValueHtml('Node',node?.details?.actual||'Not required')}</div><div class="meta-cell"><span>Network</span>${metaValueHtml('Network',validation.backend?.network||'disabled')}</div>`);}
  if($('executionSteps')) $('executionSteps').innerHTML=(e.steps||[]).map(s=>`<span class="step-chip ${esc(s.status||'pending')}">${symbols[s.status]||'○'} ${esc(s.name)} · ${esc(s.status||'pending')}</span>`).join('');
  updateExecutionValidationButton(e);
  if(!preserveLog&&$('executionDetail')) $('executionDetail').textContent='Loading log…';
}
async function openExecution(id){
  stopLogPolling();
  setLogAutoScroll(true);
  const e=(await getJson('/api/executions/'+encodeURIComponent(id))).execution; adminState.selectedExecution=e; adminState.logOffset=0;
  renderExecutions();
  renderOpenExecution(e);
  const log=await loadExecutionLog(id,{reset:true});
  if(executionIsLive(e)&&!log.complete){ adminState.logFollowing=true; updateLogLiveBadge(); adminState.logPollTimer=setTimeout(pollOpenExecution,1500); }
  else stopLogPolling();
  if(isMobileViewport()) document.body.classList.add('mobile-log-open');
}
function isMobileViewport(){ return window.matchMedia('(max-width: 899px)').matches; }
function setMobileNav(open){ document.body.classList.toggle('mobile-nav-open', !!open); const btn=$('btnMobileMenu'); if(btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false'); const bd=$('mobileNavBackdrop'); if(bd) bd.hidden=!open; }
function closeMobileNav(){ setMobileNav(false); }
function closeLogDetail(){ document.body.classList.remove('mobile-log-open'); }
function handleResponsiveResize(){
  closeMobileNav(); closeLogDetail();
}

function setSidebarCompact(compact) {
  document.body.classList.toggle('sidebar-collapsed', !!compact);
  const button = $('btnSidebarCompact');
  if (!button) return;
  button.setAttribute('aria-pressed', compact ? 'true' : 'false');
  button.setAttribute('aria-label', compact ? 'Expand sidebar' : 'Collapse sidebar');
  button.title = compact ? 'Expand sidebar' : 'Collapse sidebar';
}

function toggleSidebarCompact() {
  const compact = !document.body.classList.contains('sidebar-collapsed');
  localStorage.setItem('debBuilderSidebarCompact', compact ? '1' : '0');
  setSidebarCompact(compact);
}

function restoreSidebarPreference() {
  setSidebarCompact(localStorage.getItem('debBuilderSidebarCompact') === '1');
}

async function copyInstallCommand() {
  const status = $('status');
  const command = status?.textContent?.trim();
  if (!status || !command || !command.startsWith('curl ')) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(command);
  } else {
    const input = document.createElement('textarea');
    input.value = command;
    input.style.position = 'fixed';
    input.style.opacity = '0';
    document.body.appendChild(input);
    input.select();
    document.execCommand('copy');
    input.remove();
  }
  status.classList.add('copied');
  setTimeout(() => {
    status.classList.remove('copied');
  }, 1200);
}

const sourceChangeDefinitions={replace:{operation:'replace',fields:[['path','Target file','input'],['search','Content to find','textarea'],['content','Replace with','textarea']]},before:{operation:'insert_before',fields:[['path','Target file','input'],['search','Matching text to find','textarea'],['content','Content to add before','textarea']]},after:{operation:'insert_after',fields:[['path','Target file','input'],['search','Matching text to find','textarea'],['content','Content to add after','textarea']]},remove:{operation:'remove',fields:[['path','Target file','input'],['search','Exact content to find and remove','textarea']]},create:{operation:'create_file',fields:[['path','New file path','input'],['content','File contents','textarea']]},'delete-file':{operation:'remove_file',fields:[['path','Path of the file to delete','input']]}};
let editingSourceChangeIndex=null;
function sourceChoiceForOperation(operation){return Object.keys(sourceChangeDefinitions).find(key=>sourceChangeDefinitions[key].operation===operation)||'replace';}
function selectSourceChangeType(type,change={}){const definition=sourceChangeDefinitions[type]||sourceChangeDefinitions.replace;document.querySelectorAll('[data-change-type]').forEach(button=>button.classList.toggle('active',button.dataset.changeType===type));$('sourceChangeDialog').dataset.changeType=type;$('sourceChangeFields').innerHTML=definition.fields.map(([key,label,kind])=>`<label><span>${label}</span>${kind==='textarea'?`<textarea data-change-field="${key}" rows="3">${esc(change[key]||'')}</textarea>`:`<input data-change-field="${key}" value="${esc(change[key]||'')}">`}</label>`).join('')+'<div class="change-preview single"><span class="recipe-group-title">Result preview</span><p>The validated change will be applied to the isolated source workspace before build commands.</p></div>';}
function openSourceChangeDialog(type='replace',index=null){editingSourceChangeIndex=index;const change=index===null?{}:window.recipeSourceChanges[index];selectSourceChangeType(change?.operation?sourceChoiceForOperation(change.operation):type,change||{});$('sourceChangeDialog').showModal();}
function confirmSourceChange(){const type=$('sourceChangeDialog').dataset.changeType||'replace';const definition=sourceChangeDefinitions[type];const change={operation:definition.operation};$('sourceChangeFields').querySelectorAll('[data-change-field]').forEach(field=>{change[field.dataset.changeField]=field.value;});if(editingSourceChangeIndex===null)window.recipeSourceChanges.push(change);else window.recipeSourceChanges[editingSourceChangeIndex]=change;renderSourceChanges();scheduleRecipeAutosave();$('sourceChangeDialog').close();}

function wireAdmin() {
  restoreSidebarPreference();
  document.querySelectorAll('.nav-link').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $('packageSearch')?.addEventListener('input', renderPackages);
  $('packageFilter')?.addEventListener('change', renderPackages);
  $('logSearch')?.addEventListener('input', renderExecutions);
  $('logStatus')?.addEventListener('change', renderExecutions);
  $('logVerbosity')?.addEventListener('change', event => changeLogVerbosity(event.target.value));
  $('btnDeleteExecutionLog')?.addEventListener('click', () => { if(adminState.selectedExecution) deleteExecutionLog(adminState.selectedExecution.id).catch(error=>alert(error.message)); });
  $('btnRevalidateExecution')?.addEventListener('click', () => { if(adminState.selectedExecution) validateExecution(adminState.selectedExecution.id).catch(error=>alert(error.message)); });
  $('btnLogLiveBadge')?.addEventListener('click', resumeLiveLog);
  $('executionDetail')?.addEventListener('scroll', handleLogScroll);
  $('executionMeta')?.addEventListener('click', event => {
    const button=event.target.closest('[data-copy-value]');
    if(!button) return;
    copyTextValue(button.dataset.copyValue || '').then(() => {
      button.classList.add('copied');
      setTimeout(() => button.classList.remove('copied'), 900);
    }).catch(() => {});
  });
  $('executionList')?.addEventListener('keydown', event => { const item=event.target.closest('[data-execution-id]'); if(item&&(event.key==='Enter'||event.key===' ')){ event.preventDefault(); openExecution(item.dataset.executionId).catch(error=>alert(error.message)); } });
  $('btnNewPackage')?.addEventListener('click', () => createPackageUi().catch(error => alert(error.message)));
  $('btnNewRecipe')?.addEventListener('click', newRecipeUi);
  $('recipeMetaName')?.addEventListener('input', event => {$('recipeTitle').textContent = event.target.value || 'Recipe';});
  $('newRecipePackage')?.addEventListener('change', event => syncNewRecipeFromPackage(event.target.value));
  $('newRecipeForm')?.addEventListener('submit', event => {
    event.preventDefault();
    if (event.currentTarget.reportValidity()) createRecipeFromDialog().catch(reportAutosaveError);
  });
  $('cancelNewRecipe')?.addEventListener('click', () => $('newRecipeDialog').close());
  $('btnClosePackageDrawer')?.addEventListener('click', closePackageDrawer);
  $('packageDrawerBackdrop')?.addEventListener('click', closePackageDrawer);
  $('btnSidebarCompact')?.addEventListener('click', toggleSidebarCompact);
  $('status')?.addEventListener('click', () => copyInstallCommand().catch(() => {}));
  $('status')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      copyInstallCommand().catch(() => {});
    }
  });
  $('btnMobileMenu')?.addEventListener('click', () => setMobileNav(!document.body.classList.contains('mobile-nav-open')));
  $('mobileNavBackdrop')?.addEventListener('click', closeMobileNav);
  $('btnCloseLogDetail')?.addEventListener('click', closeLogDetail);
  $('btnAddSourceChange')?.addEventListener('click',()=>openSourceChangeDialog());
  $('sourceChangeList')?.addEventListener('click',event=>{const edit=event.target.closest('[data-edit-change-index]');const remove=event.target.closest('[data-remove-change-index]');if(edit)openSourceChangeDialog('replace',Number(edit.dataset.editChangeIndex));if(remove){window.recipeSourceChanges.splice(Number(remove.dataset.removeChangeIndex),1);renderSourceChanges();scheduleRecipeAutosave();}});
  document.querySelectorAll('[data-change-type]').forEach(button=>button.addEventListener('click',()=>selectSourceChangeType(button.dataset.changeType)));
  ['btnCloseSourceChange','btnCancelSourceChange'].forEach(id=>$(id)?.addEventListener('click',()=>$('sourceChangeDialog').close()));
  $('btnConfirmSourceChange')?.addEventListener('click',confirmSourceChange);
  $('btnEditBuildCommands')?.addEventListener('click',()=>{$('buildCommandPreview').classList.toggle('hidden');$('buildCommandEditor').classList.toggle('hidden');$('btnEditBuildCommands').textContent=$('buildCommandEditor').classList.contains('hidden')?'Edit commands':'View preview';});
  $('buildOutputMode')?.addEventListener('change',event=>{setBuildOutputMode(event.target.value);const output=collectBuildOutput();if(output.mode==='source'||output.path||(output.paths||[]).length)scheduleRecipeAutosave();});
  $('buildOutputPath')?.addEventListener('input',event=>{window.recipeBuildOutput.path=event.target.value;renderInstallContentSummary();if(event.target.value.trim())scheduleRecipeAutosave();});
  $('btnAddBuildOutputPath')?.addEventListener('click',()=>addBuildOutputPath());
  $('buildOutputPathList')?.addEventListener('input',event=>{const field=event.target.closest('input[data-output-path-index]');if(!field)return;window.recipeBuildOutput.paths[Number(field.dataset.outputPathIndex)]=field.value;renderInstallContentSummary();if(field.value.trim())scheduleRecipeAutosave();});
  $('buildOutputPathList')?.addEventListener('click',event=>{const remove=event.target.closest('[data-remove-output-path]');if(remove){removeBuildOutputPath(Number(remove.dataset.removeOutputPath));if(window.recipeBuildOutput.paths.length)scheduleRecipeAutosave();}});
  $('buildOutputSuggestionList')?.addEventListener('click',event=>{const add=event.target.closest('[data-add-output-suggestion]');if(!add)return;setBuildOutputMode('paths');addBuildOutputPath(add.dataset.addOutputSuggestion);scheduleRecipeAutosave();});
  $('btnAddInstallMapping')?.addEventListener('click',addInstallMapping);
  $('installMappingList')?.addEventListener('input',event=>{const keys=['source','destination','policy','owner','group','mode'];const key=keys.find(item=>event.target.closest(`[data-install-mapping-${item}]`));if(!key)return;const field=event.target.closest(`[data-install-mapping-${key}]`);const index=Number(field.dataset[`installMapping${key[0].toUpperCase()}${key.slice(1)}`]);const mapping=window.recipeInstallMappings[index];mapping[key]=field.value;if(mapping.source.trim()&&mapping.destination.trim())scheduleRecipeAutosave();});
  $('installMappingList')?.addEventListener('click',event=>{const remove=event.target.closest('[data-remove-install-mapping]');if(!remove)return;removeInstallMapping(Number(remove.dataset.removeInstallMapping));scheduleRecipeAutosave();});
  $('btnAddBuildDependency')?.addEventListener('click',()=>{const dependency=prompt('Debian build dependency name');if(dependency&&!window.recipeExtraDependencies.includes(dependency.trim())){window.recipeExtraDependencies.push(dependency.trim());renderDependencyChips();scheduleRecipeAutosave();}});
  $('buildDependencyChips')?.addEventListener('click',event=>{const remove=event.target.closest('[data-remove-dependency]');if(remove){window.recipeExtraDependencies.splice(Number(remove.dataset.removeDependency),1);renderDependencyChips();scheduleRecipeAutosave();}});
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closeMobileNav(); closeLogDetail(); closePackageDrawer(); }
  });
  window.addEventListener('resize', handleResponsiveResize);
  window.addEventListener('orientationchange', () => setTimeout(handleResponsiveResize, 250));
  handleResponsiveResize();
  loadDashboard().catch(error => { if ($('latestOperations')) $('latestOperations').textContent = error.message; });
  loadPackages().catch(() => {});
  scheduleBackgroundPackageRefresh();
  loadExecutions().catch(() => {});
}

wireAdmin();
