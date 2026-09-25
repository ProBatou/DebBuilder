<script>
  import {packagesFor,repositoryInventory} from '../lib/fixtures.js';
  import {translate,formatCount,formatWhen} from '../lib/i18n.js';
  import StatusChip from '../lib/StatusChip.svelte';
  export let locale,scenario,navigate,focusPackage='',openModal;
  $: t=(key,values={})=>translate(locale,key,values);
  let inventoryOpen=false;
  let copiedIndex=-1;
  const installSteps=[
    ['repository.addKey','curl -fsSL https://packages.example.org/debian/repository.gpg | sudo tee /usr/share/keyrings/debbuilder.gpg >/dev/null'],
    ['repository.addRepository','deb [signed-by=/usr/share/keyrings/debbuilder.gpg arch=amd64] https://packages.example.org/debian Luminous main'],
    ['repository.update','sudo apt update'],
  ];
  async function copyCommand(command,index){try{await navigator.clipboard.writeText(command);copiedIndex=index;}catch{copiedIndex=-2;}}
  let search='';
  let filter='all';
  let selected='zoraxy';
  let mobileDetail=false;
  $: rows=packagesFor(scenario);
  $: filtered=rows.filter(row=>row.id.toLowerCase().includes(search.trim().toLowerCase())&&(filter==='all'||row.status===filter));
  $: current=rows.find(row=>row.id===selected)||rows[0];
  $: if(focusPackage==='repository')inventoryOpen=true;
  $: if(focusPackage==='')inventoryOpen=false;
  $: if(focusPackage && focusPackage!=='repository' && rows.some(row=>row.id===focusPackage)){selected=focusPackage;mobileDetail=true;inventoryOpen=false;}
  function sourceLabel(source){return source==='System-managed self-build'?t('recipes.managed'):source.includes('archive')?t('recipes.sourceArchive'):source.includes('repository')?t('recipes.repositorySource'):t('recipes.releaseAsset');}
  function select(row){selected=row.id;mobileDetail=true;}
