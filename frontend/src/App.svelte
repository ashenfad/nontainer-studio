<script>
    // The shell: a projection of the foreground session's runtime.
    // Runtimes live in a module map and keep streaming while
    // backgrounded — switching sessions is just switching projections.
    import { api } from './lib/api.js'
    import {
        createSession,
        dropRuntime,
        ensureSession,
        getRuntime,
        loadCatalog,
        rail,
        refreshSessions,
        setForegroundName,
    } from './lib/runtime.svelte.js'
    import SessionRail from './lib/SessionRail.svelte'
    import FileModal from './lib/FileModal.svelte'
    import SplitPane from './lib/SplitPane.svelte'
    import MessageList from './lib/MessageList.svelte'
    import ChatInput from './lib/ChatInput.svelte'
    import Preview from './lib/Preview.svelte'
    import FilesTab from './lib/FilesTab.svelte'

    // null until the bootstrap picks one: session names are minted
    // slugs now, so there is no well-known name to default to
    let active = $state(new URLSearchParams(location.search).get('session'))
    let tab = $state('preview')
    let ready = $state(false)
    // Set when the shell has no session to show and couldn't mint one.
    // Distinct from a per-session error: there's no message list to put
    // it in, so it renders as the body.
    let bootstrapError = $state(null)

    // layout prefs: per-browser, survive reloads, no server involvement
    let showRail = $state(localStorage.getItem('nts.rail') !== '0')
    let showSide = $state(localStorage.getItem('nts.side') !== '0')
    $effect(() => localStorage.setItem('nts.rail', showRail ? '1' : '0'))
    $effect(() => localStorage.setItem('nts.side', showSide ? '1' : '0'))

    function onKeydown(e) {
        if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return
        if (e.key === 'b') {
            e.preventDefault()
            showRail = !showRail
        } else if (e.key === 'j') {
            e.preventDefault()
            showSide = !showSide
        }
    }

    // open (create-or-resume) the active session; only the foreground
    // session streams (cleanup backgrounds the previous one)
    $effect(() => {
        const name = active
        if (!name) return
        setForegroundName(name)
        const rt = getRuntime(name)
        rt.setForeground(true)
        ensureSession(name)
            .then(() => (ready = true))
            // ready stays false on failure, so the chat pane never
            // appears — say why instead of showing an empty shell
            // forever (a stale ?session= in the URL lands here).
            .catch((e) => (bootstrapError = e.message))
        return () => rt.setForeground(false)
    })

    const rt = $derived(active ? getRuntime(active) : null)
    // the rail row is the title's source of truth (the server resolves
    // user > agent > default); the slug never shows
    const title = $derived(rail.sessions.find((s) => s.name === active)?.title ?? '')

    $effect(() => {
        refreshSessions()
        loadCatalog()
        const t = setInterval(refreshSessions, 4000)
        return () => clearInterval(t)
    })

    // Bootstrap: no ?session= means adopt the newest session, or mint the
    // very first one on a fresh install. Runs once — setting `active`
    // re-enters and returns early.
    $effect(() => {
        if (active) return
        ;(async () => {
            try {
                bootstrapError = null
                await refreshSessions()
                switchTo(rail.sessions[0]?.name ?? (await createSession()))
            } catch (e) {
                // Nothing to report INTO yet — no session means no
                // message list — so the shell itself has to say it.
                // Silence here reads as a broken build, not a down
                // server. (refreshSessions swallows its own errors; a
                // throw here is createSession.)
                bootstrapError = e.message
            }
        })()
    })

    function switchTo(name) {
        history.replaceState(null, '', `?session=${encodeURIComponent(name)}`)
        active = name
    }

    async function createAndSwitch() {
        switchTo(await createSession())
    }

    async function deleteSession(name) {
        try {
            await api(`/api/sessions/${name}`, undefined, 'DELETE')
        } catch (e) {
            getRuntime(active)?.messages.push({ role: 'error', text: e.message })
            return
        }
        dropRuntime(name)
        await refreshSessions()
        // deleting the last session leaves nothing to fall back to: mint
        // one rather than strand the shell with no active session
        if (name === active) {
            try {
                switchTo(rail.sessions[0]?.name ?? (await createSession()))
            } catch (e) {
                // The delete succeeded and the fallback didn't, so there
                // is no active session left to report into — the same
                // stranded shell the bootstrap handles.
                bootstrapError = e.message
            }
        }
    }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="shell">
    <header class="topbar">
        <button
            class="chrome-btn"
            class:on={showRail}
            title="toggle session drawer (⌘B)"
            aria-label="toggle session drawer"
            onclick={() => (showRail = !showRail)}>☰</button
        >
        <span class="session-name">{title}</span>
        <span class="grow"></span>
        {#if rt && !rt.connected}
            <span class="offline" title="event feed reconnecting…">⟳</span>
        {/if}
        <button
            class="chrome-btn"
            class:on={showSide}
            title="toggle preview panel (⌘J)"
            aria-label="toggle preview panel"
            onclick={() => (showSide = !showSide)}>◨</button
        >
    </header>
    <div class="body">
        {#if showRail}
            <SessionRail
                {active}
                onSwitch={switchTo}
                onCreate={createAndSwitch}
                onDelete={deleteSession}
            />
        {/if}
        {#if bootstrapError}
            <div class="bootstrap-error">
                <p class="what">Couldn't open a session.</p>
                <p class="why">{bootstrapError}</p>
                <p class="hint">
                    The server may have stopped — check the terminal you
                    launched it from.
                </p>
                <button class="retry" onclick={() => location.reload()}>
                    retry
                </button>
            </div>
        {:else if ready}
            {#if showSide}
                <SplitPane>
                    {#snippet left()}
                        <div class="chat">
                            <MessageList {rt} />
                            <ChatInput {rt} />
                        </div>
                    {/snippet}
                    {#snippet right()}
                        <div class="side">
                            <div class="tabs">
                                {#each ['preview', 'files'] as t (t)}
                                    <button
                                        class="tab"
                                        class:active={tab === t}
                                        onclick={() => (tab = t)}>{t}</button
                                    >
                                {/each}
                            </div>
                            {#if tab === 'preview'}
                                <Preview {rt} />
                            {:else}
                                <FilesTab {rt} />
                            {/if}
                        </div>
                    {/snippet}
                </SplitPane>
            {:else}
                <!-- full-width mode: cap the column so lines stay readable -->
                <div class="chat solo">
                    <MessageList {rt} />
                    <ChatInput {rt} />
                </div>
            {/if}
        {/if}
    </div>
    <!-- one viewer for every surface that mentions a workspace path -->
    <FileModal session={active} />
</div>

<style>
    .shell {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    .topbar {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        padding: 0.3rem 0.6rem;
        border-bottom: 1px solid var(--border);
        background: var(--surface);
        flex: none;
    }
    .chrome-btn {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.95rem;
        line-height: 1;
        padding: 0.3rem 0.45rem;
        border-radius: 6px;
        cursor: pointer;
    }
    .chrome-btn:hover {
        color: var(--text);
        background: var(--surface-hover);
    }
    .chrome-btn.on {
        color: var(--text);
    }
    .session-name {
        font-family: var(--font-display);
        font-size: 0.9rem;
        color: var(--text);
    }
    .body {
        display: flex;
        flex: 1;
        min-height: 0;
    }
    .bootstrap-error {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
        padding: 2rem;
        text-align: center;
    }
    .bootstrap-error .what {
        color: var(--text);
        font-weight: 600;
    }
    .bootstrap-error .why {
        color: var(--error);
        font-family: ui-monospace, monospace;
        font-size: 0.9em;
    }
    .bootstrap-error .hint {
        color: var(--text-muted);
        font-size: 0.9em;
    }
    .bootstrap-error .retry {
        margin-top: 0.5rem;
        padding: 0.35rem 1rem;
        color: var(--text);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 6px;
        cursor: pointer;
    }
    .bootstrap-error .retry:hover {
        background: var(--surface-hover);
    }

    .chat {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
    }
    .chat.solo {
        max-width: 900px;
        width: 100%;
        margin: 0 auto;
    }
    .side {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
        border-left: none;
    }
    .tabs {
        display: flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.4rem 0.6rem;
        border-bottom: 1px solid var(--border);
        background: var(--surface);
    }
    .tab {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.76rem;
        padding: 0.25rem 0.7rem;
        border-radius: 6px;
        cursor: pointer;
        text-transform: lowercase;
    }
    .tab:hover {
        color: var(--text);
        background: var(--surface-hover);
    }
    .tab.active {
        color: var(--text);
        background: var(--surface-hover);
        font-weight: 600;
    }
    .grow {
        flex: 1;
    }
    .offline {
        color: var(--warning);
        font-size: 0.8rem;
        animation: spin 1.5s linear infinite;
        display: inline-block;
    }
</style>
