<script>
    // Workspace file list — viewing happens in the shared FileModal
    // (one rich viewer for every surface), so this is just the index.
    import { api } from './api.js'
    import { viewFile } from './viewer.svelte.js'

    let { rt } = $props()

    let paths = $state([])
    let failed = $state(false)

    // Refetch on turn activity (version), session switch (name), and
    // SSE reconnect (connected — a server restart mid-view otherwise
    // leaves this pane frozen). The stale flag guards the async race:
    // a slow response for session A must not land after B's, and a
    // FAILED fetch must clear rather than keep showing the previous
    // session's files as if they were current.
    $effect(() => {
        void rt.version
        void rt.connected
        const session = rt.name
        let stale = false
        api(`/api/sessions/${session}/files`)
            .then((d) => {
                if (!stale) {
                    paths = d.files
                    failed = false
                }
            })
            .catch(() => {
                if (!stale) {
                    paths = []
                    failed = true
                }
            })
        return () => (stale = true)
    })
</script>

<div class="files">
    {#each paths as p (p)}
        <button class="file" onclick={() => viewFile(p)}>{p}</button>
    {/each}
    {#if failed}
        <div class="hint">couldn't load files — retrying on next update</div>
    {:else if paths.length === 0}
        <div class="hint">no files yet</div>
    {/if}
</div>

<style>
    .files {
        flex: 1;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        padding: 0.5rem;
    }
    .file {
        background: none;
        border: none;
        color: var(--text-muted);
        font-family: var(--font-mono);
        font-size: 0.75rem;
        text-align: left;
        padding: 0.3rem 0.55rem;
        border-radius: 5px;
        cursor: pointer;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex-shrink: 0;
    }
    .file:hover {
        background: var(--surface-hover);
        color: var(--text);
    }
    .hint {
        color: var(--text-muted);
        font-size: 0.78rem;
        padding: 0.4rem;
    }
</style>
