<script>
    // The transcript. User bubbles carry an inline `edit`: submit
    // rewinds files + agent memory to before that turn AND truncates
    // the visible transcript — the edited prompt runs as a fresh turn
    // from there. One mental model: everything below gets replaced.
    import AgentMessage from './AgentMessage.svelte'
    import { published } from './runtime.svelte.js'
    import { viewFile } from './viewer.svelte.js'

    let { rt } = $props()

    // The composer prepends "[attached: /a, /b]" for the AGENT's
    // benefit; humans get chips. Split it back out for display.
    function splitAttached(text) {
        const m = text.match(/^\[attached: ([^\]]+)\]\n?/)
        if (!m) return { files: [], body: text }
        return {
            files: m[1].split(', ').filter((p) => p.startsWith('/')),
            body: text.slice(m[0].length),
        }
    }

    let scroller = $state(null)
    let nearBottom = true

    // capture "was I near the bottom" BEFORE the DOM grows, restore after
    $effect.pre(() => {
        void rt.messages.length
        void rt.messages.at(-1)
        if (!scroller) return
        nearBottom =
            scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 150
    })
    $effect(() => {
        void rt.messages.length
        void rt.messages.at(-1)?.items?.length
        if (scroller && nearBottom) scroller.scrollTop = scroller.scrollHeight
    })

    let editing = $state(null) // index of the user message being edited
    let draft = $state('')

    function startEdit(i, body) {
        editing = i
        draft = body
    }

    async function submitEdit(msg, files) {
        let message = draft.trim()
        if (!message || rt.busy) return
        // re-prepend the attach chips (uploads happened before the
        // turn, so the rewind keeps the files themselves)
        if (files.length) message = `[attached: ${files.join(', ')}]\n${message}`
        editing = null
        await rt.edit(msg.seq, message)
    }

    function editKeydown(e, msg, files) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            submitEdit(msg, files)
        } else if (e.key === 'Escape') {
            editing = null
        }
    }

    function focusEnd(node) {
        node.focus()
        node.selectionStart = node.selectionEnd = node.value.length
    }

    // A publish marker is an anchor, like a user message: restoring to
    // one rewinds the files and the agent's memory to where that
    // version was tagged and cuts everything after it. Armed on the
    // first tap, like the rail's delete — it unsays turns.
    let armedRestore = $state(null)

    // What the marker's app is NOW. The marker itself is a history
    // fact — it stays in the log, unmutated, because the publish
    // happened — but it must not keep offering a link to an app that
    // was unpublished, or to a version that was deleted. Resolved
    // against the same store-wide list the rail reads, so an unpublish
    // from the modal or the rail retitles every marker for it at once.
    // Restoring stays available in every state: the commit is anchored
    // in this session's own history, not in the publication.
    function status(msg) {
        // before the first fetch, believe the marker: reading "removed"
        // for a beat on every load would be worse than a link that is
        // corrected a moment later
        if (!published.loaded) return { state: 'live', url: msg.url }
        const app = published.apps.find((a) => a.token === msg.token)
        if (!app) return { state: 'gone', text: 'since removed' }
        if (!app.versions.some((v) => v.name === msg.version))
            return { state: 'dropped', text: 'version removed' }
        return { state: 'live', url: app.url }
    }

    function restore(msg) {
        if (armedRestore !== msg.seq) {
            armedRestore = msg.seq
            setTimeout(() => armedRestore === msg.seq && (armedRestore = null), 3000)
            return
        }
        armedRestore = null
        rt.restoreTo(msg.seq)
    }
</script>

