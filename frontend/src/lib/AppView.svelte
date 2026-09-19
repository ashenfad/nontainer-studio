<script>
    // A published app on its own, opened from the rail — no session
    // involved. That is the whole reason this view exists: an app
    // outlives the session that made it, so the ones whose origin is
    // deleted are reachable from nowhere else. The frame and the
    // version strip are the preview's published mode; only the bar
    // differs, because there is no live side to toggle to.
    import PublishedView from './PublishedView.svelte'
    import { published, refreshApps } from './runtime.svelte.js'

    let { token, onSwitch, onClose } = $props()

    let manual = $state(0)

    const app = $derived(published.apps.find((a) => a.token === token) ?? null)
</script>

<div class="app-view">
    <div class="bar">
        <span class="title">{app?.title ?? 'app'}</span>
        {#if app}
            <span class="serving">serving {app.current}</span>
        {/if}
        <span class="grow"></span>
        <button class="small" onclick={() => manual++}>reload</button>
        {#if app}
            <a class="small" href={app.url} target="_blank" rel="noopener">open ↗</a>
        {/if}
        <button class="small" onclick={onClose}>close</button>
    </div>
    {#if app}
        <PublishedView {app} tick={manual} {onSwitch} onChanged={refreshApps} />
    {:else}
        <div class="gone">
            <p>This app is no longer published.</p>
        </div>
    {/if}
</div>

<style>
    .app-view {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
    }
    .bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.45rem 0.7rem;
        border-bottom: 1px solid var(--border);
        background: var(--surface);
        font-size: 0.75rem;
        flex-wrap: wrap;
    }
    .title {
        color: var(--text);
        font-weight: 600;
    }
    .serving {
        color: var(--success);
        font-size: 0.66rem;
        border: 1px solid color-mix(in srgb, var(--success) 45%, transparent);
        border-radius: 999px;
        padding: 0 0.4rem;
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
        text-decoration: none;
    }
    .small:hover {
        color: var(--text);
        background: var(--surface-hover);
    }
    .gone {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--text-muted);
        font-size: 0.85rem;
    }
</style>
