<script>
    // Renders a *.cards.json artifact: {items: [{label, value, delta?, unit?}]}
    // as a wrapping KPI row of stat tiles (the dataviz stat-tile contract:
    // muted label, prominent proportional-figure value + unit, signed delta
    // colored by direction). Fetch-and-render mirrors DataTable.svelte.
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

    // Delta sign drives the accent: up reads good (success), down bad (error),
    // zero/unparseable stays neutral ink. parseFloat copes with both raw
    // numbers and pre-signed strings ("+3.2%", "-4").
    function deltaSign(d) {
        const n = typeof d === 'number' ? d : parseFloat(d)
        if (!Number.isFinite(n) || n === 0) return 'flat'
        return n > 0 ? 'up' : 'down'
    }

    // Faithful to the agent's formatting; only supply a leading "+" for a
    // bare positive number (negatives already carry their sign).
    function fmtDelta(d) {
        if (typeof d === 'number') return (d > 0 ? '+' : '') + d.toLocaleString()
        return String(d)
    }
</script>

{#if failed}
    <div class="cards-error">cards failed: {failed}</div>
{:else if items.length}
    <div class="cards">
        {#each items as it, i (i)}
            <div class="tile">
                <div class="label">{it.label}</div>
                <div class="value">
                    {fmtValue(it.value)}{#if it.unit}<span class="unit"
                            >{it.unit}</span
                        >{/if}
                </div>
                {#if it.delta !== undefined && it.delta !== null && it.delta !== ''}
                    <div class="delta {deltaSign(it.delta)}">
                        {fmtDelta(it.delta)}
                    </div>
                {/if}
            </div>
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
    .unit {
        font-size: 0.8rem;
        font-weight: 400;
        color: var(--text-muted);
        margin-left: 0.25em;
    }
    .delta {
        font-size: 0.78rem;
        font-weight: 600;
    }
    .delta.up {
        color: var(--success);
    }
    .delta.down {
        color: var(--error);
    }
    .delta.flat {
        color: var(--text-muted);
    }
    .cards-error {
        color: var(--error);
        font-size: 0.8rem;
    }
</style>
