<script>
    // Native model reasoning: expanded while it streams (live), folds
    // to a chip once the reply proper starts; click to reopen. Only
    // rendered at all when the model/provider exposes thinking.
    let { item, live } = $props()
    let open = $state(null) // user override; null = follow `live`
    const show = $derived(open ?? live)
</script>

<div class="think-block">
    <button class="think-toggle" onclick={() => (open = !show)}>
        <span class="spark" class:pulsing={live}>✳</span>
        thinking{live ? '…' : ''}
    </button>
    {#if show}
        <div class="think-text">{item.text}</div>
    {/if}
</div>

<style>
    .think-block {
        margin: 0.35rem 0;
    }
    .think-toggle {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        background: none;
        border: none;
        padding: 0.1rem 0;
        color: var(--text-muted);
        font-size: 0.72rem;
        font-family: var(--font-mono);
        cursor: pointer;
    }
    .think-toggle:hover {
        color: var(--text);
    }
    .spark {
        color: var(--purple);
    }
    .spark.pulsing {
        animation: pulse 1.2s ease-in-out infinite;
    }
    .think-text {
        margin-top: 0.25rem;
        padding: 0.4rem 0.7rem;
        border-left: 2px solid var(--border);
        color: var(--text-muted);
        font-size: 0.78rem;
        line-height: 1.5;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 320px;
        overflow-y: auto;
    }
</style>
