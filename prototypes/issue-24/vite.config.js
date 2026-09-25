import {defineConfig} from 'vite';
import {svelte} from '@sveltejs/vite-plugin-svelte';
import {fileURLToPath} from 'node:url';
export default defineConfig({plugins: [svelte()], base: './', build: {outDir: 'dist', manifest: true, emptyOutDir: true, rollupOptions: {input: {admin: fileURLToPath(new URL('./index.html',import.meta.url)), repository: fileURLToPath(new URL('./repository-public.html',import.meta.url))}}}});
