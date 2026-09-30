<script>
    // One agent turn: an ordered item list folded by the runtime.
    // Consecutive tool calls collapse into one activity chip; prose
    // renders as markdown with workspace-image references spliced in
    // as live artifact renderers (the code-interpreter URI idiom).
    import Markdown from './Markdown.svelte'
    import Artifact from './Artifact.svelte'
    import ToolGroup from './ToolGroup.svelte'
    import ThinkingBlock from './ThinkingBlock.svelte'
    import { duration } from './activity.js'

    let { msg, session } = $props()

    // A finished turn folds its work -- tool runs and thinking -- under
    // one line, "Worked for 3m", and keeps its prose and artifacts in
    // view: the prose is the agent talking to the human, the work is
    // how it got there. A turn still running shows everything.
    let showWork = $state(false)
    const hasWork = $derived(
        msg.items.some((i) => i.kind === 'tool' || i.kind === 'thinking'),
    )
    const folded = $derived(!msg.streaming && hasWork && !showWork)
    const worked = $derived(duration(msg.startTs, msg.endTs))
    // the live tail already says it is working when it is a running
    // tool or streaming thinking; otherwise one quiet line does
    const liveTail = $derived.by(() => {
        const last = msg.items.at(-1)
        return last?.kind === 'thinking' || (last?.kind === 'tool' && last.running)
    })

    // Group items: runs of WORK (tools + the thinking interleaved
    // between them) collapse into one activity chip; prose and
    // artifacts stand alone. Thinking joins a work group when it
    // follows one or leads into a tool — EXCEPT the live tail: while
    // the newest streamed item is thinking, it renders standalone so
    // the live block stays visible without expanding the chip (it
    // folds in when the model moves on).
    const groups = $derived.by(() => {
        const out = []
        const items = msg.items
        for (let i = 0; i < items.length; i++) {
            const item = items[i]
            const last = out.at(-1)
            const liveTail = msg.streaming && i === items.length - 1
            const joinsWork =
                item.kind === 'tool' ||
                (item.kind === 'thinking' &&
                    !liveTail &&
                    (last?.kind === 'tools' || items[i + 1]?.kind === 'tool'))
            if (joinsWork) {
                if (last?.kind === 'tools') last.entries.push(item)
                else out.push({ kind: 'tools', entries: [item] })
            } else out.push(item)
        }
        return out
    })

    // split prose on ![name](/workspace/path) image refs
    function splice(text) {
        const parts = []
        const re = /!\[([^\]]*)\]\((\/[^)\s]+)\)/g
        let last = 0
        let m
        while ((m = re.exec(text))) {
            if (m.index > last) parts.push({ md: text.slice(last, m.index) })
            parts.push({ name: m[1], path: m[2] })
            last = m.index + m[0].length
        }
        if (last < text.length) parts.push({ md: text.slice(last) })
        return parts
    }
</script>

<div class="agent-msg">
    {#if !msg.streaming && hasWork}
        <button class="act-line worked" class:open={showWork} onclick={() => (showWork = !showWork)}>
            <span class="act-verb">Worked</span>
            {#if worked}<span class="act-rest">{worked}</span>{/if}
            <span class="act-chev">⌄</span>
        </button>
    {/if}
    {#each groups as g, i (i)}
        {#if folded && (g.kind === 'tools' || g.kind === 'thinking')}
            <!-- folded under "Worked" -->
        {:else if g.kind === 'tools'}
            <ToolGroup entries={g.entries} {session} />
        {:else if g.kind === 'thinking'}
            <ThinkingBlock item={g} live={msg.streaming && g === msg.items.at(-1)} />
        {:else if g.kind === 'text'}
            <div class="bubble">
                {#each splice(g.text) as part, j (j)}
                    {#if part.md !== undefined}
                        <Markdown text={part.md} />
                    {:else}
                        <Artifact {session} path={part.path} name={part.name} />
                    {/if}
                {/each}
            </div>
        {:else if g.kind === 'image' || g.kind === 'artifact'}
            <Artifact {session} path={g.path} name={g.name} />
        {/if}
    {/each}
    {#if msg.streaming && !liveTail}
        <div class="act-line act-live working"><span class="act-verb">Working</span><span class="act-rest">…</span></div>
    {/if}
</div>

<style>
    .agent-msg {
        max-width: 100%;
    }
    /* agent prose sits directly on the page (the agex-studio look) —
       only USER messages get a bubble; the reply is the document */
    .bubble {
        padding: 0.15rem 0.1rem;
        margin: 0.35rem 0;
        font-size: 0.88rem;
        line-height: 1.55;
    }
    .worked {
        display: flex;
    }
    .working {
        cursor: default;
    }
</style>
