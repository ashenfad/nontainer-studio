<script>
    // Session list: server view overlaid with live runtime status.
    // Pulsing dot = a turn in flight; steady green = finished while
    // you were elsewhere (cleared on focus).
    import { rail, published, peekRuntime, renameSession } from './runtime.svelte.js'

    const DEFAULT_TITLE = 'New session' // mirrors the server's fallback

    let { active, activeApp, onSwitch, onCreate, onDelete, onFork, onOpenApp } =
        $props()

    // An app's origin session may be deleted — the app is not. That is
    // why this list is store-wide and why the row says so: those apps
    // are reachable from no session's own panel.
    const originOf = (a) => rail.sessions.find((s) => s.name === a.session) ?? null

    function ago(seconds) {
        if (!seconds) return ''
        const mins = Math.round((Date.now() / 1000 - seconds) / 60)
        if (mins < 1) return 'just now'
        if (mins < 60) return `${mins}m ago`
        const hours = Math.round(mins / 60)
        if (hours < 24) return `${hours}h ago`
        return `${Math.round(hours / 24)}d ago`
    }

    let armed = $state(null) // session name whose delete is one tap away
    let renaming = $state(null) // session name being retitled
    let draft = $state('')
    let cancelled = false // Escape sets it; blur (which commits) checks it

    function startRename(s) {
        renaming = s.name
        // the default isn't a name worth editing — start empty there
        draft = s.title === DEFAULT_TITLE ? '' : s.title
    }

    async function commitRename(s) {
        const next = draft.trim()
        renaming = null
        if (cancelled) {
            cancelled = false // Escape: blur still fires, don't save
            return
        }
        // Unchanged means "I looked at it", not "make it mine": writing
        // here would silently promote the AGENT's title to the human's
        // and pin it, so the agent could never update the label again.
        if (next === s.title) return
        try {
            await renameSession(s.name, next)
        } catch (e) {
            // Say so where the human is looking. Without this the title
            // just snaps back to the old one — indistinguishable from
            // "the rename didn't take" and from "I mistyped".
            peekRuntime(s.name)?.messages.push({
                role: 'error',
                text: `rename failed: ${e.message}`,
            })
        }
    }

    function autofocus(node) {
        node.focus()
        node.select()
    }

    function del(s, e) {
        e.stopPropagation()
        if (armed !== s.name) {
            armed = s.name // first tap arms; second confirms
            setTimeout(() => {
                if (armed === s.name) armed = null
            }, 3000)
            return
        }
        armed = null
        onDelete(s.name)
    }

    function fork(s, e) {
        e.stopPropagation()
        armed = null
        onFork(s.name)
    }

    function status(s) {
        // server busy is the truth for background sessions (they hold
        // no event stream); the foreground runtime is fresher between
        // rail polls
        const rt = peekRuntime(s.name)
        if (s.busy || (rt?.foreground && rt.busy)) return 'busy'
        if (rail.unseen.includes(s.name) || rt?.unseen) return 'unseen'
        return ''
    }

</script>

