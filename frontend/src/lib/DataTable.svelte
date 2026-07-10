<script>
    // Renders a *.table.json artifact: {columns, data, total}.
    let { url } = $props()

    let table = $state(null)
    let failed = $state(null)
    let sortCol = $state(null)
    let sortDir = $state(1)

    $effect(() => {
        let dead = false
        fetch(url)
            .then((r) => r.json())
            .then((t) => !dead && (table = t))
            .catch((e) => !dead && (failed = e.message))
        return () => (dead = true)
    })

    const rows = $derived.by(() => {
        if (!table) return []
        if (sortCol === null) return table.data
        const i = sortCol
        return [...table.data].sort((a, b) => {
            const x = a[i]
            const y = b[i]
            if (typeof x === 'number' && typeof y === 'number')
                return (x - y) * sortDir
            return String(x).localeCompare(String(y)) * sortDir
        })
    })

    function sortBy(i) {
        if (sortCol === i) sortDir = -sortDir
        else {
            sortCol = i
            sortDir = 1
        }
    }
</script>

{#if failed}
    <div class="table-error">table failed: {failed}</div>
{:else if !table}
    <div class="table-loading">…</div>
{:else}
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    {#each table.columns as c, i (i)}
                        <th onclick={() => sortBy(i)}>
                            {c}{sortCol === i ? (sortDir > 0 ? ' ▲' : ' ▼') : ''}
                        </th>
                    {/each}
                </tr>
            </thead>
            <tbody>
                {#each rows as row, ri (ri)}
                    <tr>
                        {#each row as v, vi (vi)}
                            <td>{String(v)}</td>
                        {/each}
                    </tr>
                {/each}
            </tbody>
        </table>
    </div>
    {#if table.total > table.data.length}
        <div class="table-note">
            showing {table.data.length} of {table.total} rows
        </div>
    {/if}
{/if}

<style>
    .table-wrap {
        max-height: 340px;
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: 8px;
        margin: 0.5rem 0;
    }
    table {
        border-collapse: collapse;
        width: 100%;
        font-size: 0.8rem;
    }
    th,
    td {
        padding: 0.3em 0.6em;
        text-align: left;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        white-space: nowrap;
    }
    th {
        position: sticky;
        top: 0;
        background: var(--surface);
        cursor: pointer;
        user-select: none;
        font-weight: 600;
    }
    th:hover {
        background: var(--surface-hover);
    }
    tr:nth-child(even) td {
        background: rgba(255, 255, 255, 0.02);
    }
    .table-note,
    .table-loading {
        font-size: 0.72rem;
        color: var(--text-muted);
        padding: 0.15rem 0.2rem 0.4rem;
    }
    .table-error {
        color: var(--error);
        font-size: 0.8rem;
    }
</style>
