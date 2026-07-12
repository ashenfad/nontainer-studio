<script>
    // Checkpoint rail: the power view of time travel. Restore rewinds
    // files + cache + agent memory together; fork-from-here spins a
    // new session (new universe: branched files, copied db, fresh chat).
    import { api } from './api.js'

    let { rt, onFork } = $props()

    let entries = $state([])
    let head = $state(null)
    let error = $state(null)
    let forking = $state(null) // checkpoint id awaiting a name
    let forkName = $state('')

    $effect(() => {
        void rt.version
        void rt.connected // refetch when SSE reconnects (server restart)
        const session = rt.name
        let stale = false // guard the async race across session switches
        api(`/api/sessions/${session}/history`)
            .then((d) => {
                if (stale) return
                entries = d.history
                head = d.head
                error = null
            })
            .catch((e) => {
                if (stale) return
                entries = [] // never show another session's history as current
                error = e.message
            })
        return () => (stale = true)
    })

    async function restore(id) {
        error = null
        await rt.restore(id)
    }

    async function fork(id) {
        const name = forkName.trim()
        if (!name) return
        try {
            await api(`/api/sessions/${rt.name}/fork`, { name, checkpoint: id })
            forking = null
            forkName = ''
            onFork(name)
        } catch (e) {
            error = e.message
        }
    }

    function when(t) {
        if (!t) return ''
        const d = new Date(t * 1000)
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    }

    function label(info) {
        return info.tool || (info.published ? 'published' : '') || info.target || ''
    }
</script>

<div class="history">
    <div class="hint">
        restore rewinds files, cache & the agent's memory together — the app's
        <code>db</code> and this transcript keep their history
    </div>
    {#if error}<div class="error">{error}</div>{/if}
    {#each entries as c (c.id)}
        <div class="commit" class:head={c.id === head}>
            <code class="id">{c.id.slice(0, 8)}</code>
            <span class="label">{label(c.info)}</span>
            <span class="time">{when(c.time)}</span>
            {#if c.id === head}
                <span class="head-tag">head</span>
            {:else}
                <button class="small" disabled={rt.busy} onclick={() => restore(c.id)}
                    >restore</button
                >
            {/if}
            <button
                class="small"
                onclick={() => {
                    forking = forking === c.id ? null : c.id
                    forkName = ''
                }}>fork</button
            >
        </div>
        {#if forking === c.id}
            <form
                class="fork-row"
                onsubmit={(e) => {
                    e.preventDefault()
                    fork(c.id)
                }}
            >
                <input placeholder="new session name…" bind:value={forkName} />
                <button class="small" type="submit">create</button>
            </form>
        {/if}
    {/each}
</div>

<style>
    .history {
        flex: 1;
        overflow-y: auto;
        padding: 0.6rem 0.8rem;
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
    }
    .hint {
        color: var(--text-muted);
        font-size: 0.72rem;
        line-height: 1.4;
        padding-bottom: 0.5rem;
    }
    .hint code {
        background: rgba(255, 255, 255, 0.08);
        padding: 0 0.25rem;
        border-radius: 3px;
    }
    .error {
        color: var(--error);
        font-size: 0.75rem;
        padding: 0.3rem 0;
    }
    .commit {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        padding: 0.3rem 0.45rem;
        border-radius: 6px;
        font-size: 0.76rem;
    }
    .commit:hover {
        background: var(--surface-hover);
    }
    .commit.head {
        background: color-mix(in srgb, var(--accent) 8%, transparent);
    }
    .id {
        color: var(--purple);
        font-size: 0.72rem;
    }
    .label {
        flex: 1;
        color: var(--text-muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .time {
        color: var(--text-muted);
        font-size: 0.68rem;
    }
    .head-tag {
        color: var(--accent);
        font-size: 0.66rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .small {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 5px;
        font-size: 0.68rem;
        padding: 0.12rem 0.45rem;
        cursor: pointer;
    }
    .small:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .small:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .fork-row {
        display: flex;
        gap: 0.4rem;
        padding: 0.2rem 0.4rem 0.4rem 2rem;
    }
    .fork-row input {
        flex: 1;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 5px;
        color: var(--text);
        font-size: 0.74rem;
        padding: 0.25rem 0.5rem;
        outline: none;
    }
</style>
