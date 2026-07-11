<script>
    // Workspace file list — viewing happens in the shared FileModal
    // (one rich viewer for every surface), so this is just the index.
    import { api } from './api.js'
    import { viewFile } from './viewer.svelte.js'

    let { rt } = $props()

    let paths = $state([])

    $effect(() => {
        void rt.version
        const session = rt.name
        api(`/api/sessions/${session}/files`)
            .then((d) => (paths = d.files))
            .catch(() => {})
    })
</script>

<div class="files">
    {#each paths as p (p)}
        <button class="file" onclick={() => viewFile(p)}>{p}</button>
    {/each}
    {#if paths.length === 0}
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
