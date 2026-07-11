<script>
    // One agent turn: an ordered item list folded by the runtime.
    // Consecutive tool calls collapse into one activity chip; prose
    // renders as markdown with workspace-image references spliced in
    // as live artifact renderers (the code-interpreter URI idiom).
    import Markdown from './Markdown.svelte'
    import Artifact from './Artifact.svelte'
    import ToolGroup from './ToolGroup.svelte'

    let { msg, session } = $props()

    // group items: runs of tools together, everything else standalone
    const groups = $derived.by(() => {
        const out = []
        for (const item of msg.items) {
            const last = out.at(-1)
            if (item.kind === 'tool') {
                if (last?.kind === 'tools') last.tools.push(item)
                else out.push({ kind: 'tools', tools: [item] })
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
            <ToolGroup tools={g.tools} {session} />
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
        max-width: 92%;
    }
    .bubble {
        background: var(--agent-bubble);
        border-radius: 12px 12px 12px 4px;
        padding: 0.6rem 0.85rem;
        margin: 0.35rem 0;
        font-size: 0.88rem;
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
