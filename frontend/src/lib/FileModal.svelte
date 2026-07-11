<script>
    // The one rich file viewer (the agex-studio FileModal idea):
    // per-type renders over the raw file endpoint. Markdown renders,
    // code highlights, images display, ui-artifact formats reuse the
    // chat's own renderers.
    import { fileUrl } from './api.js'
    import { renderMarkdown, highlightCode } from './markdown.js'
    import { viewer, closeViewer } from './viewer.svelte.js'
    import PlotlyChart from './PlotlyChart.svelte'
    import DataTable from './DataTable.svelte'

    let { session } = $props()

    const MAX_TEXT = 100_000
    const CODE_LANGS = {
        py: 'python',
        js: 'javascript',
        mjs: 'javascript',
        ts: 'typescript',
        html: 'html',
        htm: 'html',
        css: 'css',
        json: 'json',
        sh: 'bash',
        bash: 'bash',
        sql: 'sql',
    }

    const path = $derived(viewer.path)
    const url = $derived(path ? fileUrl(session, path) : null)
    const ext = $derived((path?.split('.').pop() ?? '').toLowerCase())

    const kind = $derived.by(() => {
        if (!path) return null
        if (/\.plotly\.json$/.test(path)) return 'plotly'
        if (/\.table\.json$/.test(path)) return 'table'
        if (/\.(png|jpe?g|gif|webp|svg)$/i.test(path)) return 'image'
        if (ext === 'md' || ext === 'markdown') return 'markdown'
        if (ext in CODE_LANGS) return 'code'
        return 'text'
    })

    let text = $state(null)
    let failed = $state(null)

    $effect(() => {
        if (!url || kind === 'plotly' || kind === 'table' || kind === 'image')
            return
        let dead = false
        text = null
        failed = null
        fetch(url)
            .then((r) => (r.ok ? r.text() : Promise.reject(new Error(r.statusText))))
            .then((t) => {
                if (dead) return
                text =
                    t.length > MAX_TEXT
                        ? t.slice(0, MAX_TEXT) + '\n…[truncated]'
                        : t
            })
            .catch((e) => !dead && (failed = e.message))
        return () => (dead = true)
    })

    $effect(() => {
        if (!path) return
        function onEsc(e) {
            if (e.key === 'Escape') closeViewer()
        }
        document.addEventListener('keydown', onEsc)
        return () => document.removeEventListener('keydown', onEsc)
    })
</script>

{#if path}
    <div
        class="overlay"
        role="presentation"
        onclick={(e) => {
            if (e.target === e.currentTarget) closeViewer()
        }}
    >
        <div class="modal" role="dialog" aria-label={path}>
            <div class="head">
                <code class="path">{path}</code>
                <a class="download" href={url} download={path.split('/').pop()}
                    >download</a
                >
                <button class="close" aria-label="close" onclick={closeViewer}
                    >×</button
                >
            </div>
            <div class="body">
                {#if failed}
                    <div class="error">cannot read {path}: {failed}</div>
                {:else if kind === 'image'}
                    <img src={url} alt={path} />
                {:else if kind === 'plotly'}
                    <PlotlyChart {url} />
                {:else if kind === 'table'}
                    <DataTable {url} />
                {:else if text === null}
                    <div class="loading">…</div>
                {:else if kind === 'markdown'}
                    <!-- eslint-disable-next-line svelte/no-at-html-tags — DOMPurify'd -->
                    <div class="markdown">{@html renderMarkdown(text)}</div>
                {:else if kind === 'code'}
                    <!-- eslint-disable-next-line svelte/no-at-html-tags — hljs output over escaped text -->
                    <pre class="code"><code class="hljs"
                            >{@html highlightCode(text, CODE_LANGS[ext])}</code
                        ></pre>
                {:else}
                    <pre class="plain">{text}</pre>
                {/if}
            </div>
        </div>
    </div>
{/if}

<style>
    .overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.6);
        z-index: 200;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 2rem;
    }
    .modal {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        width: min(860px, 100%);
        max-height: 100%;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
    }
    .head {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        padding: 0.6rem 0.9rem;
        border-bottom: 1px solid var(--border);
    }
    .path {
        flex: 1;
        font-size: 0.78rem;
        color: var(--text-muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .download {
        color: var(--link);
        font-size: 0.75rem;
        text-decoration: none;
    }
    .download:hover {
        text-decoration: underline;
    }
    .close {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 1.2rem;
        line-height: 1;
        padding: 0.1rem 0.35rem;
        border-radius: 5px;
        cursor: pointer;
    }
    .close:hover {
        background: var(--surface-hover);
        color: var(--text);
    }
    .body {
        overflow: auto;
        padding: 1rem 1.1rem;
        font-size: 0.85rem;
    }
    .body img {
        max-width: 100%;
        border-radius: 8px;
        display: block;
        margin: 0 auto;
    }
    .code,
    .plain {
        font-size: 0.76rem;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
    }
    .loading,
    .error {
        color: var(--text-muted);
        font-size: 0.8rem;
    }
    .error {
        color: var(--error);
    }
</style>
