function $(id) {
  return document.getElementById(id);
}

const STATUS_LABELS = {
  ready: 'Up to date',
  up_to_date: 'Up to date',
  update_available: 'Update available',
  publication_available: 'Ready to publish',
  build_available: 'Build needed',
  build_required: 'Build needed',
  not_published: 'Build needed',
  recipe_missing: 'Recipe needed',
  success: 'Success',
  build_success: 'Validation needed',
  validation_needed: 'Validation needed',
  build_failed: 'Build failed',
  validating: 'Validating',
  validation_failed: 'Validation failed',
  publishing: 'Publishing',
  publication_failed: 'Publication failed',
  ready_to_publish: 'Ready to publish',
  published: 'Published',
  running: 'Running',
  failed: 'Error',
  cancelled: 'Cancelled',
  dry_run: 'Dry-run',
  prepared: 'Prepared',
  unknown: 'Unknown',
};
window.STATUS_LABELS = STATUS_LABELS;

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[character]));
}

function statusBadge(label, state = 'neutral') {
  return `<span class="settings-badge ${state}">${esc(label)}</span>`;
}

async function getJson(url) {
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function fmtTime(timestamp) {
  if (!timestamp) return '—';
  try {
    return new Date(timestamp * 1000).toLocaleString('en-US');
  } catch (error) {
    return '—';
  }
}

const APP_DIALOG_TYPES = new Set(['confirm', 'prompt']);
const TOAST_TYPES = new Set(['success', 'info', 'warning', 'error']);
let appDialogRequest = null;
let appDialogPreviousFocus = null;

function showToast(message, {type = 'info', duration = 5000} = {}) {
  const region = $('toastRegion');
  if (!region) return null;
  const safeType = TOAST_TYPES.has(type) ? type : 'info';
  const toast = document.createElement('div');
  toast.className = `toast toast--${safeType}`;
  toast.setAttribute('role', safeType === 'error' ? 'alert' : 'status');
  toast.innerHTML = `<span class="toast-message"></span><button type="button" class="toast-dismiss" aria-label="Dismiss notification">×</button>`;
  toast.querySelector('.toast-message').textContent = String(message || '');
  const dismiss = () => {
    toast.classList.add('toast--leaving');
    setTimeout(() => toast.remove(), 160);
  };
  toast.querySelector('.toast-dismiss').addEventListener('click', dismiss);
  region.appendChild(toast);
  if (duration > 0) setTimeout(dismiss, duration);
  return toast;
}

function settleAppDialog(value) {
  const dialog = $('appDialog');
  const request = appDialogRequest;
  appDialogRequest = null;
  if (dialog?.open) dialog.close();
  if (appDialogPreviousFocus?.isConnected) appDialogPreviousFocus.focus();
  appDialogPreviousFocus = null;
  request?.resolve(value);
}

function openAppDialog({
  type = 'confirm',
  title,
  description = '',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  inputLabel = '',
  inputValue = '',
  inputPlaceholder = '',
  inputRequired = false,
} = {}) {
  const dialog = $('appDialog');
  if (!dialog || !APP_DIALOG_TYPES.has(type)) return Promise.resolve(type === 'prompt' ? null : false);
  if (appDialogRequest) settleAppDialog(appDialogRequest.type === 'prompt' ? null : false);
  appDialogPreviousFocus = document.activeElement;
  $('appDialogTitle').textContent = title || (danger ? 'Confirm destructive action' : 'Confirm action');
  const descriptionNode = $('appDialogDescription');
  descriptionNode.textContent = description;
  descriptionNode.hidden = !description;
  const inputField = $('appDialogInputField');
  const input = $('appDialogInput');
  inputField.hidden = type !== 'prompt';
  $('appDialogInputLabel').textContent = inputLabel || 'Value';
  input.value = String(inputValue ?? '');
  input.placeholder = inputPlaceholder;
  input.required = !!inputRequired;
  const confirmButton = $('appDialogConfirm');
  confirmButton.textContent = confirmLabel;
  confirmButton.classList.toggle('btn--danger', !!danger);
  confirmButton.classList.toggle('btn--primary', !danger);
  $('appDialogCancel').textContent = cancelLabel;
  dialog.dataset.dialogType = type;
  return new Promise(resolve => {
    appDialogRequest = {resolve, type};
    dialog.showModal();
    (type === 'prompt' ? input : confirmButton).focus();
    if (type === 'prompt') input.select();
  });
}

function showConfirm(options) {
  return openAppDialog({...options, type: 'confirm'}).then(Boolean);
}

function showPrompt(options) {
  return openAppDialog({...options, type: 'prompt'});
}

function wireAppFeedback() {
  const dialog = $('appDialog');
  if (!dialog) return;
  $('appDialogCancel')?.addEventListener('click', () => settleAppDialog(dialog.dataset.dialogType === 'prompt' ? null : false));
  $('appDialogConfirm')?.addEventListener('click', () => {
    if (dialog.dataset.dialogType === 'prompt') {
      const input = $('appDialogInput');
      if (!input.reportValidity()) return;
      settleAppDialog(input.value);
      return;
    }
    settleAppDialog(true);
  });
  $('appDialogForm')?.addEventListener('submit', event => {
    event.preventDefault();
    $('appDialogConfirm')?.click();
  });
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    settleAppDialog(dialog.dataset.dialogType === 'prompt' ? null : false);
  });
  dialog.addEventListener('click', event => {
    if (event.target === dialog) settleAppDialog(dialog.dataset.dialogType === 'prompt' ? null : false);
  });
}

wireAppFeedback();
