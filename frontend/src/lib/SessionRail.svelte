<script>
    // Session list: server view overlaid with live runtime status.
    // Pulsing dot = a turn in flight; steady green = finished while
    // you were elsewhere (cleared on focus).
    import { rail, peekRuntime } from './runtime.svelte.js'

    let { active, onSwitch, onCreate, onDelete } = $props()

    let armed = $state(null) // session name whose delete is one tap away

    function del(s, e) {
        e.stopPropagation()
        if (armed !== s.name) {
            armed = s.name // first tap arms; second confirms
            setTimeout(() => {
                if (armed === s.name) armed = null
            }, 3000)
            return
        }
        armed = null
        onDelete(s.name)
    }

    function status(s) {
        // server busy is the truth for background sessions (they hold
        // no event stream); the foreground runtime is fresher between
        // rail polls
        const rt = peekRuntime(s.name)
        if (s.busy || (rt?.foreground && rt.busy)) return 'busy'
        if (rail.unseen.includes(s.name) || rt?.unseen) return 'unseen'
        return ''
    }

</script>

<nav class="rail">
    <div class="brand">nontainer<span>-studio</span></div>
    <div class="rail-title">sessions</div>
    <div class="items">
        {#each rail.sessions as s (s.name)}
            <div class="row" class:active={s.name === active}>
                <button
                    class="item"
                    onclick={() => {
                        armed = null
                        onSwitch(s.name)
                    }}
                >
                    <span class="dot {status(s)}"></span>
                    <span class="name" title={s.title}>{s.title}</span>
                </button>
                <button
                    class="delete"
                    class:armed={armed === s.name}
                    title={armed === s.name
                        ? 'click again to delete everything this session owns'
                        : `delete ${s.title}`}
                    aria-label="delete {s.title}"
                    onclick={(e) => del(s, e)}
                >
                    {armed === s.name ? 'sure?' : '×'}
                </button>
            </div>
        {/each}
    </div>
    <div class="new">
        <button class="new-btn" onclick={() => onCreate()}>+ New session</button>
    </div>
</nav>

<style>
    .rail {
        width: 200px;
        flex-shrink: 0;
        border-right: 1px solid var(--border);
        background: var(--surface);
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }
    .brand {
        font-family: var(--font-display);
        font-variation-settings: 'opsz' 40, 'SOFT' 80;
        font-weight: 600;
        font-size: 1.05rem;
        padding: 0.9rem 1rem 0.7rem;
        border-bottom: 1px solid var(--border);
        color: var(--text);
    }
    .brand span {
        color: var(--accent);
    }
    .rail-title {
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--text-muted);
        padding: 0.8rem 1rem 0.3rem;
    }
    .items {
        flex: 1;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        padding: 0 0.5rem;
    }
    .row {
        display: flex;
        align-items: center;
        border-radius: 6px;
    }
    .row:hover,
    .row.active {
        background: var(--surface-hover);
    }
    .row.active .item {
        color: var(--text);
        font-weight: 600;
    }
    .item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        flex: 1;
        min-width: 0;
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.82rem;
        padding: 0.4rem 0.5rem;
        cursor: pointer;
        text-align: left;
    }
    .row:hover .item {
        color: var(--text);
    }
    .delete {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.85rem;
        line-height: 1;
        padding: 0.25rem 0.45rem;
        margin-right: 0.15rem;
        border-radius: 5px;
        cursor: pointer;
        opacity: 0;
        transition: opacity 0.15s;
        flex-shrink: 0;
    }
    .row:hover .delete,
    .delete.armed {
        opacity: 1;
    }
    .delete:hover {
        color: var(--error);
    }
    .delete.armed {
        color: var(--error);
        font-size: 0.68rem;
        font-weight: 600;
    }
    .name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--border);
        flex-shrink: 0;
    }
    .dot.busy {
        background: var(--accent);
        animation: pulse 1.2s ease-in-out infinite;
    }
    .dot.unseen {
        background: var(--success);
    }
    .new {
        padding: 0.6rem;
        border-top: 1px solid var(--border);
    }
    .new-btn {
        width: 100%;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font-family: inherit;
        font-size: 0.78rem;
        padding: 0.4rem 0.55rem;
        cursor: pointer;
        text-align: left;
    }
    .new-btn:hover {
        border-color: var(--text-muted);
        background: var(--surface-hover);
    }
</style>
