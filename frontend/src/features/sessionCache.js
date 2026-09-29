// Read models live only for this browser document. Mutations invalidate the views
// they can affect; every mount still asks the server for a fresh copy.
const models = new Map();
const ui = new Map();

export const cached = key => models.get(key);
export const remember = (key, value) => { models.set(key, value); return value; };
export const uiState = key => ui.get(key) || {};
export const rememberUi = (key, value) => ui.set(key, value);
export function invalidate(...keys) {
  for (const key of keys) models.delete(key);
  for (const key of [...models.keys()]) {
    if (keys.some(prefix => key.startsWith(`${prefix}:`))) models.delete(key);
  }
}

export function invalidateForMutation(path) {
  if (path === '/api/executions/delete-logs' || /^\/api\/executions\/[^/]+\/logs$/.test(path)) {
    invalidate('runs', 'overview', 'packages', 'system');
  } else if (path.startsWith('/api/executions/') || path === '/api/run') {
    invalidate('runs', 'overview', 'packages', 'system');
  } else if (path.startsWith('/api/workflows/') || path.startsWith('/api/recipes/')) {
    invalidate('recipes', 'packages', 'overview', 'system');
  } else if (path.startsWith('/api/packages/') || path.startsWith('/api/repository/')) {
    invalidate('packages', 'overview', 'system');
  } else if (path === '/api/settings') {
    invalidate('settings', 'system', 'overview', 'packages');
  }
}