</script>
{#if inventoryOpen}
  <div class="inventory-view"><button class="back-button" on:click={() => inventoryOpen=false}>← {t('packages.backFromInventory')}</button>
  <section class="panel repository-hero"><p class="detail-breadcrumb">{t('nav.packages')} / <strong>{t('packages.repositoryInventory')}</strong></p><div class="section-head"><h2>{t('repository.title')}</h2><StatusChip label={t('common.online')} tone="success" icon="●" /></div><p class="repo-line">Luminous · main · amd64</p><p class="muted">{t('repository.signed')}</p><p class="muted repository-note">{t('packages.inventoryHelp')}</p><p class="muted repository-note">{t('repository.last')}: {formatWhen(locale,'2026-09-25T09:42:00Z')}</p></section>
  <section class="panel"><h2>{t('repository.install')}</h2><div class="install-steps">{#each installSteps as [key,command],index}<div class="install-step"><h3><span>{index+1}.</span> {t(key)}</h3><div class="copy-row"><code>{command}</code><button class="button secondary" aria-label={t('repository.copy')+' '+t(key)} on:click={() => copyCommand(command,index)}>{copiedIndex===index?t('repository.copied'):t('repository.copy')}</button></div></div>{/each}</div>{#if copiedIndex===-2}<p class="field-help" role="status">{t('repository.copyFailed')}</p>{/if}</section>
  <section class="panel"><div class="section-head"><h2>{t('repository.publishedList')}</h2><span class="count-label">{formatCount(locale,repositoryInventory.length)}</span></div><div class="repo-packages">{#each repositoryInventory as row}<div><strong>{row.id}</strong><code>{row.version} · {row.architecture}</code></div>{/each}</div></section>
  <section class="panel repository-resources"><h2>{t('repository.resources')}</h2><div><button class="text-button" on:click={() => openModal('landing')}>{t('repository.publicKey')} →</button><button class="text-button" on:click={() => openModal('landing')}>{t('repository.inRelease')} →</button><button class="text-button" on:click={() => openModal('landing')}>{t('repository.metadata')} →</button></div></section></div>
{:else}
  <div class="package-layout" class:mobile-detail={mobileDetail}><section class="panel package-list"><div class="section-head"><h2>{t('packages.list')}</h2><span class="count-label">{formatCount(locale,filtered.length)} / {formatCount(locale,rows.length)}</span></div><div class="filters"><label><span>{t('packages.search')}</span><input type="search" bind:value={search} placeholder={t('common.search')} /></label><label><span>{t('packages.filter')}</span><select bind:value={filter}><option value="all">{t('common.all')}</option><option value="validationNeeded">{t('status.validationNeeded')}</option><option value="readyPublish">{t('status.readyPublish')}</option><option value="buildFailed">{t('status.buildFailed')}</option><option value="upToDate">{t('status.upToDate')}</option></select></label></div>
  {#if rows.length===0}<div class="empty-state"><strong>{t('packages.empty')}</strong><button class="button primary" on:click={() => navigate('recipes')}>{t('packages.create')}</button></div>{:else if filtered.length===0}<div class="empty-state"><strong>{t('packages.noResults')}</strong></div>{:else}<div class="list-head package-columns"><span>{t('packages.name')}</span><span>{t('common.status')}</span><span>{t('packages.built')}</span><span>{t('packages.published')}</span></div><div class="package-rows">{#each filtered as row}<button class="package-row package-columns" class:selected={selected===row.id} aria-current={selected===row.id?'true':undefined} on:click={() => select(row)}><span class="row-identity"><strong>{row.id}</strong><small>{sourceLabel(row.source)}</small></span><span><StatusChip label={t('status.'+row.status)} tone={row.tone} icon={row.tone==='danger'?'!':'✓'} /></span><span class="version-cell" data-label={t('packages.built')}>{row.built}</span><span class="version-cell" data-label={t('packages.published')}>{row.published}</span></button>{/each}</div>{/if}</section>
  {#if current}<aside class="panel package-detail"><button class="back-button mobile-only" on:click={() => mobileDetail=false}>← {t('packages.back')}</button><p class="detail-breadcrumb">{t('nav.packages')} / <strong>{current.id}</strong></p><h2>{current.id}</h2><p class="muted">{sourceLabel(current.source)}</p><div class="detail-facts"><div><span>{t('packages.source')}</span><strong>{current.id==='zoraxy'?'v3.1.4':current.built} · GitHub</strong></div><div><span>{t('packages.built')}</span><strong>{current.built}</strong></div><div><span>{t('packages.published')}</span><strong>{current.published}</strong></div><div><span>{t('common.status')}</span><StatusChip label={t('status.'+current.status)} tone={current.tone} icon={current.tone==='danger'?'!':'✓'} /></div></div><div class="detail-buttons"><button class="button secondary" on:click={() => navigate('runs','ui-24-validated')}>{t('packages.viewRun')}</button>{#if current.status==='readyPublish'}<button class="button primary" on:click={() => openModal('publish')}>{t('runs.publish')}</button>{:else if current.status==='validationNeeded'}<button class="button primary" on:click={() => openModal('validate')}>{t('runs.validate')}</button>{:else if current.status==='buildFailed'}<button class="button secondary" on:click={() => navigate('runs','ui-24-failed')}>{t('runs.reviewFailure')}</button>{/if}</div></aside>{/if}</div>
  <section class="panel repository-summary"><div><div class="section-head"><h2>{t('overview.repository')}</h2><StatusChip label={t('common.online')} tone="success" icon="●" /></div><p class="repo-line">Luminous · main · amd64</p><p class="muted">{t('packages.publishedCount',{count:formatCount(locale,repositoryInventory.length)})}</p></div><button class="button secondary" on:click={() => {inventoryOpen=true;mobileDetail=false;}}>{t('packages.viewInventory')} →</button></section>
{/if}
