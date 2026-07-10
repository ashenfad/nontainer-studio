<script>
    // Live preview of /app: a sandboxed iframe (opaque origin — no
    // allow-same-origin, so app code can't reach the studio API)
    // dispatching into the session's authoring runtime. Reloads on the
    // runtime's version tick (turn done, upload, restore).
    import { api } from './api.js'

    let { rt } = $props()

    let published = $state(null)
    let manual = $state(0)

    // reset the publish banner when the session changes
    $effect(() => {
        void rt.name
        published = null
    })

    async function publish() {
        try {
            published = await api(`/api/sessions/${rt.name}/publish`, {})
        } catch (e) {
            published = { error: e.message }
        }
    }

    const src = $derived(`/preview/${rt.name}/?v=${rt.version + manual}`)
</script>

<div class="preview">
    <div class="bar">
        <code class="path">/preview/{rt.name}/</code>
        <button class="small" onclick={() => manual++}>reload</button>
        <a
            class="small open"
            href={`/preview/${rt.name}/`}
            target="_blank"
            rel="noopener">open ↗</a
        >
        <span class="grow"></span>
        {#if published?.url}
            <a class="published" href={published.url} target="_blank" rel="noopener">
                published ↗ <code>{published.checkpoint?.slice(0, 8)}</code>
            </a>
        {:else if published?.error}
            <span class="pub-error">{published.error}</span>
        {/if}
        <button
            class="small accent"
            onclick={publish}
            title="freeze the current state behind a shareable capability URL — the live session keeps moving, the snapshot doesn't"
        >
            publish
        </button>
    </div>
    {#key src}
        <iframe title="app preview" {src} sandbox="allow-scripts allow-forms"
        ></iframe>
    {/key}
</div>

<style>
    .preview {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
    }
    .bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.45rem 0.7rem;
        border-bottom: 1px solid var(--border);
        background: var(--surface);
        font-size: 0.75rem;
        flex-wrap: wrap;
    }
    .path {
        color: var(--text-muted);
        font-size: 0.72rem;
    }
    .grow {
        flex: 1;
    }
    .small {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.72rem;
        padding: 0.2rem 0.55rem;
        cursor: pointer;
        text-decoration: none;
    }
    .small:hover {
        color: var(--text);
        background: var(--surface-hover);
    }
    .small.accent {
        border-color: var(--accent);
        color: var(--accent);
    }
    .small.accent:hover {
        background: color-mix(in srgb, var(--accent) 15%, transparent);
    }
    .published {
        color: var(--success);
        font-size: 0.72rem;
        text-decoration: none;
    }
    .published:hover {
        text-decoration: underline;
    }
    .pub-error {
        color: var(--error);
        font-size: 0.72rem;
    }
    iframe {
        flex: 1;
        border: none;
        background: #fff;
    }
</style>
