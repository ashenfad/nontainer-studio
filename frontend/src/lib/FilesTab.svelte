<script>
    // Workspace file browser: tree list + inline viewer (images render,
    // text truncates, everything downloadable via the raw endpoint).
    import { api, fileUrl } from './api.js'

    let { rt } = $props()

    let paths = $state([])
    let selected = $state(null)
    let text = $state(null)

    $effect(() => {
        void rt.version
        const session = rt.name
        api(`/api/sessions/${session}/files`)
            .then((d) => {
                paths = d.files
                if (selected && !d.files.includes(selected)) {
                    selected = null
                    text = null
                }
            })
            .catch(() => {})
    })

    const isImage = (p) => /\.(png|jpe?g|gif|webp)$/i.test(p)

    async function open(path) {
        selected = path
        text = null
        if (isImage(path)) return
        const res = await fetch(fileUrl(rt.name, path))
        const body = await res.text()
        text = body.length > 20000 ? body.slice(0, 20000) + '\n…[truncated]' : body
    }
</script>

<div class="files">
    <div class="list">
        {#each paths as p (p)}
            <button class="file" class:active={p === selected} onclick={() => open(p)}>
                {p}
            </button>
        {/each}
        {#if paths.length === 0}
            <div class="hint">no files yet</div>
        {/if}
    </div>
    <div class="view">
        {#if selected && isImage(selected)}
            <img src={fileUrl(rt.name, selected)} alt={selected} />
        {:else if selected}
            <div class="view-bar">
                <code>{selected}</code>
                <a href={fileUrl(rt.name, selected)} download={selected.split('/').pop()}
                    >download</a
                >
            </div>
            <pre>{text ?? '…'}</pre>
        {:else}
            <div class="hint center">select a file</div>
        {/if}
    </div>
</div>

<style>
    .files {
        display: flex;
        flex: 1;
        min-height: 0;
    }
    .list {
        width: 42%;
        max-width: 320px;
        overflow-y: auto;
        border-right: 1px solid var(--border);
        display: flex;
        flex-direction: column;
        padding: 0.4rem;
    }
    .file {
        background: none;
        border: none;
        color: var(--text-muted);
        font-family: var(--font-mono);
        font-size: 0.72rem;
        text-align: left;
        padding: 0.28rem 0.5rem;
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
    .file.active {
        background: var(--surface-hover);
        color: var(--text);
    }
    .view {
        flex: 1;
        overflow: auto;
        display: flex;
        flex-direction: column;
        min-width: 0;
    }
    .view-bar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0.4rem 0.8rem;
        border-bottom: 1px solid var(--border);
        font-size: 0.72rem;
        color: var(--text-muted);
    }
    .view-bar a {
        color: var(--link);
        font-size: 0.72rem;
    }
    .view img {
        max-width: 100%;
        padding: 0.8rem;
    }
    .view pre {
        padding: 0.8rem;
        font-size: 0.74rem;
        white-space: pre-wrap;
        word-break: break-word;
        overflow-y: auto;
    }
    .hint {
        color: var(--text-muted);
        font-size: 0.78rem;
        padding: 0.6rem;
    }
    .hint.center {
        margin: auto;
    }
</style>
