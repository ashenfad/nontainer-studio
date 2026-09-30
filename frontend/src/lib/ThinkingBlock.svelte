<script>
    // The model's reasoning, as one quiet line: "Thinking…" while it
    // streams, open so it can be read as it arrives; "Thought for 12s"
    // once the model moves on, folded, click to reopen. Only rendered
    // when the model/provider exposes thinking. Models think in
    // markdown (lists, backticks, headers), so the text is rendered.
    import Markdown from './Markdown.svelte'
    import { duration } from './activity.js'

    let { item, live = false } = $props()
    let open = $state(null) // user override; null = follow `live`
    const show = $derived(open ?? live)
    const took = $derived(duration(item.ts, item.endTs))
</script>

<div class="think-block">
    <button
        class="act-line think-toggle"
        class:open={show}
        class:act-live={live}
        onclick={() => (open = !show)}
    >
        <span class="act-verb">{live ? 'Thinking' : 'Thought'}</span>
        {#if live}<span class="act-rest">…</span>{:else if took}<span class="act-rest"
                >{took}</span
            >{/if}
        <span class="act-chev">⌄</span>
    </button>
    {#if show}
        <div class="act-body think-text"><Markdown text={item.text} /></div>
    {/if}
</div>

<style>
    .think-text {
        color: var(--text-muted);
        font-size: 0.82rem;
        line-height: 1.5;
    }
</style>
