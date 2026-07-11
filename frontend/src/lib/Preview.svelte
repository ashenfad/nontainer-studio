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

    // the iframe is an opaque origin (we can't read its document), so
    // probe from the shell: no /app yet → friendly empty state. The
    // probe endpoint 200s either way (a /preview/ probe would console-
    // log a 404 on every empty session).
    let hasApp = $state(false)
    $effect(() => {
        void src
        let dead = false
        api(`/api/sessions/${rt.name}/app`)
            .then((d) => !dead && (hasApp = d.exists))
            .catch(() => {})
        return () => (dead = true)
    })
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
    {#if hasApp}
        {#key src}
            <!-- allow-modals: agent apps use alert()/confirm() for
                 error surfacing; a localhost demo pane gains nothing
                 by muting them. Still NO allow-same-origin — the app
                 stays an opaque origin, unable to reach the studio API. -->
            <iframe
                title="app preview"
                {src}
                sandbox="allow-scripts allow-forms allow-modals"
            ></iframe>
        {/key}
    {:else}
        <div class="no-app">
            <div>
                <h3>no app yet</h3>
                <p>
                    Ask the agent to build one — anything it writes under
                    <code>/app</code> serves live here as it takes shape.
                </p>
            </div>
        </div>
    {/if}
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
    .no-app {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        text-align: center;
        color: var(--text-muted);
    }
    .no-app h3 {
        color: var(--text);
        margin-bottom: 0.4rem;
        font-variation-settings: 'opsz' 48, 'SOFT' 80;
    }
    .no-app p {
        font-size: 0.8rem;
        max-width: 300px;
        line-height: 1.5;
    }
    .no-app code {
        background: rgba(255, 255, 255, 0.08);
        padding: 0 0.25rem;
        border-radius: 3px;
    }
</style>
