<script>
    // The app pane, in two modes. LIVE is the session's authoring
    // runtime — a sandboxed iframe (opaque origin, so app code can't
    // reach the studio API) that reloads on the runtime's version tick.
    // PUBLISHED is the app's own URL: frozen code at the version that
    // URL currently serves, over the app's own db. The toggle is the
    // whole point — what you are building beside what people have.
    import { api } from './api.js'
    import PublishedPanel from './PublishedPanel.svelte'

    let { rt, onSwitch } = $props()

    let mode = $state('live') // 'live' | 'published'
    let manual = $state(0)
    let composing = $state(false) // the publish form is open
    let draft = $state('')
    let error = $state(null)
    let panel = $state(false)

    // the session's current app: the one it published to last, which is
    // what an unqualified `publish` extends and what the toggle shows
    const app = $derived(rt.apps[0] ?? null)
    const changed = $derived(app?.changed_since ?? null)

    // Mirrors the server's default (v1, v2, ... skipping taken names)
    // so the form can show the name before it exists. The server still
    // decides: leave the field as it came and it is sent verbatim, and
    // a name it won't take comes back as an error, not a surprise.
    function nextVersion(a) {
        const taken = new Set((a?.versions ?? []).map((v) => v.name))
        let n = taken.size + 1
        while (taken.has(`v${n}`)) n++
        return `v${n}`
    }

    // a session switch is a different app entirely: forget the form,
    // the error, and any published view of the old session
    $effect(() => {
        void rt.name
        mode = 'live'
        composing = false
        error = null
        rt.loadApps()
    })

    // publishing, turns, uploads and restores all move the app list
    $effect(() => {
        void rt.version
        rt.loadApps()
    })

    // unpublishing the last app leaves nothing to show: fall back to
    // live rather than the "no app yet" empty state over a live app
    $effect(() => {
        if (!app && mode === 'published') mode = 'live'
    })

    function startPublish() {
        draft = nextVersion(app)
        error = null
        composing = true
    }

    async function publish() {
        const name = draft.trim()
        composing = false
        error = null
        try {
            await rt.publish(name ? { name } : {})
            mode = 'published'
        } catch (e) {
            error = e.message
        }
    }

    const src = $derived(
        mode === 'published' && app
            ? `${app.url}?v=${rt.version + manual}`
            : `/preview/${rt.name}/?v=${rt.version + manual}`,
    )

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

    const showing = $derived(mode === 'published' ? app != null : hasApp)
</script>

<div class="preview">
    <div class="bar">
        <div class="toggle" role="group" aria-label="preview source">
            <button class="seg" class:on={mode === 'live'} onclick={() => (mode = 'live')}
                >live</button
            >
            <button
                class="seg"
                class:on={mode === 'published'}
                disabled={!app}
                title={app
                    ? `serving ${app.current} at ${app.url}`
                    : 'nothing published from this session yet'}
                onclick={() => (mode = 'published')}>published</button
            >
        </div>
        {#if changed?.count}
            <span
                class="badge"
                title={`changed since ${app.current}:\n${changed.paths.join('\n')}`}
            >
                {changed.count} app file{changed.count === 1 ? '' : 's'} changed since
                {app.current}
            </span>
        {/if}
        <span class="grow"></span>
        <button class="small" onclick={() => manual++}>reload</button>
        <a
            class="small open"
            href={mode === 'published' && app ? app.url : `/preview/${rt.name}/`}
            target="_blank"
            rel="noopener">open ↗</a
        >
        {#if rt.apps.length}
            <button class="small" onclick={() => (panel = true)}>published…</button>
        {/if}
        {#if composing}
            <input
                class="vname"
                aria-label="version name"
                bind:value={draft}
                onkeydown={(e) => {
                    if (e.key === 'Enter') publish()
                    else if (e.key === 'Escape') composing = false
                }}
            />
            <button class="small accent" onclick={publish}>publish</button>
            <button class="small" onclick={() => (composing = false)}>cancel</button>
        {:else}
            <button
                class="small accent"
                onclick={startPublish}
                title={app
                    ? `add a version to ${app.title} — the URL moves to it, the old versions stay`
                    : 'freeze this app behind a URL of its own that keeps serving while you keep working'}
            >
                publish
            </button>
        {/if}
    </div>
    {#if error}
        <div class="pub-error">{error}</div>
    {/if}
    {#if showing}
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
                    <code>/workspace/app</code> serves live here as it takes shape.
                </p>
            </div>
        </div>
    {/if}
</div>

{#if panel}
    <PublishedPanel {rt} {onSwitch} onClose={() => (panel = false)} />
{/if}

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
    .toggle {
        display: flex;
        border: 1px solid var(--border);
        border-radius: 6px;
        overflow: hidden;
    }
    .seg {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.7rem;
        padding: 0.18rem 0.55rem;
        cursor: pointer;
    }
    .seg:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .seg.on {
        color: var(--text);
        background: var(--surface-hover);
        font-weight: 600;
    }
    .seg:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .badge {
        color: var(--warning);
        border: 1px solid color-mix(in srgb, var(--warning) 45%, transparent);
        border-radius: 999px;
        font-size: 0.66rem;
        padding: 0.05rem 0.5rem;
        cursor: help;
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
    .vname {
        width: 6.5rem;
        background: none;
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font: inherit;
        font-size: 0.72rem;
        padding: 0.16rem 0.4rem;
        outline: none;
    }
    .vname:focus {
        border-color: var(--accent);
    }
    .pub-error {
        color: var(--error);
        font-size: 0.72rem;
        padding: 0.3rem 0.7rem;
        border-bottom: 1px solid var(--border);
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
