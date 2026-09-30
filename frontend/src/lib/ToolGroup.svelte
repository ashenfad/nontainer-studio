<script>
    // A run of work (tool calls with the model's thinking interleaved
    // between them) as one quiet line in plain words: "Ran 2 commands,
    // edited 1 file". Opening it lists one line per step and per
    // stretch of thinking, in arrival order; each of those opens to its
    // detail. Three levels, so a long run reads as a sentence until
    // someone asks for more.
    import ToolStep from './ToolStep.svelte'
    import ThinkingBlock from './ThinkingBlock.svelte'
    import { groupPhrases } from './activity.js'

    let { entries, session } = $props()

    let open = $state(false)

    const tools = $derived(entries.filter((e) => e.kind === 'tool'))
    const running = $derived(tools.some((t) => t.running))
    const phrases = $derived(groupPhrases(tools))
</script>

<div class="activity">
    <button class="act-line group-line" class:open class:act-live={running} onclick={() => (open = !open)}>
        {#each phrases as ph, i (i)}
            <span class="act-verb">{ph.verb}</span>
            <span class="act-rest">{ph.rest}{i < phrases.length - 1 ? ',' : ''}</span>
        {/each}
        <span class="act-chev">⌄</span>
    </button>
    {#if open}
        <div class="act-body timeline">
            {#each entries as e, i (i)}
                {#if e.kind === 'tool'}
                    <ToolStep tool={e} {session} />
                {:else if e.kind === 'thinking'}
                    <ThinkingBlock item={e} />
                {/if}
            {/each}
        </div>
    {/if}
</div>

<style>
    .activity {
        margin: 0.1rem 0;
    }
</style>
