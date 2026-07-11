<script>
    // The transcript. User bubbles carry the inline undo (restore to
    // the pre-turn head — files AND agent memory rewind together;
    // the transcript itself keeps its record, so undone turns stay
    // visible above the restore notice).
    import AgentMessage from './AgentMessage.svelte'
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

    async function undo(msg) {
        if (!msg.head || rt.busy) return
        await rt.restore(msg.head)
    }
</script>

<div class="log" bind:this={scroller}>
    {#if rt.messages.length === 0}
        <div class="empty">
            <h2>nontainer-studio</h2>
            <p>
                Ask the agent to build something. It works in a versioned
                workspace — every turn is a checkpoint you can rewind, fork,
                or publish.
            </p>
        </div>
    {/if}
    {#each rt.messages as msg, i (i)}
        {#if msg.role === 'user'}
            {@const parts = splitAttached(msg.text)}
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
                {#if msg.head && !rt.busy}
                    <button
                        class="undo"
                        title="rewind files + agent memory to before this turn"
                        onclick={() => undo(msg)}>undo</button
                    >
                {/if}
            </div>
        {:else if msg.role === 'agent'}
            <AgentMessage {msg} session={rt.name} />
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
    .user-row .undo {
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
    .user-row:hover .undo {
        opacity: 1;
    }
    .user-row .undo:hover {
        color: var(--text);
        border-color: var(--text-muted);
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
