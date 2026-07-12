<script>
    // One agent turn: an ordered item list folded by the runtime.
    // Consecutive tool calls collapse into one activity chip; prose
    // renders as markdown with workspace-image references spliced in
    // as live artifact renderers (the code-interpreter URI idiom).
    import Markdown from './Markdown.svelte'
    import Artifact from './Artifact.svelte'
    import ToolGroup from './ToolGroup.svelte'
    import ThinkingBlock from './ThinkingBlock.svelte'

    let { msg, session } = $props()

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
    {#each groups as g, i (i)}
        {#if g.kind === 'tools'}
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
    {#if msg.streaming}
        <div class="thinking"><span class="pulse-dot"></span></div>
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
    .thinking {
        padding: 0.4rem 0.2rem;
    }
    .pulse-dot {
        display: inline-block;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: var(--accent);
        animation: pulse 1.2s ease-in-out infinite;
    }
</style>
