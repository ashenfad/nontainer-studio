import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Build straight into the Python package: the committed bundle is what
// `nontainer-studio` serves, so clone-and-run needs no node. Dev mode
// proxies the API (and app-serving routes) to the Python server —
// `npm run dev` against a running `nontainer-studio` gives hot reload.
export default defineConfig({
    plugins: [svelte()],
    // the Python server mounts the bundle dir at /static (index.html is
    // served at / separately), so asset URLs must be /static-rooted
    base: '/static/',
    build: {
        outDir: '../nontainer_studio/static',
        emptyOutDir: true,
    },
    server: {
        proxy: Object.fromEntries(
            ['/api', '/preview', '/apps'].map((p) => [
                p,
                'http://127.0.0.1:8321',
            ]),
        ),
    },
})
