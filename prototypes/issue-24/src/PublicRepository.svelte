<script>
  import {onMount} from 'svelte';
  import {translate,localeNames,formatCount} from './lib/i18n.js';
  import {publicRepositoryFixture as repo} from './lib/publicRepositoryFixture.js';
  let locale='en';
  let copied='';
  let copyStatus='';
  let clean=false;
  const supported=['en','fr','de','es'];
  $: t=(key,values={})=>translate(locale,key,values);
  $: if(typeof document!=='undefined')document.documentElement.lang=locale;
  $: installerCommand=`curl -fsSL ${repo.baseUrl}/install.sh | sudo bash`;
  $: keyUrl=`${repo.baseUrl}/repository.gpg`;
  $: sourceText=`Types: deb\nURIs: ${repo.baseUrl}\nSuites: ${repo.suite}\nComponents: ${repo.component}\nSigned-By: /etc/apt/keyrings/debbuilder.gpg`;
  const updateCommand='sudo apt-get update';
  const exampleCommand='sudo apt-get install zoraxy';
  onMount(()=>{
    const stored=localStorage.getItem('debBuilderPublicLocale');
    const browser=(navigator.languages||[navigator.language]).map(value=>value?.split('-')[0]).find(value=>supported.includes(value));
    locale=supported.includes(stored)?stored:browser||'en';
    clean=new URLSearchParams(location.search).get('clean')==='1';
  });
  function chooseLocale(value){locale=value;localStorage.setItem('debBuilderPublicLocale',value);}
  async function copy(value,key){
    try{await navigator.clipboard.writeText(value);copied=key;copyStatus=t('public.copiedItem',{item:t(key)});}
    catch{copyStatus=t('public.copyFailed');}
  }
</script>
<svelte:head><title>{t('public.title')} · DebBuilder</title><meta name="description" content={t('public.subtitle')} /></svelte:head>
<div class="public-page"><header class="public-header"><div class="public-brand"><span class="public-mark" aria-hidden="true">D<span>▪</span></span><span>DebBuilder</span></div><label class="language-control"><span>{t('public.language')}</span><select aria-label={t('public.language')} value={locale} on:change={(event)=>chooseLocale(event.currentTarget.value)}>{#each Object.entries(localeNames) as [code,name]}<option value={code}>{name}</option>{/each}</select></label></header>
<main class="public-content"><section class="public-intro"><div class="intro-heading"><div><p class="overline">DebBuilder</p><h1>{t('public.title')}</h1><p class="subtitle">{t('public.subtitle')}</p></div><span class="status-online"><span aria-hidden="true">●</span>{t('common.online')}</span></div><div class="distribution"><strong>{repo.suite}</strong><span>·</span><strong>{repo.component}</strong><span>·</span><strong>{repo.architectures.join(', ')}</strong></div></section>
<section class="public-panel install-panel" aria-labelledby="install-heading"><div class="section-heading"><div><p class="overline">01 / {t('public.getStarted')}</p><h2 id="install-heading">{t('public.install')}</h2></div></div><p class="section-intro">{t('public.installerHelp')}</p><div class="command-line featured"><code>{installerCommand}</code><button class="copy-button" aria-label={t('public.copyLabel',{item:t('public.installer')})} on:click={()=>copy(installerCommand,'public.installer')}>{copied==='public.installer'?t('public.copied'):t('public.copy')}</button></div><ol class="install-steps"><li><div><h3>{t('public.addKey')}</h3><p>{t('public.addKeyHelp')}</p></div><div class="command-line"><code>{keyUrl}</code><button class="copy-button" aria-label={t('public.copyLabel',{item:t('public.addKey')})} on:click={()=>copy(keyUrl,'public.addKey')}>{copied==='public.addKey'?t('public.copied'):t('public.copy')}</button></div></li><li><div><h3>{t('public.addRepository')}</h3><p>{t('public.sourceFile')}: <code>/etc/apt/sources.list.d/debbuilder.sources</code></p></div><div class="command-line"><code class="multiline">{sourceText}</code><button class="copy-button" aria-label={t('public.copyLabel',{item:t('public.addRepository')})} on:click={()=>copy(sourceText,'public.addRepository')}>{copied==='public.addRepository'?t('public.copied'):t('public.copy')}</button></div></li><li><div><h3>{t('public.update')}</h3></div><div class="command-line"><code>{updateCommand}</code><button class="copy-button" aria-label={t('public.copyLabel',{item:t('public.update')})} on:click={()=>copy(updateCommand,'public.update')}>{copied==='public.update'?t('public.copied'):t('public.copy')}</button></div></li></ol><div class="install-example"><span>{t('public.thenInstall')}</span><code>{exampleCommand}</code></div><span class="sr-only" role="status">{copyStatus}</span></section>
<section class="public-panel" aria-labelledby="packages-heading"><div class="section-heading"><div><p class="overline">02 / {t('public.available')}</p><h2 id="packages-heading">{t('public.publishedPackages')}</h2></div><span class="package-count">{formatCount(locale,repo.packages.length)}</span></div><div class="package-table"><div class="table-head"><span>{t('public.package')}</span><span>{t('public.version')}</span></div>{#each repo.packages as pkg}<div class="table-row"><strong>{pkg.name}</strong><code>{pkg.version}</code></div>{/each}</div></section>
<section class="public-panel" aria-labelledby="information-heading"><div class="section-heading"><div><p class="overline">03 / {t('public.details')}</p><h2 id="information-heading">{t('public.repositoryInformation')}</h2></div></div><div class="information-grid"><div><span>{t('repository.suite')}</span><strong>{repo.suite}</strong></div><div><span>{t('repository.component')}</span><strong>{repo.component}</strong></div><div><span>{t('repository.architectures')}</span><strong>{repo.architectures.join(', ')}</strong></div><div><span>{t('public.fingerprint')}</span><strong>{repo.fingerprint||t('public.fingerprintPending')}</strong></div></div><div class="public-links"><a href={`${repo.baseUrl}/repository.gpg`}>{t('repository.publicKey')} ↗</a><a href={`${repo.baseUrl}/dists/${repo.suite}/InRelease`}>{t('repository.inRelease')} ↗</a><a href={`${repo.baseUrl}/dists/${repo.suite}/Release`}>{t('repository.metadata')} ↗</a><a href={`${repo.baseUrl}/install.sh`}>{t('public.installer')} ↗</a></div></section></main><footer class="public-footer"><span>DebBuilder Repository</span>{#if !clean}<small>{t('public.fixtureNotice')}</small>{/if}</footer></div>
