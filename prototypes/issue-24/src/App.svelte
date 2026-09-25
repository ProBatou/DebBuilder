<script>
  import {onMount, tick} from 'svelte';
  import {views, scenarios} from './lib/fixtures.js';
  import {translate, localeNames} from './lib/i18n.js';
  import Modal from './lib/Modal.svelte';
  import BrandMark from './lib/BrandMark.svelte';
  import NavIcon from './lib/NavIcon.svelte';
  import Overview from './pages/Overview.svelte';
  import Packages from './pages/Packages.svelte';
  import Recipes from './pages/Recipes.svelte';
  import Runs from './pages/Runs.svelte';
  import System from './pages/System.svelte';
  import Settings from './pages/Settings.svelte';

  let view = 'overview';
  let scenario = 'normal';
  let locale = 'en';
  let theme = 'system';
  let systemDark = false;
  let collapsed = false;
  let menuOpen = false;
  let toolsOpen = true;
  let toolsEnabled = true;
  let modal = '';
  let focusPackage = '';
  let focusRun = '';
  let focusRecipe = '';
  let menuButton;
  $: t = (key, values = {}) => translate(locale, key, values);
  $: effectiveTheme = theme === 'system' ? (systemDark ? 'dark' : 'light') : theme;
  $: if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = effectiveTheme;
    document.documentElement.lang = locale;
  }
  onMount(() => {
    locale = ['en','fr','de','es'].includes(localStorage.getItem('debBuilder24Locale')) ? localStorage.getItem('debBuilder24Locale') : 'en';
    theme = ['system','light','dark'].includes(localStorage.getItem('debBuilder24Theme')) ? localStorage.getItem('debBuilder24Theme') : 'system';
    collapsed = localStorage.getItem('debBuilder24SidebarCollapsed') === '1';
    const media = matchMedia('(prefers-color-scheme: dark)');
    const update = () => systemDark = media.matches;
    update(); media.addEventListener('change', update);
    const requested = new URLSearchParams(location.search);
    if (scenarios.includes(requested.get('scenario'))) scenario = requested.get('scenario');
    if (views.some(([key]) => key === requested.get('view'))) view = requested.get('view');
    if (requested.get('clean') === '1') {toolsOpen = false;toolsEnabled = false;}
    return () => media.removeEventListener('change',update);
  });
  async function navigate(target, id = '') {
    view = target;
    focusPackage = target === 'packages' ? id : '';
    focusRun = target === 'runs' ? id : '';
    focusRecipe = target === 'recipes' ? id : '';
    menuOpen = false;
    window.scrollTo(0,0);
    await tick();
    document.querySelector('#main h1')?.focus();
  }
  function toggleSidebar() {collapsed = !collapsed; localStorage.setItem('debBuilder24SidebarCollapsed',collapsed?'1':'0');}
  function closeMobileMenu() {menuOpen = false; menuButton?.focus();}
  function changeTheme(value) {theme=value;localStorage.setItem('debBuilder24Theme',value);}
  function changeLocale(value) {locale=value;localStorage.setItem('debBuilder24Locale',value);}
  function openModal(name) {modal=name;}
</script>
<svelte:window on:keydown={(event) => {if(event.key==='Escape' && menuOpen) closeMobileMenu();}} />
<svelte:head><title>DebBuilder · {t('nav.'+view)}</title></svelte:head>
<div class="shell" class:sidebar-collapsed={collapsed}>
  <aside class:mobile-open={menuOpen} class="sidebar" aria-label={t('nav.open')}>
    <div class="sidebar-head"><div class="brand-mark"><BrandMark /></div><div class="brand-copy"><strong>DebBuilder</strong><small>{t('nav.version')}</small></div><button class="collapse-button" title={collapsed?t('nav.expand'):t('nav.collapse')} aria-label={collapsed?t('nav.expand'):t('nav.collapse')} aria-expanded={!collapsed} on:click={toggleSidebar}>{collapsed?'»':'«'}</button></div>
    <nav aria-label={t('nav.open')}>
      {#each views as [key]}<button class:active={view===key} title={t('nav.'+key)} aria-label={t('nav.'+key)} aria-current={view===key?'page':undefined} on:click={() => navigate(key)}><span class="nav-icon"><NavIcon name={key} /></span><span class="nav-text">{t('nav.'+key)}</span></button>{/each}
    </nav>
    <div class="sidebar-bottom"><div class="repo-indicator" title={t('nav.repositoryOnline')} aria-label={t('nav.repositoryOnline')}><span class="repo-dot" aria-hidden="true"></span><div class="repo-copy"><strong>{t('nav.repositoryOnline')}</strong><small>Luminous · amd64</small></div></div><span class="version" title={t('nav.version')}>{collapsed?'v1.0.0':t('nav.version')}</span></div>
  </aside>
  {#if menuOpen}<button class="nav-scrim" aria-label={t('nav.close')} on:click={closeMobileMenu}></button>{/if}
  <div class="workspace">
    <header class="mobile-top"><button bind:this={menuButton} class="icon-button" aria-label={t('nav.open')} aria-expanded={menuOpen} on:click={() => menuOpen=!menuOpen}>☰</button><strong>DebBuilder</strong><span class="mobile-version">v1.0.0</span></header>
    <main id="main" class="content">
      <div class="page-title"><div><h1 tabindex="-1">{t('nav.'+view)}</h1><p>{t(view+'.subtitle')}</p></div>{#if view==='overview' || view==='packages'}<button class="button primary" on:click={() => navigate('recipes')}>+ {t('packages.create')}</button>{/if}</div>
      {#if view==='overview'}<Overview {locale} {scenario} {navigate} />
      {:else if view==='packages'}<Packages {locale} {scenario} {navigate} {focusPackage} {openModal} />
      {:else if view==='recipes'}<Recipes {locale} {scenario} {focusRecipe} {openModal} />
      {:else if view==='runs'}<Runs {locale} {scenario} {navigate} {focusRun} {openModal} />
      {:else if view==='system'}<System {locale} {scenario} {openModal} />
      {:else}<Settings {locale} {theme} {changeTheme} {changeLocale} {openModal} />{/if}
    </main>
  </div>
</div>
{#if toolsEnabled}{#if toolsOpen}<aside class="prototype-tools" aria-label={t('prototype.controls')}><div class="tool-head"><strong>{t('prototype.controls')}</strong><button class="tool-close" aria-label={t('prototype.hide')} on:click={() => toolsOpen=false}>×</button></div><label>{t('prototype.fixture')}<select id="scenario" bind:value={scenario}>{#each scenarios as key}<option value={key}>{t('scenario.'+key)}</option>{/each}</select></label><small>{t('prototype.notice')}</small></aside>{:else}<button class="tool-reopen" aria-label={t('prototype.show')} title={t('prototype.show')} on:click={() => toolsOpen=true}>◇</button>{/if}{/if}
<Modal title={modal==='test'?t('recipes.testPlan'):modal==='build'?t('recipes.buildPackage'):modal==='cancel'?t('runs.cancel'):modal==='publish'?t('runs.publish'):modal==='notification'?t('settings.testNotification'):t('common.viewDetails')} open={!!modal} closeLabel={t('common.close')} onClose={() => modal=''}><p class="modal-copy">{modal==='test'?t('modal.test'):modal==='publish'?t('modal.publish'):modal==='cancel'?t('modal.cancel'):t('modal.fixture')}</p><div class="modal-actions"><button class="button primary" on:click={() => modal=''}>{t('modal.close')}</button></div></Modal>
