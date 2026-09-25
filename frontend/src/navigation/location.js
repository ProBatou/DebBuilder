import {writable} from 'svelte/store';

const pages = new Set(['overview', 'packages', 'runs', 'system']);
export function parseLocation(hash = '') {
  const [page, id = ''] = hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  return {page: pages.has(page) ? page : 'overview', id};
}
export const location = writable(parseLocation(typeof window === 'undefined' ? '' : window.location.hash));
if (typeof window !== 'undefined') window.addEventListener('hashchange', () => location.set(parseLocation(window.location.hash)));
export function navigate(page, id = '') {
  if (!pages.has(page)) return;
  window.location.hash = `/${page}${id ? `/${encodeURIComponent(id)}` : ''}`;
  location.set({page, id});
}
