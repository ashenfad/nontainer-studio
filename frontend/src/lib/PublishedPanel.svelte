<script>
    // The verbs on a published app: its link, its versions, and what
    // can be done to them. The strip sits under the frame that serves
    // the app, in the preview and in the rail's app view alike. The
    // verbs are the app's, not the caller's — so the app is passed in
    // rather than read off a runtime (it may have no session left),
    // and `onChanged` says who reloads afterwards.
    import { api } from './api.js'
    import { rail, refreshSessions } from './runtime.svelte.js'

    let { app, onChanged, onSwitch } = $props()

    let busy = $state(null) // "verb:token/version" while a call is in flight
    let armed = $state(null) // a destructive verb one tap from happening
    let error = $state(null)
    let copied = $state(null)

    // Branching needs the origin session's branch, which delete takes
    // with it. The app itself is untouched by that — it owns its db and
    // each version lives on a branch of its own — so the row says why
    // the one verb is gone rather than hiding it.
    const alive = $derived(rail.sessions.some((s) => s.name === app.session))

    function arm(key) {
        // first tap arms, second commits — the rail's delete pattern,
        // for the same reason: unpublishing takes down a live URL
        if (armed !== key) {
            armed = key
            setTimeout(() => armed === key && (armed = null), 4000)
            return false
        }
        armed = null
        return true
    }

    async function run(key, call) {
        busy = key
        error = null
        try {
            await call()
            await onChanged()
        } catch (e) {
            error = e.message
        } finally {
            busy = null
        }
    }

    const makeCurrent = (v) =>
        run(`current:${app.token}/${v.name}`, () =>
            api(`/api/apps/${app.token}/current`, { version: v.name }),
        )

    const dropVersion = (v) => {
        const key = `drop:${app.token}/${v.name}`
        if (!arm(key)) return
        return run(key, () =>
            api(`/api/apps/${app.token}/versions/${v.name}`, undefined, 'DELETE'),
        )
    }

    const unpublish = () => {
        const key = `unpublish:${app.token}`
        if (!arm(key)) return
        return run(key, () => api(`/api/apps/${app.token}`, undefined, 'DELETE'))
    }

    async function copyLink() {
        try {
            await navigator.clipboard.writeText(`${location.origin}${app.url}`)
            copied = app.token
            setTimeout(() => copied === app.token && (copied = null), 1500)
        } catch (e) {
            error = `couldn't copy: ${e.message}`
        }
    }

    // Branching opens the child where the publish stood — files and
    // conversation both — so switching to it is the whole action.
    async function branch(v) {
        const key = `branch:${app.token}/${v.name}`
        busy = key
        error = null
        try {
            const child = await api(
                `/api/apps/${app.token}/versions/${v.name}/branch`,
                {},
            )
            await refreshSessions()
            onSwitch(child.name)
        } catch (e) {
            error = e.message
        } finally {
            busy = null
        }
    }

    function when(seconds) {
        if (!seconds) return ''
        return new Date(seconds * 1000).toLocaleString(undefined, {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
        })
    }
</script>

<div class="panel">
    {#if error}
        <p class="err">{error}</p>
    {/if}
    <div class="app-head">
        <a class="app-url" href={app.url} target="_blank" rel="noopener"
            >{app.title} ↗</a
        >
        <span class="grow"></span>
        <button class="verb" onclick={copyLink}
            >{copied === app.token ? 'copied' : 'copy link'}</button
        >
        <button
            class="verb danger"
            class:armed={armed === `unpublish:${app.token}`}
            disabled={busy != null}
            title="removes every version, the app's database, and the link stops working"
            onclick={unpublish}
        >
            {armed === `unpublish:${app.token}`
                ? `really unpublish — ${app.versions.length} version${app.versions.length === 1 ? '' : 's'}, its database, and the link stop working`
                : 'unpublish'}
        </button>
    </div>
    <code class="token">{app.url}</code>
    {#if app.description}
        <!-- What the app IS, generated from the session that built it
             at each publish (the human's own words win where they
             wrote any). Read-only here: it is a label on the app, not
             a verb. -->
        <p class="app-desc">{app.description}</p>
    {/if}
    <ul>
        {#each app.versions as v (v.name)}
            <li class:current={v.name === app.current}>
                <span class="vname">{v.name}</span>
                {#if v.name === app.current}
                    <span class="tag">serving</span>
                {/if}
                <span class="when">{when(v.created)}</span>
                <span class="grow"></span>
                {#if v.name !== app.current}
                    <button
                        class="verb"
                        disabled={busy != null}
                        onclick={() => makeCurrent(v)}
                        title="point this app's URL at {v.name}">make current</button
                    >
                {/if}
                <button
                    class="verb"
                    disabled={busy != null || !alive}
                    onclick={() => branch(v)}
                    title={alive
                        ? `open a new session with the files and the conversation as they stood at ${v.name}`
                        : `the session that published this is deleted — its files are still served here, but there is no conversation left to branch`}
                    >branch from</button
                >
                {#if v.name !== app.current && app.versions.length > 1}
                    <button
                        class="verb danger"
                        class:armed={armed === `drop:${app.token}/${v.name}`}
                        disabled={busy != null}
                        onclick={() => dropVersion(v)}
                        >{armed === `drop:${app.token}/${v.name}`
                            ? 'really delete'
                            : 'delete'}</button
                    >
                {/if}
            </li>
        {/each}
    </ul>
</div>

<style>
    /* The strip is bounded so the frame above it keeps most of the
       pane: the versions are the controls, the app is the subject. */
    .panel {
        background: var(--surface);
        border-top: 1px solid var(--border);
        padding: 0.6rem 0.8rem 0.8rem;
        flex: none;
        max-height: 40%;
        overflow-y: auto;
    }
    .err {
        color: var(--error);
        font-size: 0.75rem;
        margin-bottom: 0.5rem;
    }
    .app-head {
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    .app-url {
        color: var(--text);
        font-size: 0.85rem;
        text-decoration: none;
        font-weight: 600;
    }
    .app-url:hover {
        text-decoration: underline;
    }
    .token {
        display: block;
        color: var(--text-muted);
        font-size: 0.66rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        margin: 0.1rem 0 0.35rem;
    }
    .app-desc {
        margin: 0 0 0.45rem;
        color: var(--text-muted);
        font-size: 0.72rem;
        line-height: 1.35;
    }
    ul {
        list-style: none;
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
    }
    li {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        font-size: 0.74rem;
        padding: 0.2rem 0.35rem;
        border-radius: 6px;
    }
    li.current {
        background: var(--surface-hover);
    }
    .vname {
        color: var(--text);
        font-weight: 600;
    }
    .tag {
        color: var(--success);
        font-size: 0.64rem;
        border: 1px solid color-mix(in srgb, var(--success) 45%, transparent);
        border-radius: 999px;
        padding: 0 0.35rem;
    }
    .when {
        color: var(--text-muted);
        font-size: 0.66rem;
    }
    .grow {
        flex: 1;
    }
    .verb {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.68rem;
        padding: 0.12rem 0.45rem;
        cursor: pointer;
    }
    .verb:hover:not(:disabled) {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .verb:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .verb.danger:hover:not(:disabled),
    .verb.armed {
        color: var(--error);
        border-color: color-mix(in srgb, var(--error) 55%, transparent);
    }
</style>
