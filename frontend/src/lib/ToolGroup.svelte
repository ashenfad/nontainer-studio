<script>
    // A run of work — tool calls with the model's thinking interleaved
    // between them — collapsed to one activity chip (the agex-studio
    // idiom: a calm transcript with drill-down on demand). Expanding
    // shows the think -> act -> think narrative in arrival order.
    import ToolStep from './ToolStep.svelte'
    import Markdown from './Markdown.svelte'

    let { entries, session } = $props()

    let open = $state(false)

    const tools = $derived(entries.filter((e) => e.kind === 'tool'))
    const running = $derived(tools.some((t) => t.running))
    const title = $derived.by(() => {
        const names = [...new Set(tools.map((t) => t.name))]
        const label = names.slice(0, 3).join(' · ') + (names.length > 3 ? ' · …' : '')
        return tools.length > 1 ? `${label} (${tools.length} steps)` : label
    })
</script>

<div class="activity" class:open>
    <button class="chip" onclick={() => (open = !open)}>
        <span class="dot" class:running></span>
        <span class="arrow">{open ? '▾' : '▸'}</span>
        {title}
    </button>
    {#if open}
        <div class="timeline">
            {#each entries as e, i (i)}
                {#if e.kind === 'tool'}
                    <ToolStep tool={e} {session} />
                {:else if e.kind === 'thinking'}
                    <div class="think-step">
                        <div class="think-label">✳ thinking</div>
                        <div class="think-text"><Markdown text={e.text} /></div>
                    </div>
                {/if}
            {/each}
        </div>
    {/if}
</div>

<style>
    .activity {
        margin: 0.35rem 0;
    }
    .chip {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 999px;
        color: var(--text-muted);
        font-size: 0.75rem;
        padding: 0.25rem 0.75rem;
        cursor: pointer;
    }
    .chip:hover {
        background: var(--surface-hover);
        color: var(--text);
    }
    .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--success);
        flex-shrink: 0;
    }
    .dot.running {
        background: var(--accent);
        animation: pulse 1.2s ease-in-out infinite;
    }
    .arrow {
        font-size: 0.6rem;
    }
    .think-label {
        font-size: 0.72rem;
        font-weight: 600;
        color: var(--purple);
        font-family: var(--font-mono);
    }
    .think-text {
        margin-top: 0.15rem;
        color: var(--text-muted);
        font-size: 0.78rem;
        line-height: 1.5;
        word-break: break-word;
        max-height: 260px;
        overflow-y: auto;
    }
    .think-text :global(.markdown) {
        font-size: inherit;
        color: inherit;
    }
    .think-text :global(.markdown p),
    .think-text :global(.markdown li) {
        margin: 0.25rem 0;
    }
    .think-text :global(.markdown h1),
    .think-text :global(.markdown h2),
    .think-text :global(.markdown h3),
    .think-text :global(.markdown h4) {
        font-size: 0.85rem;
        margin: 0.5rem 0 0.25rem;
        color: inherit;
    }
    .timeline {
        margin: 0.4rem 0 0.2rem 0.6rem;
        padding-left: 0.8rem;
        border-left: 2px solid var(--border);
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }
</style>
