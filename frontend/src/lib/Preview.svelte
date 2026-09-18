<script>
    // The app pane, in two modes. LIVE is the session's authoring
    // runtime — a sandboxed iframe (opaque origin, so app code can't
    // reach the studio API) that reloads on the runtime's version tick.
    // PUBLISHED is the app's own URL: frozen code at the version that
    // URL currently serves, over the app's own db. The toggle is the
    // whole point — what you are building beside what people have.
    import { api } from './api.js'
    import PublishedPanel from './PublishedPanel.svelte'

    // `readonly` is a delegate: publishing commits its open work and
    // puts a version behind a public URL, which is the parent agent's
    // call, not a reader's. The published panel stays — an app is
    // addressed by token and outlives whatever session made it.
    let { rt, onSwitch, readonly = false } = $props()

    let mode = $state('live') // 'live' | 'published'
    let manual = $state(0)
    let composing = $state(false) // the publish form is open
    let draft = $state('')
    let error = $state(null)
    // a publish in flight: it holds the session until the version
    // lands, and the route refuses a second one meanwhile
    let publishing = $state(false)
    let panel = $state(false)

    // the session's current app: the one it published to last, which is
    // what an unqualified `publish` extends and what the toggle shows
    const app = $derived(rt.apps[0] ?? null)
    const changed = $derived(app?.changed_since ?? null)

    // The version the count is measured against. The row names it as
    // `since` where the server sends one; a row without it was counted
    // against the version the URL serves, so that is the honest
    // fallback — a label naming any other version would describe a
    // count nobody took.
    const newest = $derived(changed?.since ?? app?.current ?? null)

    // The button is the dirty indicator: it names what a click would do
    // and, once the live tree matches the newest version, what already
    // happened. Publishing an unchanged tree is still allowed — it just
    // has nothing to announce, so the label is dimmed rather than gone.
    const clean = $derived(!!app && !changed?.count)
    const label = $derived(
        !app
            ? 'publish'
            : changed?.count
              ? `publish · ${changed.count} file${changed.count === 1 ? '' : 's'}`
              : `published ${newest ?? app.current}`,
    )
    const pubTitle = $derived(
        rt.busy
            ? 'waits for the turn'
            : changed?.count
              ? `changed since ${newest}:\n${changed.paths.join('\n')}`
              : app
                ? `add a version to ${app.title} — the URL moves to it, the old versions stay`
                : 'freeze this app behind a URL of its own that keeps serving while you keep working',
    )
    // The route refuses a publish while a turn runs (the version would
    // race the commit), so the controls wait rather than offer a click
    // whose only outcome is a refusal.
    const held = $derived(rt.busy || publishing)

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

    // The field starts empty: blank means "let the server name it", the
    // same as the plain click, so a prefilled vN would only be a copy of
    // the server's rule waiting to drift out of step with it.
    function startPublish() {
        draft = ''
        error = null
        composing = true
    }

    // Publishing does NOT move the pane to `published`: swapping what
    // the human is looking at mid-conversation loses their place. The
    // transcript marker and the button's new label are the feedback.
    async function publish(name) {
        composing = false
        error = null
        publishing = true
        try {
            await rt.publish(name ? { name } : {})
        } catch (e) {
            error = e.message
        } finally {
            publishing = false
        }
    }

    // The app's URL is stable by design, so the CURRENT VERSION has to
    // be in the cache-busting key: making an older version current
    // changes what the URL serves without changing the URL, and a
    // keyed iframe with an identical src would keep showing the old one.
    const src = $derived(
        mode === 'published' && app
            ? `${app.url}?v=${app.current}-${rt.version + manual}`
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
        {#if readonly}
            <!-- nothing to offer: the publish route refuses this
                 session, and a button whose only outcome is a refusal
                 reads as a broken one -->
        {:else if composing}
            <!-- svelte-ignore a11y_autofocus -->
            <input
                class="vname"
                aria-label="version name"
                placeholder="name"
                autofocus
                bind:value={draft}
                onkeydown={(e) => {
                    if (e.key === 'Enter') publish(draft.trim())
                    else if (e.key === 'Escape') composing = false
                }}
            />
            <button
                class="small accent"
                onclick={() => publish(draft.trim())}
                disabled={held}
                title={rt.busy ? 'waits for the turn' : null}>publish</button
            >
            <button class="small" onclick={() => (composing = false)}>cancel</button>
        {:else}
            <div class="split">
                <button
                    class="small accent pub-main"
                    class:clean
                    onclick={() => publish('')}
                    disabled={held}
                    title={pubTitle}
                >
                    {label}
                </button>
                <button
                    class="small accent caret"
                    aria-label="publish as…"
                    title={rt.busy ? 'waits for the turn' : 'name the version yourself'}
                    onclick={startPublish}
                    disabled={held}>▾</button
                >
            </div>
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
    <PublishedPanel
        apps={rt.apps}
        onChanged={() => rt.syncApps()}
        {onSwitch}
        onClose={() => (panel = false)}
    />
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
    .small:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .small:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .small.accent {
        border-color: var(--accent);
        color: var(--accent);
    }
    .small.accent:hover:not(:disabled) {
        background: color-mix(in srgb, var(--accent) 15%, transparent);
    }
    /* the label and its caret are one control: adjoining edges lose
       their rounding and the divider between them is a single border */
    .split {
        display: flex;
    }
    .split .small {
        border-radius: 0;
    }
    .split .small:first-child {
        border-top-left-radius: 6px;
        border-bottom-left-radius: 6px;
    }
    .split .caret {
        border-left: none;
        border-top-right-radius: 6px;
        border-bottom-right-radius: 6px;
        padding: 0.2rem 0.3rem;
        line-height: 1;
    }
    /* nothing to save: still clickable (a named version of an unchanged
       tree is allowed), just not asking to be clicked */
    .pub-main.clean {
        opacity: 0.55;
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
