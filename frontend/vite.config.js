import {defineConfig} from 'vite';
import {svelte} from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  base: './',
  server: {proxy: {'/api': {target: process.env.DEBBUILDER_DEV_API || 'http://127.0.0.1:8765', changeOrigin: true}}},
  build: {manifest: true, assetsDir: 'assets', outDir: 'dist'},
});
