import {defineConfig} from 'vite';
import {svelte} from '@sveltejs/vite-plugin-svelte';
import {readFileSync} from 'node:fs';

const version = readFileSync(new URL('../debbuilder/__init__.py', import.meta.url),'utf8').match(/__version__ = "([^"]+)"/)?.[1];
if (!version) throw new Error('DebBuilder version is missing from the canonical Python package');

export default defineConfig({
  plugins: [svelte()],
  define: {'__DEBBUILDER_VERSION__': JSON.stringify(version)},
  base: './',
  server: {proxy: {'/api': {target: process.env.DEBBUILDER_DEV_API || 'http://127.0.0.1:8765', changeOrigin: true}}},
  build: {assetsDir: 'assets', outDir: 'dist'},
});
