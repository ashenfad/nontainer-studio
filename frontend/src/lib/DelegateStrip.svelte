<script>
    // The session's delegates while they are out: one card each, from the
    // moment one is asked for until its answer has reached this session.
    // A parent waiting on delegates has ended its turn and would read as
    // done; this is where it shows that it is not. A card opens the
    // delegate's own transcript, which streams live.
    //
    // Cards sit in a grid of equal columns, so a step line changing on
    // every poll changes only its own text: sized to their contents, the
    // cards reflowed between rows as their steps came and went.
    //
    // Rows come from the delegates endpoint: on open, whenever the
    // runtime says the delegates likely changed (an ask, an answer, a
    // wake), and every couple of seconds only while one is running — the
    // last-step line is the one thing that moves without an event.
    import { loadDelegates } from './runtime.svelte.js'
    import { stepLine } from './activity.js'
    import { latestOnly } from './latest.js'

    let { rt, name, onSwitch } = $props()

    const POLL_MS = 2000
    let rows = $state([])
    let now = $state(Date.now() / 1000)

    // Statuses that carry an answer to deliver; a cancelled or swept job
    // has none, so it never holds a card open waiting for one.
    const DELIVERABLE = new Set(['answered', 'failed', 'capped'])
    const out = $derived(
        rows.filter(
            (r) => r.status === 'running' || (r.known && !r.delivered && DELIVERABLE.has(r.status)),
        ),
    )
    const running = $derived(out.some((r) => r.status === 'running'))

    // "2 delegates working · 1 answered · 1 failed": a count per status,
    // in this order, so a failure is never counted as an answer
    const COUNTED = [
        ['running', 'working'],
        ['answered', 'answered'],
        ['failed', 'failed'],
        ['capped', 'out of turns'],
    ]
    const counts = $derived(
        COUNTED.map(([status, word]) => [out.filter((r) => r.status === status).length, word])
            .filter(([k]) => k)
            .map(([k, word], i) => (i ? `${k} ${word}` : `${k} delegate${k === 1 ? '' : 's'} ${word}`)),
    )

    // The poll and an event can overlap. Only the newest request's rows
    // are shown: an older one answering last would bring back a card the
    // newer one had already seen delivered.
    const fetchRows = latestOnly(loadDelegates)

    async function load(who) {
        try {
            const fresh = await fetchRows(who)
            if (fresh !== latestOnly.SUPERSEDED && who === name) rows = fresh
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
        <span class="label"
            >⑂ {#each counts as count, i}{#if i}{' · '}{/if}<span class="count">{count}</span>{/each}</span
        >
        <div class="cards">
            {#each out as r (r.name)}
                <button
                    class="card {r.status}"
                    title={r.task ? `${r.name}: ${r.task}` : r.name}
                    onclick={() => onSwitch(r.name)}
                >
                    <span class="head">
                        <span class="dot"></span>
                        <span class="who">{leaf(r.name)}</span>
                        <span class="when">{elapsed(r)}</span>
                    </span>
                    <span class="doing">{doing(r)}</span>
                </button>
            {/each}
        </div>
    </div>
{/if}

<style>
    .delegate-strip {
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        gap: 0.3rem;
        padding: 0.45rem 1rem 0;
    }
    .label {
        color: var(--text-muted);
        font-size: 0.7rem;
    }
    /* a narrow pane wraps the label between counts, never inside one */
    .count {
        white-space: nowrap;
    }
    .cards {
        display: grid;
        /* a narrow chat pane gets one column that fits it, not a 15rem
           card spilling into the panel beside it */
        grid-template-columns: repeat(auto-fill, minmax(min(15rem, 100%), 1fr));
        gap: 0.35rem;
    }
    .card {
        display: flex;
        flex-direction: column;
        gap: 0.1rem;
        min-width: 0;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 0.5rem;
        color: var(--text-muted);
        font-family: inherit;
        font-size: 0.7rem;
        text-align: left;
        padding: 0.3rem 0.6rem;
        cursor: pointer;
    }
    .card:hover {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .head {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        min-width: 0;
    }
    .dot {
        width: 0.45rem;
        height: 0.45rem;
        border-radius: 50%;
        flex-shrink: 0;
        background: var(--text-muted);
    }
    .card.running .dot {
        background: var(--accent);
        animation: pulse 1.4s ease-in-out infinite;
    }
    .card.answered .dot {
        background: var(--success);
    }
    .card.failed .dot {
        background: var(--error);
    }
    .card.capped .dot {
        background: var(--warning);
    }
    .who {
        color: var(--text);
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
    }
    .when {
        margin-left: auto;
        flex-shrink: 0;
        font-variant-numeric: tabular-nums;
    }
    .doing {
        /* under the name, lined up with it past the dot */
        padding-left: 0.85rem;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    @keyframes pulse {
        50% {
            opacity: 0.35;
        }
    }
    @media (prefers-reduced-motion: reduce) {
        .card.running .dot {
            animation: none;
        }
    }
</style>
