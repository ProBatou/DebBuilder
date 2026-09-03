let currentSettings = null;
let settingsDirty = false;
let settingsSaving = false;

function fieldInput(id, label, value, attrs=''){
  return `<label class="settings-field" for="${id}">
    <span class="settings-label-text">${esc(label)}</span>
    <input id="${id}" ${attrs} value="${esc(value ?? '')}">
  </label>`;
}

function fieldSelect(id, label, value, options){
  return `<label class="settings-field" for="${id}">
    <span class="settings-label-text">${esc(label)}</span>
    <select id="${id}">${options.map(([val,label])=>`<option value="${esc(val)}" ${val===value?'selected':''}>${esc(label)}</option>`).join('')}</select>
  </label>`;
}

function secretInput(id, label, configured, noun='secret'){
  const placeholder = configured ? 'Enter a value to replace' : `Enter ${noun}`;
  return `<label class="settings-field secret-field" for="${id}">
    <span class="settings-label-text">${esc(label)}</span>
    <input id="${id}" type="password" autocomplete="new-password" value="" placeholder="${esc(placeholder)}">
    <small>Leave empty to keep the current value.</small>
  </label>`;
}

function markSettingsDirty(){
  settingsDirty = true;
  const status = $('settingsSaveStatus');
  if (status) { status.textContent = 'Unsaved changes'; status.dataset.state = 'pending'; }
}

async function loadSettings(){
  currentSettings = (await getJson('/api/settings')).settings;
  settingsDirty = false;
  renderSettingsPage();
}

function settingsPayload(){
  const ntfyServer = $('settingNtfyServer')?.value.trim() || '';
  const ntfyTopic = $('settingNtfyTopic')?.value.trim() || '';
  const notificationType = ntfyServer && ntfyTopic ? 'ntfy' : 'none';
  const oidcIssuer = $('settingOidcIssuer')?.value.trim() || '';
  const oidcClientId = $('settingOidcClientId')?.value.trim() || '';
  const oidcRedirectUri = $('settingOidcRedirectUri')?.value.trim() || '';
  const oidcSecret = $('settingOidcSecret')?.value.trim() || '';
  const oidcConfigured = oidcIssuer && oidcClientId && oidcRedirectUri && (oidcSecret || currentSettings?.security?.oidc_client_secret_configured);
  const body = {
    general: {app_name: $('settingAppName').value.trim(), url: $('settingPublicUrl').value.trim()},
    apt: {
      repository: $('settingRepoUrl').value.trim(),
      distribution: $('settingSuite').value.trim(),
      component: $('settingComponent').value.trim(),
      architecture: $('settingArch').value
    },
    github: {api_url: $('settingGithubApiUrl').value.trim()},
    notifications: {
      type: notificationType,
      server_url: ntfyServer,
      topic: ntfyTopic
    },
    security: {
      auth_mode: oidcConfigured ? 'oidc' : 'none',
      oidc_issuer: oidcIssuer,
      oidc_client_id: oidcClientId,
      oidc_redirect_uri: oidcRedirectUri
    }
  };
  const token = $('settingGithubToken').value.trim();
  if (token) body.github.token = token;
  const ntfyToken = $('settingNtfyToken')?.value.trim();
  if (ntfyToken) body.notifications.token = ntfyToken;
  if (oidcSecret) body.security.oidc_client_secret = oidcSecret;
  return body;
}

