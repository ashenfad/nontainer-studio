<script>
    // What is in the live app tree and not yet in a version: the answer
    // to the count the publish button carries. The rows come off the
    // session's apps row, which is refreshed on every version tick, so
    // the list costs nothing and follows the agent as it writes; the
    // two sides of a file are fetched only while its row is open.
    import { api } from './api.js'
    import { viewFile } from './viewer.svelte.js'
    import Diff from './Diff.svelte'

    // `readonly` is a delegate: it may read the diff (a diff commits
    // nothing) but the publish route refuses it, so the button goes.
    let { rt, readonly = false } = $props()

    // Files the studio publishes live here; the empty-state count says
    // how many of them a first publish would put behind a URL.
    const APP_DIR = '/workspace/app/'

    const app = $derived(rt.apps[0] ?? null)
    const changed = $derived(app?.changed_since ?? null)
    // the version the rows were measured against: each row is a file
    // whose bytes differ from this one
    const newest = $derived(changed?.since ?? null)
    const files = $derived(changed?.files ?? [])

    // The rows exist for ONE baseline — nothing counts the files that
    // differ from an older version — so there is no picker over the
    // whole version list; offering one would label rows with a
    // baseline they were not measured against. What the choice does
    // cover is the case a person actually hits: the URL is behind, and
    // "how does the live tree differ from what visitors get" is a
    // question about the served version. Only the diff of an opened
    // row moves; the rows stay the newest version's.
    let against = $state(null) // null = the newest version
    const behind = $derived(
        app && newest && app.current !== newest ? app.current : null,
    )
    const baseline = $derived(against ?? newest)

    // a session switch, or a rollback that catches the pointer up,
    // leaves a choice that no longer names anything
    $effect(() => {
        if (against && against !== behind) against = null
    })

    let open = $state([]) // paths whose diff is showing
    let diffs = $state({}) // path -> {data, error}; absent = in flight

    function toggle(path) {
        open = open.includes(path)
            ? open.filter((p) => p !== path)
            : [...open, path]
    }

    // The apps row is this tab's whole listing, and the preview pane is
    // not necessarily mounted to keep it fresh, so refresh it here too.
    $effect(() => {
        void rt.name
        void rt.version
        rt.loadApps()
    })

    // An open row follows the agent: every version tick refetches its
    // two sides, so what is on screen is the edit as it stands rather
    // than as it stood when the row was opened. A path that has left
    // the list is skipped — its file is back in step with the version,
    // and asking for it would only 404. The previous answer stays on
    // screen until the new one lands, so a tick doesn't blank the row.
    $effect(() => {
        void rt.version
        const session = rt.name
        const token = app?.token
        const since = baseline
        const wanted = open.filter((p) => files.some((f) => f.path === p))
        if (!token || wanted.length === 0) return
        let stale = false
        for (const path of wanted) {
            const q = new URLSearchParams({ path })
            if (since) q.set('since', since)
            api(`/api/sessions/${session}/apps/${token}/changes/file?${q}`)
                .then((d) => {
                    if (!stale) diffs[path] = { data: d, error: null }
                })
                .catch((e) => {
                    if (!stale) diffs[path] = { data: null, error: e.message }
                })
        }
        return () => (stale = true)
    })

    // Only asked with no app published: once there is one, the apps row
    // answers everything this pane shows and a file listing would be a
    // second fetch per tick for nothing.
    let firstCount = $state(null)
    $effect(() => {
        if (app) return
        void rt.version
        const session = rt.name
        let stale = false
        api(`/api/sessions/${session}/files`)
            .then((d) => {
                if (!stale)
                    firstCount = d.files.filter((p) =>
                        p.startsWith(APP_DIR),
                    ).length
            })
            .catch(() => {
                if (!stale) firstCount = null
            })
        return () => (stale = true)
    })

    let publishing = $state(false)
    let error = $state(null)
    // The route refuses a publish while a turn runs (the version would
    // race the commit), so the button waits rather than offering a
    // click whose only outcome is a refusal.
    const held = $derived(rt.busy || publishing)

    // No name field here: naming a version lives on the preview bar's
    // caret, and one click from this tab means "save what this list
    // shows" — the server picks the vN.
    async function publish() {
        error = null
        publishing = true
        try {
            await rt.publish({})
        } catch (e) {
            error = e.message
        } finally {
            publishing = false
        }
    }

    const WORDS = { added: 'added', modified: 'changed', removed: 'removed' }

    // Paths arrive absolute (that is what the file endpoints take); the
    // app directory is the same for every row, so it is noise here. The
    // first `/app/` is the boundary, not the last: a subdirectory of
    // the app called `app` is part of the name that stays.
    const rel = (path) =>
        path.startsWith(APP_DIR)
            ? path.slice(APP_DIR.length)
            : path.replace(/^.*?\/app\//, '')

    function size(n) {
        if (n == null) return ''
        if (n < 1024) return `${n} B`
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
        return `${(n / 1024 / 1024).toFixed(1)} MB`
    }
</script>

<div class="changes">
    <div class="head">
        {#if !app}
            <span class="what"
                >nothing published yet — publishing puts {firstCount ?? '…'}
                file{firstCount === 1 ? '' : 's'} behind a URL</span
            >
        {:else}
            <span class="what">changes since <code>{newest}</code></span>
        {/if}
        <span class="grow"></span>
        {#if !readonly}
            <button
                class="small accent"
                onclick={publish}
                disabled={held}
                title={rt.busy ? 'waits for the turn' : 'add a version'}
                >publish</button
            >
        {/if}
    </div>

    {#if behind}
        <div class="note">
            the link serves <code>{app.current}</code>
            <button class="link" onclick={() => (against = against ? null : behind)}>
                {against
                    ? `diff against ${newest} again`
                    : `diff against ${behind} instead`}
            </button>
        </div>
    {/if}
    {#if against}
        <div class="note">
            the rows are the files that differ from {newest}; each diff below is
            against {against}
        </div>
    {/if}
    {#if error}
        <div class="err">{error}</div>
    {/if}

    <div class="rows">
        {#each files as f (f.path)}
            <div class="row">
                <button class="row-head" onclick={() => toggle(f.path)}>
                    <span class="status {f.status}">{WORDS[f.status] ?? f.status}</span>
                    <span class="path">{rel(f.path)}</span>
                    <span class="size">{size(f.size)}</span>
                </button>
                {#if open.includes(f.path)}
                    {@const d = diffs[f.path]}
                    <div class="body">
                        {#if !d}
                            <div class="hint">loading…</div>
                        {:else if d.error}
                            <div class="err">{d.error}</div>
                        {:else if d.data.binary}
                            <div class="hint">
                                binary or large — {size(d.data.size)}
                                {#if d.data.status === 'removed'}
                                    · deleted
                                {:else}
                                    <button
                                        class="link"
                                        onclick={() => viewFile(f.path)}>view</button
                                    >
                                {/if}
                            </div>
                        {:else if d.data.status === 'unchanged'}
                            <div class="hint">unchanged since the list was drawn</div>
                        {:else}
                            <Diff old={d.data.old ?? ''} new={d.data.new ?? ''} />
                        {/if}
                    </div>
                {/if}
            </div>
        {/each}
        {#if app && files.length === 0}
            <div class="hint">nothing unpublished — the live app matches {newest}</div>
        {/if}
    </div>
</div>

<style>
    .changes {
        flex: 1;
        min-height: 0;
        display: flex;
        flex-direction: column;
    }
    .head {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.45rem 0.7rem;
        border-bottom: 1px solid var(--border);
        background: var(--surface);
        font-size: 0.75rem;
        flex-wrap: wrap;
    }
    .what {
        color: var(--text-muted);
    }
    .grow {
        flex: 1;
    }
    .small {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.72rem;
        padding: 0.2rem 0.55rem;
        cursor: pointer;
    }
    .small:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .small:disabled {
        opacity: 0.4;
        cursor: default;
    }
    .small.accent {
        border-color: var(--accent);
        color: var(--accent);
    }
    .small.accent:hover:not(:disabled) {
        background: color-mix(in srgb, var(--accent) 15%, transparent);
    }
    .note {
        color: var(--text-muted);
        font-size: 0.72rem;
        padding: 0.3rem 0.7rem;
        border-bottom: 1px solid var(--border);
    }
    .err {
        color: var(--error);
        font-size: 0.72rem;
        padding: 0.3rem 0.7rem;
    }
    code {
        font-family: var(--font-mono);
        color: var(--text);
    }
    .rows {
        flex: 1;
        overflow-y: auto;
        padding: 0.4rem;
    }
    .row {
        border-radius: 5px;
    }
    .row-head {
        width: 100%;
        display: flex;
        align-items: baseline;
        gap: 0.5rem;
        background: none;
        border: none;
        border-radius: 5px;
        padding: 0.3rem 0.55rem;
        cursor: pointer;
        text-align: left;
        font-family: var(--font-mono);
        font-size: 0.75rem;
        color: var(--text-muted);
    }
    .row-head:hover {
        background: var(--surface-hover);
        color: var(--text);
    }
    .status {
        font-size: 0.68rem;
        flex-shrink: 0;
        width: 3.9rem;
    }
    .status.added {
        color: var(--success);
    }
    .status.modified {
        color: var(--accent);
    }
    .status.removed {
        color: var(--error);
    }
    .path {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .size {
        flex-shrink: 0;
        font-size: 0.68rem;
    }
    .body {
        padding: 0 0.55rem 0.4rem;
    }
    .hint {
        color: var(--text-muted);
        font-size: 0.72rem;
        padding: 0.25rem 0;
    }
    .link {
        background: none;
        border: none;
        padding: 0;
        color: var(--accent);
        font: inherit;
        font-size: 0.72rem;
        cursor: pointer;
        text-decoration: underline;
    }
</style>
