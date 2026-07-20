<script>
    // The shell: a projection of the foreground session's runtime.
    // Runtimes live in a module map and keep streaming while
    // backgrounded — switching sessions is just switching projections.
    import { api } from './lib/api.js'
    import {
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

    let active = $state(
        new URLSearchParams(location.search).get('session') || 'scratch',
    )
    let tab = $state('preview')
    let ready = $state(false)

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
        setForegroundName(name)
        const rt = getRuntime(name)
        rt.setForeground(true)
        ensureSession(name)
            .then(() => (ready = true))
            .catch(() => {})
        return () => rt.setForeground(false)
    })

    const rt = $derived(getRuntime(active))

    $effect(() => {
        refreshSessions()
        loadCatalog()
        const t = setInterval(refreshSessions, 4000)
        return () => clearInterval(t)
    })

    function switchTo(name) {
        history.replaceState(null, '', `?session=${encodeURIComponent(name)}`)
        active = name
    }

    async function deleteSession(name) {
        try {
            await api(`/api/sessions/${name}`, undefined, 'DELETE')
        } catch (e) {
            getRuntime(active).messages.push({ role: 'error', text: e.message })
            return
        }
        dropRuntime(name)
        await refreshSessions()
        if (name === active)
            switchTo(rail.sessions.find((s) => s.name !== name)?.name ?? 'scratch')
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
        <span class="session-name">{active}</span>
        <span class="grow"></span>
        {#if !rt.connected}
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
                onCreate={switchTo}
                onDelete={deleteSession}
            />
        {/if}
        {#if ready}
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
