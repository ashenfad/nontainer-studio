<script>
    // The session's delegates while they are out: one chip each, from the
    // moment one is asked for until its answer has reached this session.
    // A parent waiting on delegates has ended its turn and would read as
    // done; this is where it shows that it is not. A chip opens the
    // delegate's own transcript, which streams live.
    //
    // Rows come from the delegates endpoint: on open, whenever the
    // runtime says the delegates likely changed (an ask, an answer, a
    // wake), and every couple of seconds only while one is running — the
    // last-step line is the one thing that moves without an event.
    import { loadDelegates } from './runtime.svelte.js'
    import { stepLine } from './activity.js'

    let { rt, name, onSwitch } = $props()

    const POLL_MS = 2000
    let rows = $state([])
    let now = $state(Date.now() / 1000)

    const out = $derived(rows.filter((r) => r.status === 'running' || (r.known && !r.delivered)))
    const running = $derived(out.some((r) => r.status === 'running'))

    async function load(who) {
        try {
            const fresh = await loadDelegates(who)
            if (who === name) rows = fresh
        } catch {
            // a failed refresh keeps what was showing; the next one retries
        }
    }

    // Which session the rows are for. Plain, not state: the effect below
    // writes `rows`, and reading it there would make the effect its own
    // trigger.
    let shownFor = null

    $effect(() => {
        const who = name
        rt?.delegateTick // refresh when the runtime says something changed
        if (shownFor !== who) {
            rows = []
            shownFor = who
        }
        load(who)
    })

    $effect(() => {
        if (!running) return
        const who = name
        const poll = setInterval(() => {
            if (document.visibilityState === 'visible') load(who)
        }, POLL_MS)
        const tick = setInterval(() => (now = Date.now() / 1000), 1000)
        return () => {
            clearInterval(poll)
            clearInterval(tick)
        }
    })

    // `boss.scout` under `boss` reads as `scout`, the handle its parent gave it
    const leaf = (child) => (child.startsWith(name + '.') ? child.slice(name.length + 1) : child)

    function elapsed(r) {
        if (!r.started) return ''
        const end = r.status === 'running' ? now : (r.finished ?? now)
        const s = Math.max(0, Math.round(end - r.started))
        return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
    }

    function doing(r) {
        if (r.status !== 'running') {
            return r.status === 'capped' ? 'ran out of turns' : r.status
        }
        const step = r.step
        if (!step) return 'starting'
        if (step.thinking) return 'thinking'
        if (step.writing) return 'writing its answer'
        const { verb, subject } = stepLine(step)
        return subject ? `${verb} ${subject}` : verb
    }
</script>

{#if out.length}
    <div class="delegate-strip" aria-live="polite">
        <span class="label">⑂ {running ? 'working' : 'answered'}</span>
        {#each out as r (r.name)}
            <button
                class="chip {r.status}"
                title={r.task ? `${r.name}: ${r.task}` : r.name}
                onclick={() => onSwitch(r.name)}
            >
                <span class="dot"></span>
                <span class="who">{leaf(r.name)}</span>
                <span class="when">{elapsed(r)}</span>
                <span class="doing">{doing(r)}</span>
            </button>
        {/each}
    </div>
{/if}

<style>
    .delegate-strip {
        flex-shrink: 0;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.35rem;
        padding: 0.45rem 1rem 0;
    }
    .label {
        color: var(--text-muted);
        font-size: 0.7rem;
        margin-right: 0.15rem;
    }
    .chip {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        max-width: 22rem;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 999px;
        color: var(--text-muted);
        font-family: inherit;
        font-size: 0.7rem;
        padding: 0.15rem 0.65rem 0.15rem 0.5rem;
        cursor: pointer;
    }
    .chip:hover {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .dot {
        width: 0.45rem;
        height: 0.45rem;
        border-radius: 50%;
        flex-shrink: 0;
        background: var(--text-muted);
    }
    .chip.running .dot {
        background: var(--accent);
        animation: pulse 1.4s ease-in-out infinite;
    }
    .chip.answered .dot {
        background: var(--success);
    }
    .chip.failed .dot {
        background: var(--error);
    }
    .chip.capped .dot {
        background: var(--warning);
    }
    .who {
        color: var(--text);
        font-weight: 600;
    }
    .when {
        font-variant-numeric: tabular-nums;
    }
    .doing {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
    }
    @keyframes pulse {
        50% {
            opacity: 0.35;
        }
    }
    @media (prefers-reduced-motion: reduce) {
        .chip.running .dot {
            animation: none;
        }
    }
</style>
