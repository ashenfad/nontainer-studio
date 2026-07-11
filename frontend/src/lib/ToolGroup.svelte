<script>
    // A run of consecutive tool calls, collapsed to one activity chip
    // (the agex-studio idiom: a calm transcript with drill-down on
    // demand). Expands inline to the per-call timeline.
    import { fileUrl } from './api.js'

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
                <div class="step">
                    <div class="step-name">
                        {t.name}
                        {#if t.running}<span class="working">working…</span>{/if}
                    </div>
                    {#if t.args}<pre class="args">{t.args}</pre>{/if}
                    {#if t.result != null}<pre class="result">{t.result}</pre>{/if}
                    {#if t.images?.length}
                        <div class="step-images">
                            {#each t.images as p (p)}
                                <a
                                    href={fileUrl(session, p)}
                                    target="_blank"
                                    rel="noopener"
                                    title={p}
                                >
                                    <img
                                        class="step-img"
                                        src={fileUrl(session, p)}
                                        alt={p}
                                    />
                                </a>
                            {/each}
                        </div>
                    {/if}
                </div>
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
    .step-name {
        font-size: 0.72rem;
        font-weight: 600;
        color: var(--purple);
        display: flex;
        gap: 0.5rem;
        align-items: baseline;
    }
    .working {
        color: var(--accent);
        font-weight: 400;
        animation: pulse 1.2s ease-in-out infinite;
    }
    pre {
        font-size: 0.72rem;
        background: rgba(255, 255, 255, 0.05);
        border-radius: 6px;
        padding: 0.4rem 0.6rem;
        margin: 0.25rem 0 0;
        overflow-x: auto;
        max-height: 240px;
        overflow-y: auto;
        white-space: pre-wrap;
        word-break: break-word;
    }
    .args {
        color: var(--text-muted);
    }
    .step-images {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin-top: 0.3rem;
    }
    .step-img {
        max-width: 320px;
        max-height: 220px;
        border: 1px solid var(--border);
        border-radius: 6px;
        display: block;
    }
</style>