<nav class="rail">
    <div class="brand">nontainer<span>-studio</span></div>
    <div class="rail-title">sessions</div>
    <div class="items">
        {#each rail.sessions as s (s.name)}
            <div class="row" class:active={s.name === active}>
                {#if renaming === s.name}
                    <!-- the whole row, not just the label: an input
                         nested in the switch button would swallow its
                         own clicks -->
                    <input
                        class="rename"
                        aria-label="rename session"
                        placeholder={s.title}
                        bind:value={draft}
                        use:autofocus
                        onblur={() => commitRename(s)}
                        onkeydown={(e) => {
                            if (e.key === 'Enter') e.currentTarget.blur()
                            else if (e.key === 'Escape') {
                                cancelled = true
                                e.currentTarget.blur()
                            }
                        }}
                    />
                {:else}
                    <button
                        class="item"
                        onclick={() => {
                            armed = null
                            onSwitch(s.name)
                        }}
                        ondblclick={() => startRename(s)}
                    >
                        <span class="dot {status(s)}"></span>
                        <span class="name" title="{s.title} (double-click to rename)"
                            >{s.title}</span
                        >
                    </button>
                    <button
                        class="fork"
                        title="fork {s.title} — same files and conversation, its own universe from here"
                        aria-label="fork {s.title}"
                        onclick={(e) => fork(s, e)}
                    >
                        ⑂
                    </button>
                    <button
                        class="delete"
                        class:armed={armed === s.name}
                        title={armed === s.name
                            ? 'click again to delete everything this session owns'
                            : `delete ${s.title}`}
                        aria-label="delete {s.title}"
                        onclick={(e) => del(s, e)}
                    >
                        {armed === s.name ? 'sure?' : '×'}
                    </button>
                {/if}
            </div>
        {/each}
    </div>
    <div class="new">
        <button class="new-btn" onclick={() => onCreate()}>+ New session</button>
    </div>
    {#if published.apps.length}
        <div class="rail-title">published</div>
        <div class="apps">
            {#each published.apps as a (a.token)}
                {@const origin = originOf(a)}
                <button
                    class="app-row"
                    class:active={a.token === activeApp}
                    onclick={() => onOpenApp(a.token)}
                >
                    <span class="app-name" title={a.title}>{a.title}</span>
                    <span class="app-meta">
                        {a.current} · {a.versions.length} version{a.versions
                            .length === 1
                            ? ''
                            : 's'} · {ago(a.published)}
                    </span>
                    {#if origin}
                        <!-- svelte-ignore a11y_click_events_have_key_events -->
                        <!-- svelte-ignore a11y_no_static_element_interactions -->
                        <span
                            class="app-origin link"
                            title="open {origin.title}"
                            onclick={(e) => {
                                e.stopPropagation()
                                onSwitch(a.session)
                            }}>from {origin.title}</span
                        >
                    {:else}
                        <span class="app-origin gone">session deleted</span>
                    {/if}
                </button>
            {/each}
        </div>
    {/if}
</nav>

<style>
    .rail {
        width: 200px;
        flex-shrink: 0;
        border-right: 1px solid var(--border);
        background: var(--surface);
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }
    .brand {
        font-family: var(--font-display);
        font-variation-settings: 'opsz' 40, 'SOFT' 80;
        font-weight: 600;
        font-size: 1.05rem;
        padding: 0.9rem 1rem 0.7rem;
        border-bottom: 1px solid var(--border);
        color: var(--text);
    }
    .brand span {
        color: var(--accent);
    }
    .rail-title {
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--text-muted);
        padding: 0.8rem 1rem 0.3rem;
    }
    .apps {
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        padding: 0 0.5rem 0.6rem;
        max-height: 40%;
        flex: none;
    }
    .app-row {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 0.05rem;
        background: none;
        border: none;
        border-radius: 6px;
        padding: 0.3rem 0.45rem;
        cursor: pointer;
        text-align: left;
        width: 100%;
    }
    .app-row:hover,
    .app-row.active {
        background: var(--surface-hover);
    }
    .app-name {
        color: var(--text-muted);
        font-size: 0.78rem;
        max-width: 100%;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .app-row.active .app-name,
    .app-row:hover .app-name {
        color: var(--text);
    }
    .app-meta {
        color: var(--text-muted);
        font-size: 0.62rem;
    }
    .app-origin {
        font-size: 0.62rem;
        color: var(--text-muted);
    }
    .app-origin.link:hover {
        color: var(--accent);
        text-decoration: underline;
    }
    .app-origin.gone {
        color: var(--warning);
    }
    .items {
        flex: 1;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        padding: 0 0.5rem;
    }
    .row {
        display: flex;
        align-items: center;
        border-radius: 6px;
    }
    .row:hover,
    .row.active {
        background: var(--surface-hover);
    }
    .row.active .item {
        color: var(--text);
        font-weight: 600;
    }
    .item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        flex: 1;
        min-width: 0;
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.82rem;
        padding: 0.4rem 0.5rem;
        cursor: pointer;
        text-align: left;
    }
    .row:hover .item {
        color: var(--text);
    }
    .fork {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.95rem;
        line-height: 1;
        padding: 0.25rem 0.2rem;
        border-radius: 5px;
        cursor: pointer;
        opacity: 0;
        transition: opacity 0.15s;
        flex-shrink: 0;
    }
    .row:hover .fork {
        opacity: 1;
    }
    .fork:hover {
        color: var(--accent);
    }
    .delete {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.85rem;
        line-height: 1;
        padding: 0.25rem 0.45rem;
        margin-right: 0.15rem;
        border-radius: 5px;
        cursor: pointer;
        opacity: 0;
        transition: opacity 0.15s;
        flex-shrink: 0;
    }
    .row:hover .delete,
    .delete.armed {
        opacity: 1;
    }
    .delete:hover {
        color: var(--error);
    }
    .delete.armed {
        color: var(--error);
        font-size: 0.68rem;
        font-weight: 600;
    }
    .name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .rename {
        width: 100%;
        min-width: 0;
        background: var(--input-bg);
        border: 1px solid var(--accent);
        border-radius: 5px;
        color: var(--text);
        font-family: inherit;
        font-size: 0.82rem;
        /* line up with the .item text it replaces */
        padding: 0.4rem 0.5rem;
        margin: 0 0.15rem;
        outline: none;
    }
    .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--border);
        flex-shrink: 0;
    }
    .dot.busy {
        background: var(--accent);
        animation: pulse 1.2s ease-in-out infinite;
    }
    .dot.unseen {
        background: var(--success);
    }
    .new {
        padding: 0.6rem;
        border-top: 1px solid var(--border);
    }
    .new-btn {
        width: 100%;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font-family: inherit;
        font-size: 0.78rem;
        padding: 0.4rem 0.55rem;
        cursor: pointer;
        text-align: left;
    }
    .new-btn:hover {
        border-color: var(--text-muted);
        background: var(--surface-hover);
    }
</style>
