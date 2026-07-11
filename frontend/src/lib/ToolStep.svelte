<script>
    // One tool call in the activity timeline, rendered by TYPE (the
    // agex-studio EventDetail idiom): terminal commands as a prompt
    // block, python highlighted, file writes highlighted by extension,
    // file edits as a computed line diff, test_app runs with their
    // verdict. Structured args come from the server; legacy string
    // args fall back to the generic view.
    import { fileUrl } from './api.js'
    import { highlightCode } from './markdown.js'
    import { lineDiff } from './diff.js'
    import { viewFile } from './viewer.svelte.js'

    let { tool, session } = $props()

    const args = $derived.by(() => {
        if (tool.args && typeof tool.args === 'object') return tool.args
        // salvage JSON-string args (older events; defensive for any
        // provider that ships arguments unparsed). Python-repr strings
        // from pre-structured transcripts stay in the generic view.
        if (typeof tool.args === 'string' && tool.args.startsWith('{')) {
            try {
                return JSON.parse(tool.args)
            } catch {
                return null
            }
        }
        return null
    })

    const EXT_LANGS = {
        py: 'python',
        js: 'javascript',
        mjs: 'javascript',
        ts: 'typescript',
        html: 'html',
        htm: 'html',
        css: 'css',
        json: 'json',
        sh: 'bash',
        sql: 'sql',
        md: 'markdown',
    }
    const langFor = (path) =>
        EXT_LANGS[(path ?? '').split('.').pop()?.toLowerCase()] ?? 'plaintext'

    const kind = $derived.by(() => {
        if (!args) return 'generic'
        if (tool.name === 'terminal' && typeof args.command === 'string')
            return 'terminal'
        if (tool.name === 'run_python' && typeof args.code === 'string')
            return 'python'
        if (tool.name === 'file_write' && typeof args.path === 'string')
            return 'write'
        if (tool.name === 'file_edit' && typeof args.path === 'string')
            return 'edit'
        if (tool.name === 'view_image' && typeof args.path === 'string')
            return 'view'
        if (tool.name === 'test_app' && Array.isArray(args.actions))
            return 'test'
        return 'generic'
    })

    const label = $derived.by(() => {
        if (kind === 'write') return `write — ${args.path}`
        if (kind === 'edit')
            return `edit${args.replace_all ? ' (all)' : ''} — ${args.path}`
        if (kind === 'view') return `view — ${args.path}`
        if (kind === 'python') return 'python'
        return tool.name
    })

    const diff = $derived(
        kind === 'edit'
            ? lineDiff(args.old_string ?? '', args.new_string ?? '')
            : [],
    )

    const verdict = $derived.by(() => {
        if (kind !== 'test' || typeof tool.result !== 'string') return null
        if (tool.result.startsWith('test_app: PASS')) return 'pass'
        if (tool.result.startsWith('test_app: FAIL')) return 'fail'
        return null
    })

    function pretty(value) {
        return typeof value === 'string' ? value : JSON.stringify(value, null, 1)
    }
</script>

<div class="step">
    <div class="step-name">
        {label}
        {#if tool.running}<span class="working">working…</span>{/if}
    </div>

    {#if kind === 'terminal'}
        <pre class="block terminal">{'$ ' +
                args.command.split('\n').join('\n$ ')}</pre>
    {:else if kind === 'python'}
        <!-- eslint-disable-next-line svelte/no-at-html-tags — hljs over escaped text -->
        <pre class="block"><code class="hljs"
                >{@html highlightCode(args.code, 'python')}</code
            ></pre>
    {:else if kind === 'write'}
        <!-- eslint-disable-next-line svelte/no-at-html-tags — hljs over escaped text -->
        <pre class="block"><code class="hljs"
                >{@html highlightCode(args.content ?? '', langFor(args.path))}</code
            ></pre>
    {:else if kind === 'edit'}
        <pre class="block diff">{#each diff as line, i (i)}<span
                    class="diff-{line.type}"
                    >{line.type === 'removed'
                        ? '− '
                        : line.type === 'added'
                          ? '+ '
                          : '  '}{line.text}
</span>{/each}</pre>
    {:else if kind === 'view'}
        <button class="img-btn" title={args.path} onclick={() => viewFile(args.path)}>
            <img class="step-img" src={fileUrl(session, args.path)} alt={args.path} />
        </button>
    {:else if kind === 'test'}
        <pre class="block">{args.actions
                .map((a) => JSON.stringify(a))
                .join('\n')}</pre>
    {:else if tool.args}
        <pre class="block args">{pretty(tool.args)}</pre>
    {/if}

    {#if tool.result != null && kind !== 'view'}
        <pre
            class="block result"
            class:pass={verdict === 'pass'}
            class:fail={verdict === 'fail'}>{tool.result}</pre>
    {/if}

    {#if tool.images?.length}
        <div class="step-images">
            {#each tool.images as p (p)}
                <button class="img-btn" title={p} onclick={() => viewFile(p)}>
                    <img class="step-img" src={fileUrl(session, p)} alt={p} />
                </button>
            {/each}
        </div>
    {/if}
</div>

<style>
    .step-name {
        font-size: 0.72rem;
        font-weight: 600;
        color: var(--purple);
        display: flex;
        gap: 0.5rem;
        align-items: baseline;
        font-family: var(--font-mono);
    }
    .working {
        color: var(--accent);
        font-weight: 400;
        font-family: var(--font-body);
        animation: pulse 1.2s ease-in-out infinite;
    }
    .block {
        font-size: 0.72rem;
        background: rgba(255, 255, 255, 0.05);
        border-radius: 6px;
        padding: 0.4rem 0.6rem;
        margin: 0.25rem 0 0;
        overflow-x: auto;
        max-height: 260px;
        overflow-y: auto;
        /* no wrapping: long lines scroll horizontally (code, terminal
           output, and df prints read wrong when soft-wrapped) */
        white-space: pre;
        line-height: 1.45;
    }
    .terminal {
        background: rgba(0, 0, 0, 0.35);
        color: #9ece9e;
    }
    .args {
        color: var(--text-muted);
    }
    .result {
        color: var(--text-muted);
    }
    .result.pass {
        color: var(--success);
    }
    .result.fail {
        color: var(--error);
    }
    .diff {
        padding: 0.4rem 0.4rem;
    }
    .diff span {
        display: block;
        /* highlight bars span the scrolled width, not just the viewport */
        width: max-content;
        min-width: 100%;
    }
    .diff-removed {
        background: color-mix(in srgb, var(--error) 14%, transparent);
        color: color-mix(in srgb, var(--error) 70%, var(--text));
    }
    .diff-added {
        background: color-mix(in srgb, var(--success) 12%, transparent);
        color: color-mix(in srgb, var(--success) 65%, var(--text));
    }
    .diff-context {
        color: var(--text-muted);
    }
    .step-images {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin-top: 0.3rem;
    }
    .img-btn {
        background: none;
        border: none;
        padding: 0;
        cursor: zoom-in;
        margin-top: 0.25rem;
    }
    .step-img {
        max-width: 320px;
        max-height: 220px;
        border: 1px solid var(--border);
        border-radius: 6px;
        display: block;
    }
</style>