function renderSettingsPage(){
  const s = currentSettings || {};
  const general = s.general || {}, apt = s.apt || {}, github = s.github || {};
  const notifications = s.notifications || {}, security = s.security || {};
  const notificationType = notifications.type === 'ntfy' ? 'ntfy' : 'none';
  const tokenConfigured = !!github.token_configured;
  const ntfyTokenConfigured = !!notifications.token_configured;

  $('settingsPanel').innerHTML = `<form id="settingsForm" class="settings-admin-form">
    <div class="settings-editable-area">
      <section class="settings-section settings-card editable-settings-card general-settings-card">
        <header class="settings-section-head"><div><h3>General</h3><p class="muted">Console identity and public URL.</p></div></header>
        <div class="settings-form-grid settings-grid-two">
          ${fieldInput('settingAppName','Application name',general.app_name,'required maxlength="80"')}
          ${fieldInput('settingPublicUrl','Public URL',general.url,'type="url"')}
        </div>
      </section>

      <section class="settings-section settings-card editable-settings-card apt-settings-card">
        <header class="settings-section-head"><div><h3>APT repository</h3><p class="muted">Default configuration used by recipes without explicit repository settings.</p></div></header>
        <div class="settings-form-grid settings-grid-one">${fieldInput('settingRepoUrl','Repository URL',apt.repository,'type="url" required')}</div>
        <div class="settings-form-grid settings-grid-three">
          ${fieldInput('settingSuite','Distribution / suite',apt.distribution,'required pattern="[A-Za-z0-9._-]+"')}
          ${fieldInput('settingComponent','Component',apt.component,'required pattern="[A-Za-z0-9._-]+"')}
          ${fieldSelect('settingArch','Architecture',apt.architecture || 'amd64',[['amd64','amd64'],['arm64','arm64'],['armhf','armhf'],['i386','i386'],['all','all']])}
        </div>
      </section>

      <section class="settings-section settings-card editable-settings-card github-settings-card">
        <header class="settings-section-head"><div><h3>GitHub integration</h3><p class="muted">Server API and token used to read releases.</p></div>${statusBadge(tokenConfigured ? 'CONFIGURED' : 'NOT CONFIGURED', tokenConfigured ? 'active' : 'warning')}</header>
        <div class="settings-form-grid settings-grid-one">
          ${fieldInput('settingGithubApiUrl','GitHub API URL',github.api_url,'type="url" required')}
          ${secretInput('settingGithubToken','GitHub token',tokenConfigured,'token')}
        </div>
      </section>

      <section class="settings-section settings-card editable-settings-card notifications-settings-card">
        <header class="settings-section-head"><div><h3>Notifications</h3><p class="muted">Updates, builds and publications via ntfy.</p></div><div class="settings-head-actions">${statusBadge(notificationType === 'none' ? 'OFF' : 'ACTIVE', notificationType === 'none' ? 'neutral' : 'active')}<button id="testNtfyButton" class="settings-link-action" type="button" title="Send a test notification">Test</button></div></header>
        <div id="ntfySettings" class="ntfy-settings">
          <div class="settings-form-grid settings-grid-two ntfy-endpoint-row">
            ${fieldInput('settingNtfyServer','ntfy server',notificationType === 'ntfy' ? notifications.server_url : '','type="url" placeholder="https://ntfy.sh"')}
            ${fieldInput('settingNtfyTopic','Topic',notificationType === 'ntfy' ? notifications.topic : '','pattern="[A-Za-z0-9._-]+" placeholder="debbuilder"')}
          </div>
          ${secretInput('settingNtfyToken','ntfy token (optional)',ntfyTokenConfigured,'token')}
          <small id="ntfyTestStatus" class="settings-inline-status" aria-live="polite"></small>
        </div>
      </section>

      <section class="settings-section settings-card editable-settings-card auth-settings-card">
      <header class="settings-section-head"><div><h3>OIDC authentication</h3><p class="muted">Automatically active when configuration is complete.</p></div>${statusBadge(security.auth_mode === 'oidc' ? 'ACTIVE' : 'INACTIVE',security.auth_mode === 'oidc' ? 'active' : 'neutral')}</header>
      <div id="oidcSettings" class="oidc-settings">
        <div class="settings-form-grid settings-grid-two oidc-fields-grid">
          ${fieldInput('settingOidcIssuer','Issuer URL',security.oidc_issuer || '','type="url"')}
          ${fieldInput('settingOidcClientId','Client ID',security.oidc_client_id || '')}
          ${fieldInput('settingOidcRedirectUri','Redirect URI',security.oidc_redirect_uri || (general.url ? `${general.url.replace(/\/$/,'')}/auth/callback` : ''),'type="url"')}
          ${secretInput('settingOidcSecret','Client Secret',security.oidc_client_secret_configured,'secret')}
        </div>
      </div>
      </section>
    </div>
    <div class="settings-save-bar"><span id="settingsSaveStatus" data-state="saved" aria-live="polite">All changes saved</span><button id="btnSaveSettings" class="btn-primary" type="submit">Save settings</button></div>
  </form>`;

  $('settingsForm')?.addEventListener('input', markSettingsDirty);
  $('settingsForm')?.addEventListener('change', markSettingsDirty);
  $('settingsForm')?.addEventListener('submit', ev=>{ ev.preventDefault(); saveAllSettings().catch(()=>{}); });
  $('testNtfyButton')?.addEventListener('click', ()=>testNtfyNotification().catch(()=>{}));
}

async function testNtfyNotification(){
  const status = $('ntfyTestStatus');
  if (settingsDirty || settingsSaving) {
    if (status) status.textContent = 'Saving settings…';
    await saveAllSettings();
  }
  if (status) status.textContent = 'Sending…';
  try {
    await postJson('/api/notifications/test', {});
    if (status) status.textContent = 'Notification sent.';
  } catch (err) {
    if (status) status.textContent = `Test failed: ${err.message || err}`;
  }
}

async function saveAllSettings(){
  if (!settingsDirty || settingsSaving) return;
  const form = $('settingsForm');
  if (form && !form.reportValidity()) {
    console.warn('Settings autosave skipped: invalid field');
    return;
  }
  settingsSaving = true;
  const status = $('settingsSaveStatus');
  const button = $('btnSaveSettings');
  if (status) { status.textContent = 'Saving…'; status.dataset.state = 'saving'; }
  if (button) button.disabled = true;
  try {
    const r = await postJson('/api/settings', settingsPayload());
    currentSettings = r.settings;
    settingsDirty = false;
    const githubToken = $('settingGithubToken');
    const ntfyToken = $('settingNtfyToken');
    const oidcSecret = $('settingOidcSecret');
    if (githubToken) githubToken.value = '';
    if (ntfyToken) ntfyToken.value = '';
    if (oidcSecret) oidcSecret.value = '';
    if (currentSettings?.apt) {
      const repo = currentSettings.apt.repository || '';
      document.getElementById('status').textContent = repo ? `curl -fsSL ${repo.replace(/\/$/, '')}/install.sh | sudo bash` : 'APT repository not configured';
    }
    if (status) { status.textContent = 'All changes saved'; status.dataset.state = 'saved'; }
  } catch (err) {
    settingsDirty = true;
    if (status) { status.textContent = `Save failed: ${err.message || err}`; status.dataset.state = 'error'; }
    console.error('Settings save failed:', err);
    throw err;
  } finally {
    settingsSaving = false;
    if (button) button.disabled = false;
  }
}
