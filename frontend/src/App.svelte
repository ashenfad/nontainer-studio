<script>
    // The shell: a projection of the foreground session's runtime.
    // Runtimes live in a module map and keep streaming while
    // backgrounded — switching sessions is just switching projections.
    import {
        ensureSession,
        getRuntime,
        loadCatalog,
        peekRuntime,
        refreshSessions,
        rail,
    } from './lib/runtime.svelte.js'
    import SessionRail from './lib/SessionRail.svelte'
    import SplitPane from './lib/SplitPane.svelte'
    import MessageList from './lib/MessageList.svelte'
    import ChatInput from './lib/ChatInput.svelte'
    import Preview from './lib/Preview.svelte'
    import FilesTab from './lib/FilesTab.svelte'
    import HistoryTab from './lib/HistoryTab.svelte'

    let active = $state(
        new URLSearchParams(location.search).get('session') || 'scratch',
    )
    let tab = $state('preview')
    let ready = $state(false)

    // open (create-or-resume) the active session; flip foreground flags
    $effect(() => {
        const name = active
        ensureSession(name)
            .then(() => (ready = true))
            .catch(() => {})
        for (const s of rail.sessions) {
            const rt = peekRuntime(s.name)
            if (rt) rt.foreground = s.name === name
        }
        const rt = getRuntime(name)
        rt.foreground = true
        rt.unseen = false
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
        const prev = peekRuntime(active)
        if (prev) prev.foreground = false
        active = name
    }
</script>

<div class="shell">
    <SessionRail {active} onSwitch={switchTo} onCreate={switchTo} />
    {#if ready}
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
                        {#each ['preview', 'files', 'history'] as t (t)}
                            <button
                                class="tab"
                                class:active={tab === t}
                                onclick={() => (tab = t)}>{t}</button
                            >
                        {/each}
                        <span class="grow"></span>
                        {#if !rt.connected}
                            <span class="offline" title="event feed reconnecting…"
                                >⟳</span
                            >
                        {/if}
                    </div>
                    {#if tab === 'preview'}
                        <Preview {rt} />
                    {:else if tab === 'files'}
                        <FilesTab {rt} />
                    {:else}
                        <HistoryTab {rt} onFork={switchTo} />
                    {/if}
                </div>
            {/snippet}
        </SplitPane>
    {/if}
</div>

<style>
    .shell {
        display: flex;
        height: 100%;
    }
    .chat {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
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
