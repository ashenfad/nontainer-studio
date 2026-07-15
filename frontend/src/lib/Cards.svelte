<script>
    // Renders a *.cards.json artifact: {items: [{type, ...}]} as a
    // wrapping row of cards. Each item is dispatched by its `type`
    // (agex-studio's CardRow model):
    //   - stat:    {label, value, sublabel?}  → muted label, hero value
    //   - callout: {title, body, tone?}       → tone-tinted icon + markdown
    // Unknown types are silently skipped. There is deliberately NO sign
    // inference: the renderer never colors a number by direction — tone
    // lives only on callouts, and a stat's story lives in its sublabel.
    // Fetch-and-render mirrors DataTable.svelte.
    import { renderMarkdown } from './markdown.js'

    let { url } = $props()

    let cards = $state(null)
    let failed = $state(null)

    $effect(() => {
        let dead = false
        fetch(url)
            .then((r) => r.json())
            .then((c) => !dead && (cards = c))
            .catch((e) => !dead && (failed = e.message))
        return () => (dead = true)
    })

    // Only the well-formed shape renders; a missing/empty items list is a
    // near-miss materialization we degrade to nothing rather than an error box.
    const items = $derived(Array.isArray(cards?.items) ? cards.items : [])

    // Big standalone values get thousands-commas but keep proportional
    // figures (no tabular-nums) — agent-supplied strings pass through as-is.
    function fmtValue(v) {
        return typeof v === 'number' ? v.toLocaleString() : String(v ?? '')
    }
</script>

{#if failed}
    <div class="cards-error">cards failed: {failed}</div>
{:else if items.length}
    <div class="cards">
        {#each items as it, i (i)}
            {#if it.type === 'callout'}
                <!-- Tone tints only the icon; the card body / border stay
                     neutral so a row of mixed-tone callouts reads as a
                     uniform set of cards rather than a noisy traffic light. -->
                <div
                    class="callout"
                    class:tone-success={it.tone === 'success'}
                    class:tone-warning={it.tone === 'warning'}
                >
                    <div class="callout-header">
                        <span class="callout-icon" aria-hidden="true">
                            {#if it.tone === 'success'}
                                <!-- shield/check -->
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
                                    <polyline points="9 12 11 14 15 10"></polyline>
                                </svg>
                            {:else if it.tone === 'warning'}
                                <!-- triangle/exclamation -->
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
                                    <line x1="12" y1="9" x2="12" y2="13"></line>
                                    <line x1="12" y1="17" x2="12.01" y2="17"></line>
                                </svg>
                            {:else}
                                <!-- info circle -->
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <circle cx="12" cy="12" r="10"></circle>
                                    <line x1="12" y1="16" x2="12" y2="12"></line>
                                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                                </svg>
                            {/if}
                        </span>
                        <h4 class="callout-title">{it.title ?? ''}</h4>
                    </div>
                    {#if it.body}
                        <!-- markdown body: renderMarkdown is already DOMPurify'd -->
                        <div class="callout-body markdown">{@html renderMarkdown(it.body)}</div>
                    {/if}
                </div>
            {:else if it.type === 'stat'}
                <div class="tile">
                    <div class="label">{it.label}</div>
                    <div class="value">{fmtValue(it.value)}</div>
                    {#if it.sublabel}
                        <div class="sublabel">{it.sublabel}</div>
                    {/if}
                </div>
            {/if}
        {/each}
    </div>
{/if}

<style>
    .cards {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem;
        margin: 0.5rem 0;
    }

    /* stat tile */
    .tile {
        flex: 1 1 130px;
        min-width: 130px;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 0.6rem 0.75rem;
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
    }
    .label {
        font-size: 0.72rem;
        color: var(--text-muted);
        line-height: 1.2;
    }
    .value {
        /* body sans (never the display serif) at semibold, proportional
           figures — the stat-tile hero number */
        font-family: var(--font-body);
        font-weight: 600;
        font-size: 1.5rem;
        line-height: 1.1;
        color: var(--text);
    }
    .sublabel {
        font-size: 0.72rem;
        color: var(--text-muted);
        margin-top: 0.1rem;
    }

    /* callout card */
    .callout {
        flex: 1 1 200px;
        min-width: 180px;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 0.85rem 0.9rem;
        display: flex;
        flex-direction: column;
        gap: 0.55rem;
    }
    .callout-header {
        display: flex;
        align-items: center;
        gap: 0.45rem;
    }
    .callout-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: #7cb7ff; /* info default */
        flex-shrink: 0;
    }
    .callout.tone-success .callout-icon {
        color: var(--success);
    }
    .callout.tone-warning .callout-icon {
        color: var(--warning);
    }
    .callout-title {
        font-size: 0.92rem;
        font-weight: 600;
        color: var(--text);
        margin: 0;
        line-height: 1.25;
    }

    /* Card-sized markdown — tighter rhythm than the chat-bubble defaults
       from app.css's `.markdown` rules. Overrides only the spacing;
       inline styling (code, bold, links) inherits the shared rules. */
    .callout-body {
        font-size: 0.82rem;
        color: var(--text);
        line-height: 1.45;
    }
    .callout-body :global(p) { margin: 0 0 0.35em; }
    .callout-body :global(p:first-child) { margin-top: 0; }
    .callout-body :global(p:last-child) { margin-bottom: 0; }
    .callout-body :global(ul),
    .callout-body :global(ol) { margin: 0.3em 0; padding-left: 1.2em; }
    .callout-body :global(li) { margin: 0.1em 0; }
    .callout-body :global(code) { font-size: 0.85em; }
    .callout-body :global(pre) { margin: 0.3em 0; font-size: 0.78em; }

    .cards-error {
        color: var(--error);
        font-size: 0.8rem;
    }
</style>
