import {writable} from 'svelte/store';

const pages = new Set(['overview', 'packages', 'runs', 'recipes', 'system', 'settings']);
function decodeSegment(segment) {try {return decodeURIComponent(segment);} catch {return '';}}
export function parseLocation(hash = '') {
  const [page, id = ''] = hash.replace(/^#\/?/, '').split('/').map(decodeSegment);
  return {page: pages.has(page) ? page : 'overview', id};
}
export const location = writable(parseLocation(typeof window === 'undefined' ? '' : window.location.hash));
let guard = null;
let current = parseLocation(typeof window === 'undefined' ? '' : window.location.hash);
export function setNavigationGuard(next) {guard = next; return () => {if (guard === next) guard = null;};}
if (typeof window !== 'undefined') window.addEventListener('hashchange', async () => {
  const next = parseLocation(window.location.hash);
  if (next.page === current.page && next.id === current.id) return;
  const previous = current;
  // Restore the address before asking, so Cancel keeps both URL and draft.
  window.history.replaceState(null,'',`#/${previous.page}${previous.id ? `/${encodeURIComponent(previous.id)}` : ''}`);
  await navigate(next.page,next.id);
});
export async function navigate(page, id = '') {
  if (!pages.has(page)) return;
  if ((page !== current.page || id !== current.id) && guard && !(await guard({page,id}))) return;
  current = {page,id};
  window.location.hash = `/${page}${id ? `/${encodeURIComponent(id)}` : ''}`;
  location.set(current);
}
