<script>
    // One tool call in the activity timeline, rendered by TYPE (the
    // agex-studio EventDetail idiom): terminal commands as a prompt
    // block, python highlighted, file writes highlighted by extension,
    // file edits as a computed line diff, test_app runs with their
    // verdict. Structured args come from the server; legacy string
    // args fall back to the generic view.
    import { fileUrl } from './api.js'
    import { highlightCode } from './markdown.js'
    import { viewFile } from './viewer.svelte.js'
    import Diff from './Diff.svelte'
    import { stepLine, toolArgs } from './activity.js'

    let { tool, session } = $props()

    // One quiet line ("Ran `pwd && ls`", "Edited app/app.jsx", "Tested
    // the app · passed · 6 steps"); the renderers below are what it
    // opens to.
    let open = $state(false)
    const line = $derived(stepLine(tool))
    // What the line says it carries, while folded: a test run's
    // screenshots, any other step's images. view_image's detail is the
    // image itself, so its line says nothing more.
    const pictures = $derived.by(() => {
        const n = tool.name === 'view_image' ? 0 : (tool.images?.length ?? 0)
        if (!n) return ''
        const noun = tool.name === 'test_app' ? 'screenshot' : 'image'
        return `· ${n} ${noun}${n === 1 ? '' : 's'}`
    })

    const args = $derived(toolArgs(tool))

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
    <button
        class="act-line step-line"
        class:open
        class:act-live={tool.running}
        onclick={() => (open = !open)}
    >
        <span class="act-verb">{line.verb}</span>
        {#if line.subject}<span class="act-rest" class:code={line.code}>{line.subject}</span
            >{/if}
        {#if pictures}<span class="act-rest">{pictures}</span>{/if}
        <span class="act-chev">⌄</span>
    </button>
    {#if open}
    <div class="act-body step-detail">

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
        <Diff old={args.old_string ?? ''} new={args.new_string ?? ''} />
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
    {/if}
</div>

<style>
    .step-line {
        display: flex;
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
