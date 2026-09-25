import {writable} from 'svelte/store';
const key = 'debBuilder24Theme';
const valid = new Set(['system', 'light', 'dark']);
export const theme = writable('system');
export function setTheme(value) {
  const selected = valid.has(value) ? value : 'system';
  localStorage.setItem(key, selected);
  theme.set(selected);
  document.documentElement.dataset.theme = selected === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : selected;
}
export function initTheme() {
  setTheme(localStorage.getItem(key));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {let current; const unsub = theme.subscribe(value => current = value); unsub(); if (current === 'system') setTheme('system');});
}
