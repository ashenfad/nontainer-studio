<script>
    // What stands where the composer stands when the open session is a
    // delegate: a window, not a lever. The parent agent writes this
    // session's prompts, judges its branch and integrates it — so there
    // is nothing here to type into, only what it is (status, whose it
    // is, and the way back to that parent), when the retention sweep
    // may take its branch and the `keep` that exempts it, and a way
    // further down when it delegated in turn.
    import { expiresIn, leaf } from './delegates.js'
    import { loadDelegates, setDelegateKept } from './runtime.svelte.js'

    let { name, delegate, parentLabel, onSwitch, onRefresh } = $props()

    const left = $derived(expiresIn(delegate.expires))

    // No local copy of the row: `delegate` is re-read as the session's
    // status moves, and a copy kept here would freeze a running
    // delegate on the status it wore when the keep was clicked. The
    // toggle writes, then asks for the row again.
    let error = $state('')
    let saving = $state(false)
    let kids = $state([])

    $effect(() => {
        const who = name
        kids = []
        error = ''
        loadDelegates(who)
            .then((rows) => {
                if (name === who) kids = rows
            })
            .catch((e) => {
                if (name === who) error = e.message
            })
    })

    async function toggleKeep() {
        error = ''
        saving = true
        try {
            await setDelegateKept(delegate.parent, name, !delegate.kept)
            await onRefresh()
        } catch (e) {
            error = e.message
        } finally {
            saving = false
        }
    }

    function ago(seconds) {
        if (!seconds) return ''
        const mins = Math.round((Date.now() / 1000 - seconds) / 60)
        if (mins < 1) return 'just now'
        if (mins < 60) return `${mins}m ago`
        const hours = Math.round(mins / 60)
        if (hours < 24) return `${hours}h ago`
        return `${Math.round(hours / 24)}d ago`
    }
</script>

<div class="delegate-bar">
    <div class="line">
        <span
            class="status {delegate.status}"
            title={delegate.status === 'expired'
                ? 'the retention sweep took this branch; its answer is gone'
                : delegate.known
                  ? ''
                  : 'no job table survived the last restart — this is what the record knows'}
            >{delegate.status}</span
        >
        <button class="back" onclick={() => onSwitch(delegate.parent)}
            >← back to <b>{parentLabel ?? delegate.parent}</b></button
        >
        <span class="when">{ago(delegate.touched)}</span>
        <span class="grow"></span>
        <span class="readonly" title="the parent agent drives this session; you can read it, not type into it"
            >read-only</span
        >
        {#if delegate.kept}
            <button
                class="keep on"
                disabled={saving}
                title="kept: the retention sweep leaves this delegate alone. Click to let it expire again, a while after anyone last dealt with it."
                onclick={toggleKeep}>kept</button
            >
        {:else if left}
            <span class="expiry">expires in {left} ·</span>
            <button
                class="keep"
                disabled={saving}
                title="a delegate's branch and transcript are swept a while after anyone last dealt with it; keep exempts this one"
                onclick={toggleKeep}>keep</button
            >
        {/if}
    </div>
    {#if error}
        <div class="note error">{error}</div>
    {/if}
    {#if kids.length}
        <div class="sub-delegates">
            <span class="note">it delegated:</span>
            {#each kids as k (k.name)}
                <button class="sub-delegate" title={k.name} onclick={() => onSwitch(k.name)}
                    >{leaf(k.name, name)} <span class="sub-status">{k.status}</span></button
                >
            {/each}
        </div>
    {/if}
</div>

<style>
    .delegate-bar {
        flex-shrink: 0;
        padding: 0.6rem 1rem 0.85rem;
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
    }
    .line {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.5rem;
        background: var(--input-bg);
        border: 1px dashed var(--border);
        border-radius: 14px;
        padding: 0.5rem 0.75rem;
        font-size: 0.76rem;
        color: var(--text-muted);
    }
    .grow {
        flex: 1;
    }
    .status {
        text-transform: lowercase;
        color: var(--text);
        font-weight: 600;
    }
    .status.running {
        color: var(--accent);
    }
    .status.expired {
        color: var(--warning);
    }
    .back {
        background: none;
        border: none;
        padding: 0.1rem 0.45rem;
        border-radius: 6px;
        font-family: inherit;
        font-size: inherit;
        color: var(--text-muted);
        cursor: pointer;
        white-space: nowrap;
    }
    .back b {
        color: var(--text);
        font-weight: 600;
    }
    .back:hover {
        background: color-mix(in srgb, var(--accent) 16%, transparent);
    }
    .when,
    .readonly,
    .expiry {
        font-size: 0.7rem;
        white-space: nowrap;
    }
    .keep {
        background: none;
        border: none;
        color: var(--text-muted);
        font-family: inherit;
        font-size: 0.7rem;
        padding: 0.1rem 0.45rem;
        border-radius: 999px;
        cursor: pointer;
        flex-shrink: 0;
    }
    .keep:hover:not(:disabled) {
        color: var(--accent);
    }
    .keep.on {
        color: var(--accent);
        background: color-mix(in srgb, var(--accent) 16%, transparent);
    }
    .keep:disabled {
        cursor: default;
        opacity: 0.5;
    }
    .sub-delegates {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.35rem;
        padding: 0 0.3rem;
    }
    .note {
        color: var(--text-muted);
        font-size: 0.7rem;
    }
    .note.error {
        color: var(--error);
        padding: 0 0.3rem;
    }
    .sub-delegate {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 999px;
        color: var(--text-muted);
        font-family: inherit;
        font-size: 0.7rem;
        padding: 0.1rem 0.6rem;
        cursor: pointer;
    }
    .sub-delegate:hover {
        color: var(--text);
        border-color: var(--text-muted);
    }
    .sub-status {
        opacity: 0.7;
    }
</style>