<div class="log" bind:this={scroller}>
    {#if rt.messages.length === 0}
        <div class="empty">
            <h2>nontainer-studio</h2>
            <p>
                Ask the agent to build something. It works in a versioned
                workspace — edit any earlier prompt to rewind and retry, or
                publish what it builds.
            </p>
        </div>
    {/if}
    {#each rt.messages as msg, i (i)}
        {#if msg.role === 'user'}
            {@const parts = splitAttached(msg.text)}
            {#if editing === i}
                <div class="edit-box">
                    <textarea
                        bind:value={draft}
                        rows={Math.min(8, draft.split('\n').length + 1)}
                        onkeydown={(e) => editKeydown(e, msg, parts.files)}
                        use:focusEnd
                    ></textarea>
                    <div class="edit-actions">
                        <span class="edit-hint">replaces this turn and everything after</span>
                        <button onclick={() => (editing = null)}>cancel</button>
                        <button
                            class="send"
                            disabled={!draft.trim()}
                            onclick={() => submitEdit(msg, parts.files)}>send</button
                        >
                    </div>
                </div>
            {:else}
            <div class="user-row">
                <div class="user-bubble">
                    {#if parts.files.length}
                        <div class="attach-chips">
                            {#each parts.files as p (p)}
                                <button
                                    class="attach-chip"
                                    title={p}
                                    onclick={() => viewFile(p)}
                                >
                                    <svg
                                        width="11"
                                        height="11"
                                        viewBox="0 0 24 24"
                                        fill="none"
                                        stroke="currentColor"
                                        stroke-width="2"
                                        stroke-linecap="round"
                                        stroke-linejoin="round"
                                        aria-hidden="true"
                                    >
                                        <path
                                            d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"
                                        ></path>
                                    </svg>
                                    {p.split('/').pop()}
                                </button>
                            {/each}
                        </div>
                    {/if}
                    {parts.body}
                </div>
                {#if msg.head && msg.seq != null && !rt.busy}
                    <button
                        class="edit"
                        title="edit this prompt — rewinds and replaces this turn and everything after"
                        onclick={() => startEdit(i, parts.body)}>edit</button
                    >
                {/if}
            </div>
            {/if}
        {:else if msg.role === 'agent'}
            <AgentMessage {msg} session={rt.name} />
        {:else if msg.role === 'publish'}
            {@const now = status(msg)}
            <div class="publish" class:removed={now.state !== 'live'}>
                <span>published <strong>{msg.version}</strong></span>
                {#if now.state === 'live'}
                    <a href={now.url} target="_blank" rel="noopener">open ↗</a>
                {:else}
                    <span class="removed-note">{now.text}</span>
                {/if}
                {#if msg.seq != null && !rt.busy}
                    <button
                        class="restore"
                        class:armed={armedRestore === msg.seq}
                        title="rewind the files and the agent's memory to this publish, dropping everything after it"
                        onclick={() => restore(msg)}
                        >{armedRestore === msg.seq
                            ? 'really restore'
                            : 'restore to this publish'}</button
                    >
                {/if}
            </div>
        {:else if msg.role === 'notice'}
            <div class="notice">{msg.text}</div>
        {:else if msg.role === 'error'}
            <div class="error">{msg.text}</div>
        {/if}
    {/each}
    {#if rt.busy && rt.messages.at(-1)?.role === 'user'}
        <div class="thinking"><span class="pulse-dot"></span></div>
    {/if}
</div>

<style>
    .log {
        flex: 1;
        overflow-y: auto;
        padding: 1rem 1.2rem;
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
    }
    .empty {
        margin: auto;
        text-align: center;
        color: var(--text-muted);
        max-width: 420px;
    }
    .empty h2 {
        font-size: 1.6rem;
        margin-bottom: 0.5rem;
        color: var(--text);
        font-variation-settings: 'opsz' 72, 'SOFT' 80;
    }
    .empty p {
        font-size: 0.85rem;
        line-height: 1.5;
    }
    .user-row {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 0.5rem;
        margin: 0.5rem 0 0.25rem;
    }
    .user-row .edit {
        opacity: 0;
        transition: opacity 0.15s;
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.68rem;
        padding: 0.15rem 0.45rem;
        cursor: pointer;
        order: -1;
    }
    .user-row:hover .edit {
        opacity: 1;
    }
    .user-row .edit:hover {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .edit-box {
        align-self: flex-end;
        width: min(80%, 560px);
        background: var(--user-bubble);
        border: 1px solid var(--border);
        border-radius: 12px 12px 4px 12px;
        padding: 0.5rem 0.6rem;
        margin: 0.5rem 0 0.25rem;
    }
    .edit-box textarea {
        width: 100%;
        background: none;
        border: none;
        outline: none;
        resize: vertical;
        color: var(--text);
        font: inherit;
        font-size: 0.88rem;
        line-height: 1.4;
    }
    .edit-actions {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding-top: 0.35rem;
    }
    .edit-hint {
        margin-right: auto;
        color: var(--text-muted);
        font-size: 0.68rem;
    }
    .edit-actions button {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.72rem;
        padding: 0.2rem 0.6rem;
        cursor: pointer;
    }
    .edit-actions button:hover {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .edit-actions button.send {
        color: var(--accent);
        border-color: color-mix(in srgb, var(--accent) 50%, transparent);
    }
    .edit-actions button.send:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .user-bubble {
        background: var(--user-bubble);
        border-radius: 12px 12px 4px 12px;
        padding: 0.55rem 0.85rem;
        font-size: 0.88rem;
        max-width: 80%;
        white-space: pre-wrap;
        word-break: break-word;
    }
    .attach-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.3rem;
        padding-bottom: 0.4rem;
    }
    .attach-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        background: rgba(255, 255, 255, 0.08);
        border: 1px solid rgba(255, 255, 255, 0.14);
        border-radius: 999px;
        color: var(--text);
        font-size: 0.7rem;
        padding: 0.14rem 0.55rem;
        cursor: pointer;
        max-width: 240px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .attach-chip:hover {
        background: rgba(255, 255, 255, 0.16);
    }
    .attach-chip svg {
        flex-shrink: 0;
        opacity: 0.7;
    }
    .notice {
        align-self: center;
        color: var(--text-muted);
        font-size: 0.72rem;
        background: rgba(255, 255, 255, 0.04);
        border-radius: 999px;
        padding: 0.2rem 0.8rem;
        margin: 0.3rem 0;
    }
    .publish {
        align-self: center;
        display: flex;
        align-items: center;
        gap: 0.5rem;
        color: var(--text-muted);
        font-size: 0.72rem;
        background: color-mix(in srgb, var(--success) 10%, transparent);
        border: 1px solid color-mix(in srgb, var(--success) 35%, transparent);
        border-radius: 999px;
        padding: 0.2rem 0.8rem;
        margin: 0.3rem 0;
    }
    .publish strong {
        color: var(--text);
    }
    /* Same weight as a live one: this publish HAPPENED, and dimming it
       would read as a lesser event rather than a removed app. Only the
       colour moves, from "there is something to open" to "there isn't". */
    .publish.removed {
        background: rgba(255, 255, 255, 0.04);
        border-color: var(--border);
    }
    .removed-note {
        color: var(--text-muted);
        font-style: italic;
    }
    .publish a {
        color: var(--success);
        text-decoration: none;
    }
    .publish a:hover {
        text-decoration: underline;
    }
    .publish .restore {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 999px;
        font-size: 0.66rem;
        padding: 0.02rem 0.45rem;
        cursor: pointer;
    }
    .publish .restore:hover,
    .publish .restore.armed {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .error {
        color: var(--error);
        font-size: 0.8rem;
        background: color-mix(in srgb, var(--error) 12%, transparent);
        border: 1px solid color-mix(in srgb, var(--error) 40%, transparent);
        border-radius: 8px;
        padding: 0.45rem 0.7rem;
        margin: 0.3rem 0;
    }
    .thinking {
        padding: 0.4rem 0.2rem;
    }
    .pulse-dot {
        display: inline-block;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: var(--accent);
        animation: pulse 1.2s ease-in-out infinite;
    }
</style>
