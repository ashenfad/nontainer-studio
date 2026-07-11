<script>
    // Session list: server view overlaid with live runtime status.
    // Pulsing dot = a turn in flight; steady green = finished while
    // you were elsewhere (cleared on focus).
    import { rail, peekRuntime } from './runtime.svelte.js'

    let { active, onSwitch, onCreate } = $props()

    let name = $state('')

    function status(s) {
        // server busy is the truth for background sessions (they hold
        // no event stream); the foreground runtime is fresher between
        // rail polls
        const rt = peekRuntime(s.name)
        if (s.busy || (rt?.foreground && rt.busy)) return 'busy'
        if (rail.unseen.includes(s.name) || rt?.unseen) return 'unseen'
        return ''
    }

    function create() {
        const n = name.trim()
        if (!n) return
        name = ''
        onCreate(n)
    }
</script>

<nav class="rail">
    <div class="brand">nontainer<span>-studio</span></div>
    <div class="rail-title">sessions</div>
    <div class="items">
        {#each rail.sessions as s (s.name)}
            <button
                class="item"
                class:active={s.name === active}
                onclick={() => onSwitch(s.name)}
            >
                <span class="dot {status(s)}"></span>
                <span class="name">{s.name}</span>
            </button>
        {/each}
    </div>
    <form
        class="new"
        onsubmit={(e) => {
            e.preventDefault()
            create()
        }}
    >
        <input placeholder="new session…" bind:value={name} />
    </form>
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
    .item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.82rem;
        padding: 0.4rem 0.5rem;
        border-radius: 6px;
        cursor: pointer;
        text-align: left;
    }
    .item:hover {
        background: var(--surface-hover);
        color: var(--text);
    }
    .item.active {
        background: var(--surface-hover);
        color: var(--text);
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
    .new input {
        width: 100%;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font-family: inherit;
        font-size: 0.78rem;
        padding: 0.35rem 0.55rem;
        outline: none;
    }
    .new input:focus {
        border-color: var(--text-muted);
    }
</style>
