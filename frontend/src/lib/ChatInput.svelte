<script>
    // The composer: one rounded card holding chips + auto-growing
    // textarea + an in-card toolbar (attach left; model picker + send
    // right) — the agex-studio / Claude.ai shape. Files upload
    // immediately (each is a checkpointed workspace write under
    // /uploads/); the chips just decide what the NEXT message mentions.
    import { uploadFiles } from './api.js'
    import ModelPicker from './ModelPicker.svelte'

    let { rt } = $props()

    let text = $state('')
    let uploading = $state(false)
    let dragover = $state(false)
    let textarea = $state(null)
    let fileInput = $state(null)

    // auto-grow with the draft, capped — then scroll inside
    $effect(() => {
        void text
        if (!textarea) return
        textarea.style.height = 'auto'
        textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px'
    })

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

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
    class="input-wrap"
    class:dragover
    ondragover={(e) => {
        e.preventDefault()
        dragover = true
    }}
    ondragleave={(e) => {
        if (e.currentTarget === e.target) dragover = false
    }}
    ondrop={(e) => {
        e.preventDefault()
        dragover = false
        attach(e.dataTransfer.files)
    }}
>
    <div class="input-card">
        {#if rt.attachments.length}
            <div class="chips">
                {#each rt.attachments as p (p)}
                    <span class="chip" title={p}>
                        {p.split('/').pop()}
                        <button
                            class="chip-remove"
                            aria-label="remove attachment"
                            onclick={() =>
                                (rt.attachments = rt.attachments.filter(
                                    (x) => x !== p,
                                ))}>×</button
                        >
                    </span>
                {/each}
            </div>
        {/if}

        <textarea
            bind:this={textarea}
            bind:value={text}
            {onkeydown}
            rows="1"
            placeholder="Ask the agent to build something…"
        ></textarea>

        <div class="toolbar">
            <div class="toolbar-left">
                <button
                    class="icon-btn"
                    title="attach files (they land in /uploads/)"
                    aria-label="Attach files"
                    disabled={uploading}
                    onclick={() => fileInput?.click()}
                >
                    {#if uploading}
                        <span class="spin">◌</span>
                    {:else}
                        <svg
                            width="17"
                            height="17"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="2"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                            aria-hidden="true"
                        >
                            <line x1="12" y1="5" x2="12" y2="19"></line>
                            <line x1="5" y1="12" x2="19" y2="12"></line>
                        </svg>
                    {/if}
                </button>
                <input
                    bind:this={fileInput}
                    type="file"
                    multiple
                    style="display: none"
                    onchange={(e) => {
                        attach(e.target.files)
                        e.target.value = ''
                    }}
                />
            </div>
            <div class="toolbar-right">
                {#if rt.usage}
                    <span
                        class="ctx"
                        title={`context sent on the last model call` +
                            (rt.usage.cached_tokens
                                ? ` (${Math.round(rt.usage.cached_tokens / 1000)}k cached)`
                                : '')}
                        >{Math.round(rt.usage.input_tokens / 1000)}k ctx</span
                    >
                {/if}
                <ModelPicker {rt} />
                {#if rt.busy}
                    <button
                        class="send-btn stop"
                        onclick={() => rt.stop()}
                        title="stop this turn (work so far is kept)"
                        aria-label="Stop"
                    >
                        <svg
                            width="11"
                            height="11"
                            viewBox="0 0 24 24"
                            fill="currentColor"
                            aria-hidden="true"
                        >
                            <rect x="4" y="4" width="16" height="16" rx="2"></rect>
                        </svg>
                    </button>
                {:else}
                    <button
                        class="send-btn"
                        disabled={!text.trim()}
                        onclick={submit}
                        title="send (Enter)"
                        aria-label="Send"
                    >
                        <svg
                            width="15"
                            height="15"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="2.5"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                            aria-hidden="true"
                        >
                            <line x1="12" y1="19" x2="12" y2="5"></line>
                            <polyline points="5 12 12 5 19 12"></polyline>
                        </svg>
                    </button>
                {/if}
            </div>
        </div>
    </div>

    {#if dragover}
        <div class="drop-overlay">drop files to attach</div>
    {/if}
</div>

<style>
    .input-wrap {
        position: relative;
        padding: 0.6rem 1rem 0.85rem;
        flex-shrink: 0;
        border-top: 1px solid var(--border);
        background: var(--surface);
    }
    .input-card {
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 0.55rem 0.6rem 0.4rem 0.75rem;
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
        transition: border-color 0.15s;
    }
    .input-card:focus-within {
        border-color: var(--text-muted);
    }
    .chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
    }
    .chip {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 999px;
        font-size: 0.72rem;
        padding: 0.15rem 0.6rem;
        max-width: 220px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .chip-remove {
        background: none;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        font-size: 0.85rem;
        line-height: 1;
        padding: 0;
    }
    .chip-remove:hover {
        color: var(--text);
    }
    textarea {
        resize: none;
        background: none;
        border: none;
        color: var(--text);
        font-family: inherit;
        font-size: 0.88rem;
        line-height: 1.45;
        outline: none;
        max-height: 200px;
        overflow-y: auto;
    }
    textarea::placeholder {
        color: var(--text-muted);
    }
    .toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .toolbar-left,
    .toolbar-right {
        display: flex;
        align-items: center;
        gap: 0.35rem;
    }
    .ctx {
        color: var(--text-muted);
        font-size: 0.68rem;
        font-family: var(--font-mono);
    }
    .icon-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        background: none;
        border: none;
        border-radius: 7px;
        color: var(--text-muted);
        cursor: pointer;
    }
    .icon-btn:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .send-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 30px;
        height: 30px;
        background: var(--accent);
        border: none;
        border-radius: 50%;
        color: #fff;
        cursor: pointer;
        transition: opacity 0.15s;
    }
    .send-btn:hover:not(:disabled) {
        background: var(--accent-hover);
    }
    .send-btn:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .send-btn.stop {
        background: var(--surface-hover);
        color: var(--text);
        border: 1px solid var(--border);
        animation: pulse 1.6s ease-in-out infinite;
    }
    .send-btn.stop:hover {
        background: color-mix(in srgb, var(--error) 25%, var(--surface));
        border-color: var(--error);
        animation: none;
    }
    .spin {
        display: inline-block;
        animation: spin 1s linear infinite;
        font-size: 0.9rem;
    }
    .input-wrap.dragover .input-card {
        border-color: var(--accent);
    }
    .drop-overlay {
        position: absolute;
        inset: 0.6rem 1rem 0.85rem;
        display: flex;
        align-items: center;
        justify-content: center;
        background: color-mix(in srgb, var(--accent) 12%, var(--bg));
        border: 2px dashed var(--accent);
        border-radius: 14px;
        color: var(--text);
        font-size: 0.85rem;
        pointer-events: none;
    }
</style>
