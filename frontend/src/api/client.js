export class ApiError extends Error {
  constructor({status = 0, code = 'request_failed', message = 'Request failed', details = {}, kind = 'http'} = {}) {
    super(message);
    this.name = 'ApiError';
    Object.assign(this, {status, code, details, kind});
    this.retryable = kind === 'network' || kind === 'timeout' || status >= 500;
  }
}

export function canonicalError(status, payload) {
  const error = payload?.error;
  const fallback = ({400: 'invalid_request', 401: 'authentication_required', 403: 'forbidden', 404: 'not_found', 409: 'request_conflict'})[status] || 'request_failed';
  if (error && typeof error === 'object') {
    return new ApiError({status, code: error.code || fallback, message: error.message || 'Request failed', details: error.details || {}});
  }
  return new ApiError({status, code: fallback, message: typeof error === 'string' ? error : 'Request failed'});
}

export async function request(path, {signal, timeout = 20000, fetchImpl = fetch} = {}) {
  if (!path.startsWith('/api/')) throw new TypeError('API path required');
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, {once: true});
  let timedOut = false;
  const timer = setTimeout(() => {timedOut = true; controller.abort();}, timeout);
  try {
    const response = await fetchImpl(path, {method: 'GET', credentials: 'same-origin', headers: {Accept: 'application/json'}, signal: controller.signal});
    const type = response.headers.get('content-type') || '';
    const payload = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) throw canonicalError(response.status, payload);
    return payload;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (timedOut) throw new ApiError({code: 'request_timeout', message: 'Request timed out', kind: 'timeout'});
    if (controller.signal.aborted) throw new DOMException('Request aborted', 'AbortError');
    throw new ApiError({code: 'network_unavailable', message: 'Network unavailable', kind: 'network'});
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

const id = value => encodeURIComponent(value);
export const api = {
  auth: opts => request('/api/auth/status', opts),
  status: opts => request('/api/status', opts),
  dashboard: opts => request('/api/dashboard', opts),
  packages: opts => request('/api/packages', opts),
  package: (name, opts) => request(`/api/packages/${id(name)}`, opts),
  runs: opts => request('/api/executions', opts),
  run: (runId, opts) => request(`/api/executions/${id(runId)}`, opts),
  logs: (runId, verbosity, after, opts) => request(`/api/executions/${id(runId)}/logs?verbosity=${id(verbosity)}&after=${after}`, opts),
  diagnostics: opts => request('/api/system/diagnostics', opts),
  storage: opts => request('/api/storage', opts),
};
