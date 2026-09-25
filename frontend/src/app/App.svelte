<script>
  import {onMount} from 'svelte';
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
  import System from '../pages/System.svelte';

  let session = null, status = null, error = null, loading = true, menu = false;
  const loginHref = import.meta.env.VITE_DEBBUILDER_AUTH_ORIGIN || '/';
  let controller;
  async function bootstrap() {
    controller?.abort(); controller = new AbortController(); loading = true; error = null;
    try {
      [session, status] = await Promise.all([api.auth({signal:controller.signal}), api.status({signal:controller.signal})]);
    } catch (caught) {if (caught.name !== 'AbortError') error = caught;}
    finally {loading = false;}
  }
  onMount(() => {initLocale(); initTheme(); bootstrap(); return () => controller?.abort();});
  const pages = ['overview', 'packages', 'runs', 'system'];
  function open(page) {navigate(page); menu = false;}
</script>

<svelte:window onkeydown={(event) => {if (event.key === 'Escape') menu = false;}} />
<div class="shell">
  <aside class:open={menu} class="sidebar" aria-label="Navigation">
    <div class="brand"><span class="mark"><BrandMark/></span><strong>DebBuilder</strong><button class="mobile-close" aria-label={t('back',$locale)} onclick={() => menu = false}>×</button></div>
    <nav aria-label="Main">
      {#each pages as page}<button class:active={$location.page === page} aria-current={$location.page === page ? 'page' : undefined} onclick={() => open(page)}><NavIcon name={page}/>{t(page,$locale)}</button>{/each}
      <button disabled title={t('availableLater',$locale)}><NavIcon name="recipes"/>{t('recipes',$locale)}</button>
      <button disabled title={t('availableLater',$locale)}><NavIcon name="settings"/>{t('settings',$locale)}</button>
    </nav>
    <div class="sidebar-bottom"><small>{status?.repo_default || t('repository',$locale)}</small><small>{session?.user || session?.auth_mode || ''}</small></div>
  </aside>
  {#if menu}<button class="scrim" aria-label={t('back',$locale)} onclick={() => menu = false}></button>{/if}
  <main class="workspace">
    <div class="mobile-top"><button onclick={() => menu = true} aria-label={t('menu',$locale)}>☰</button><strong>DebBuilder</strong></div>
    <header class="toolbar"><span>{t($location.page,$locale)}</span><div class="preferences"><label>{t('theme',$locale)} <select value={$theme} onchange={(e) => setTheme(e.currentTarget.value)}><option value="system">{t('systemTheme',$locale)}</option><option value="light">{t('light',$locale)}</option><option value="dark">{t('dark',$locale)}</option></select></label><label>{t('language',$locale)} <select value={$locale} onchange={(e) => setLocale(e.currentTarget.value)}><option value="en">EN</option><option value="fr">FR</option><option value="de">DE</option><option value="es">ES</option></select></label></div></header>
    <div class="content">
      {#if loading}<p>{t('loading',$locale)}</p>
      {:else if error}
        <ErrorNotice {error} retry={bootstrap} language={$locale}/>
        {#if error.status === 401}<a href={loginHref}>{t('signIn',$locale)}</a>{/if}
      {:else if $location.page === 'overview'}<Overview language={$locale}/>
      {:else if $location.page === 'packages'}<Packages id={$location.id} language={$locale}/>
      {:else if $location.page === 'runs'}<Runs id={$location.id} language={$locale}/>
      {:else}<System language={$locale}/>{/if}
    </div>
  </main>
</div>
