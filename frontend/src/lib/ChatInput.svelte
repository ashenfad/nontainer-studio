<script>
    // Prompt box + attachment queue. Files upload immediately (each is
    // a checkpointed workspace write under /uploads/); the queue chips
    // just decide what the NEXT message mentions.
    import { uploadFiles } from './api.js'

    let { rt } = $props()

    let text = $state('')
    let uploading = $state(false)
    let dragover = $state(false)

    async function attach(fileList) {
        if (!fileList?.length) return
        uploading = true
        try {
            const paths = await uploadFiles(rt.name, fileList)
            rt.attachments = [...new Set([...rt.attachments, ...paths])]
        } catch (e) {
            rt.messages.push({ role: 'error', text: `upload failed: ${e.message}` })
        } finally {
            uploading = false
        }
    }

    async function submit() {
        const message = text
        text = ''
        if (!(await rt.send(message))) text = message // keep the draft on failure
    }

    function onkeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            if (!rt.busy && text.trim()) submit()
        }
    }
</script>

<div
    class="composer"
    class:dragover
    role="group"
    ondragover={(e) => {
        e.preventDefault()
        dragover = true
    }}
    ondragleave={() => (dragover = false)}
    ondrop={(e) => {
        e.preventDefault()
        dragover = false
        attach(e.dataTransfer.files)
    }}
>
    {#if rt.attachments.length}
        <div class="chips">
            {#each rt.attachments as p (p)}
                <span class="chip">
                    {p.split('/').pop()}
                    <button
                        aria-label="remove attachment"
                        onclick={() =>
                            (rt.attachments = rt.attachments.filter((x) => x !== p))}
                        >×</button
                    >
                </span>
            {/each}
        </div>
    {/if}
    <div class="row">
        <label class="attach" title="upload files into /uploads/">
            {uploading ? '⏳' : '+'}
            <input
                type="file"
                multiple
                hidden
                onchange={(e) => {
                    attach(e.target.files)
                    e.target.value = ''
                }}
            />
        </label>
        <textarea
            rows="2"
            placeholder="Ask the agent to build something… (drop files to attach)"
            bind:value={text}
            {onkeydown}
        ></textarea>
        <button
            class="send"
            disabled={rt.busy || !text.trim()}
            onclick={submit}
        >
            {rt.busy ? 'running…' : 'send'}
        </button>
    </div>
</div>

<style>
    .composer {
        border-top: 1px solid var(--border);
        padding: 0.7rem 1rem 0.9rem;
        background: var(--surface);
    }
    .composer.dragover {
        outline: 2px dashed var(--accent);
        outline-offset: -6px;
    }
    .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
        padding-bottom: 0.5rem;
    }
    .chip {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        background: var(--input-bg);
        border-radius: 999px;
        font-size: 0.72rem;
        padding: 0.15rem 0.6rem;
    }
    .chip button {
        background: none;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        font-size: 0.85rem;
        line-height: 1;
        padding: 0;
    }
    .chip button:hover {
        color: var(--text);
    }
    .row {
        display: flex;
        gap: 0.6rem;
        align-items: flex-end;
    }
    .attach {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 34px;
        height: 34px;
        border: 1px solid var(--border);
        border-radius: 8px;
        color: var(--text-muted);
        font-size: 1.1rem;
        cursor: pointer;
        flex-shrink: 0;
    }
    .attach:hover {
        color: var(--text);
        background: var(--surface-hover);
    }
    textarea {
        flex: 1;
        resize: none;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        color: var(--text);
        font-family: inherit;
        font-size: 0.88rem;
        padding: 0.55rem 0.75rem;
        outline: none;
    }
    textarea:focus {
        border-color: var(--text-muted);
    }
    .send {
        background: var(--accent);
        border: none;
        color: #fff;
        border-radius: 8px;
        font-size: 0.82rem;
        font-weight: 600;
        padding: 0.55rem 1rem;
        cursor: pointer;
        flex-shrink: 0;
    }
    .send:hover:not(:disabled) {
        background: var(--accent-hover);
    }
    .send:disabled {
        opacity: 0.45;
        cursor: default;
    }
</style>
