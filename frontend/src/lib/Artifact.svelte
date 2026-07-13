<script>
    // Dispatch a workspace artifact to its renderer by extension — the
    // `ui = {...}` materialization contract (.plotly.json / .table.json
    // / images / .html / .json / .txt).
    import { fileUrl } from './api.js'
    import PlotlyChart from './PlotlyChart.svelte'
    import DataTable from './DataTable.svelte'
    import { looksLikePlotly } from './sniff.js'

    let { session, path, name = '' } = $props()

    const url = $derived(fileUrl(session, path))
    const kind = $derived.by(() => {
        if (/\.plotly\.json$/.test(path)) return 'plotly'
        if (/\.table\.json$/.test(path)) return 'table'
        if (/\.(png|jpe?g|gif|webp)$/i.test(path)) return 'image'
        if (/\.html$/.test(path)) return 'html'
        if (/\.json$/.test(path)) return 'json' // sniffed after fetch
        if (/\.txt$/.test(path)) return 'text'
        return 'link'
    })

    let text = $state(null)
    $effect(() => {
        if (kind !== 'html' && kind !== 'text' && kind !== 'json') return
        let dead = false
        text = null
        fetch(url)
            .then((r) => r.text())
            .then((t) => !dead && (text = t))
        return () => (dead = true)
    })

    // theme prelude so bare html artifacts don't render black-on-black
    const prelude =
        '<style>:root{color-scheme:dark}body{font-family:ui-sans-serif,system-ui;margin:0.5rem;color:#ddd;background:transparent}</style>'
</script>

{#if kind === 'plotly'}
    <PlotlyChart {url} />
{:else if kind === 'table'}
    <DataTable {url} />
{:else if kind === 'image'}
    <img class="artifact-img" src={url} alt={name || path} title={path} />
{:else if kind === 'html'}
    {#if text === null}
        <div class="loading">…</div>
    {:else}
        <iframe
            class="artifact-html"
            title={name || path}
            sandbox="allow-scripts"
            srcdoc={prelude + text}
        ></iframe>
    {/if}
{:else if kind === 'json'}
    <!-- agents write fig.write_json('/ui/x.json'): plain .json, plotly
         inside — render the chart when the content says so -->
    {#if text === null}
        <div class="loading">…</div>
    {:else if looksLikePlotly(text)}
        <PlotlyChart {url} />
    {:else}
        <details class="artifact-text" open>
            <summary>{name || path.split('/').pop()}</summary>
            <pre>{text}</pre>
        </details>
    {/if}
{:else if kind === 'text'}
    <details class="artifact-text" open>
        <summary>{name || path.split('/').pop()}</summary>
        <pre>{text ?? '…'}</pre>
    </details>
{:else}
    <a href={url} target="_blank" rel="noopener">{name || path}</a>
{/if}

<style>
    .artifact-img {
        max-width: 100%;
        border-radius: 8px;
        border: 1px solid var(--border);
        margin: 0.5rem 0;
        display: block;
    }
    .artifact-html {
        width: 100%;
        height: 320px;
        border: 1px solid var(--border);
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.02);
        margin: 0.5rem 0;
    }
    .artifact-text {
        margin: 0.5rem 0;
        font-size: 0.8rem;
    }
    .artifact-text summary {
        cursor: pointer;
        color: var(--text-muted);
        font-size: 0.75rem;
    }
    .artifact-text pre {
        max-height: 260px;
        overflow: auto;
        background: rgba(255, 255, 255, 0.06);
        border-radius: 6px;
        padding: 0.5rem 0.7rem;
        margin-top: 0.3rem;
        font-size: 0.75rem;
        white-space: pre-wrap;
    }
    .loading {
        color: var(--text-muted);
        font-size: 0.8rem;
    }
    a {
        color: var(--link);
    }
</style>
