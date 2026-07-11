<script>
    // A run of consecutive tool calls, collapsed to one activity chip
    // (the agex-studio idiom: a calm transcript with drill-down on
    // demand). Expands inline; each call renders by type (ToolStep).
    import ToolStep from './ToolStep.svelte'

    let { tools, session } = $props()

    let open = $state(false)

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
            {#each tools as t, i (i)}
                <ToolStep tool={t} {session} />
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
    .timeline {
        margin: 0.4rem 0 0.2rem 0.6rem;
        padding-left: 0.8rem;
        border-left: 2px solid var(--border);
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }
</style>
