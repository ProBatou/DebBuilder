<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {location, navigate} from '../navigation/location.js';
  import {locale, initLocale, setLocale, t} from '../i18n/i18n.js';
  import {theme, initTheme, setTheme} from '../theme/theme.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import BrandMark from '../components/BrandMark.svelte';
  import NavIcon from '../components/NavIcon.svelte';
  import Overview from '../pages/Overview.svelte';
  import Packages from '../pages/Packages.svelte';
  import Runs from '../pages/Runs.svelte';
  import Recipes from '../pages/Recipes.svelte';
  import System from '../pages/System.svelte';
  import Settings from '../pages/Settings.svelte';

  let session = null, status = null, error = null, loading = true, menu = false, collapsed = false;
  const loginHref = import.meta.env.VITE_DEBBUILDER_AUTH_ORIGIN || '/';
  const appVersion = `v${__DEBBUILDER_VERSION__}`;
  let controller, menuButton;
  async function bootstrap() {
    controller?.abort(); controller = new AbortController(); loading = true; error = null;
    try {
      [session, status] = await Promise.all([api.auth({signal:controller.signal}), api.status({signal:controller.signal})]);
    } catch (caught) {if (caught.name !== 'AbortError') error = caught;}
    finally {loading = false;}
  }
  onMount(() => {initLocale(); initTheme(); collapsed = localStorage.getItem('debBuilder24SidebarCollapsed') === '1'; bootstrap(); return () => controller?.abort();});
  const pages = ['overview', 'packages', 'recipes', 'runs', 'system', 'settings'];
  async function open(page) {await navigate(page); menu = false; window.scrollTo(0,0); await tick(); document.querySelector('#main h1')?.focus();}
  function closeMobileMenu() {menu = false; menuButton?.focus();}
  function toggleSidebar() {collapsed = !collapsed; localStorage.setItem('debBuilder24SidebarCollapsed', collapsed ? '1' : '0');}
</script>

<svelte:window onkeydown={(event) => {if (event.key === 'Escape' && menu) closeMobileMenu();}} />
<div class="shell" class:sidebar-collapsed={collapsed}>
  <aside class:mobile-open={menu} class="sidebar" aria-label="Navigation">
    <div class="sidebar-head"><div class="brand-mark"><BrandMark/></div><div class="brand-copy"><strong>DebBuilder</strong><small>DebBuilder {appVersion}</small></div><button class="collapse-button" aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} aria-expanded={!collapsed} onclick={toggleSidebar}>{collapsed ? '»' : '«'}</button></div>
    <nav aria-label="Main">
      {#each pages as page}<button class:active={$location.page === page} aria-label={t(page,$locale)} title={t(page,$locale)} aria-current={$location.page === page ? 'page' : undefined} onclick={() => open(page)}><span class="nav-icon"><NavIcon name={page}/></span><span class="nav-text">{t(page,$locale)}</span></button>{/each}
    </nav>
    <div class="sidebar-bottom"><div class="repo-indicator"><span class="repo-dot" aria-hidden="true"></span><div class="repo-copy"><strong>{t('repository',$locale)}</strong><small>{status ? `${status.suite_default || '—'} · ${status.component_default || '—'} · ${status.arch_default || '—'}` : '—'}</small></div></div><span class="version">DebBuilder {appVersion}</span></div>
  </aside>
  {#if menu}<button class="nav-scrim" aria-label={t('back',$locale)} onclick={closeMobileMenu}></button>{/if}
  <div class="workspace">
    <header class="mobile-top"><button bind:this={menuButton} class="icon-button" onclick={() => menu = !menu} aria-label={t('menu',$locale)} aria-expanded={menu}>☰</button><strong>DebBuilder</strong><span class="mobile-version">{appVersion}</span></header>
    <main id="main" class="content">
      <div class="page-title"><div><h1 tabindex="-1">{t($location.page,$locale)}</h1><p>{t(`${$location.page}Subtitle`,$locale)}</p></div>{#if $location.page === 'overview' || $location.page === 'packages'}<button class="button primary" onclick={() => open('recipes')}>+ {t('newRecipe',$locale)}</button>{/if}</div>
      {#if loading}<p>{t('loading',$locale)}</p>
      {:else if error}
        <ErrorNotice {error} retry={bootstrap} language={$locale}/>
        {#if error.status === 401}<a href={loginHref}>{t('signIn',$locale)}</a>{/if}
      {:else if $location.page === 'overview'}<Overview language={$locale}/>
      {:else if $location.page === 'packages'}<Packages id={$location.id} language={$locale}/>
      {:else if $location.page === 'runs'}<Runs id={$location.id} language={$locale}/>
      {:else if $location.page === 'recipes'}<Recipes id={$location.id} language={$locale}/>
      {:else if $location.page === 'settings'}<Settings language={$locale}/>
      {:else}<System id={$location.id} language={$locale}/>{/if}
    </main>
  </div>
</div>
