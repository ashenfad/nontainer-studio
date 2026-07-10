<script>
    // Per-session model picker. Providers come from the server's env
    // (GET /api/models) — the browser chooses models, never touches
    // keys. Switching rebuilds the agent server-side; chat memory
    // carries over (it's keyed by session, not by agent).
    import { api } from './api.js'
    import { catalog, rail, refreshSessions } from './runtime.svelte.js'

    let { rt } = $props()

    let custom = $state(false)
    let customText = $state('')

    const current = $derived(
        rail.sessions.find((s) => s.name === rt.name)?.model ?? catalog.default,
    )

    // the current spec might be outside the curated lists (free-text)
    const known = $derived(
        catalog.providers.some((p) =>
            p.models.some((m) => `${p.name}:${m}` === current),
        ) || current === 'dummy',
    )

    async function set(spec) {
        if (!spec || spec === current) return
        try {
            await api(`/api/sessions/${rt.name}/model`, { model: spec })
            await refreshSessions()
        } catch (e) {
            rt.messages.push({ role: 'error', text: e.message })
        }
        custom = false
    }

    function onSelect(e) {
        const v = e.target.value
        if (v === '::custom') {
            custom = true
            customText = current ?? ''
            e.target.value = current ?? ''
        } else set(v)
    }
</script>

{#if catalog.providers.length}
    {#if custom}
        <form
            class="custom"
            onsubmit={(e) => {
                e.preventDefault()
                set(customText.trim())
            }}
        >
            <input
                bind:value={customText}
                placeholder="provider:model (e.g. openrouter:qwen/qwen3.7-max)"
            />
            <button type="submit">set</button>
            <button type="button" onclick={() => (custom = false)}>×</button>
        </form>
    {:else}
        <select
            class="picker"
            value={current}
            onchange={onSelect}
            disabled={rt.busy}
            title="model for this session — switching keeps the conversation"
        >
            {#if current && !known}
                <option value={current}>{current}</option>
            {/if}
            {#each catalog.providers as p (p.name)}
                <optgroup label={p.name}>
                    {#each p.models.length ? p.models : [p.default] as m (m)}
                        <option value={p.name === 'dummy' ? 'dummy' : `${p.name}:${m}`}>
                            {p.name}:{m}
                        </option>
                    {/each}
                </optgroup>
            {/each}
            <option value="::custom">custom…</option>
        </select>
    {/if}
{/if}

<style>
    .picker {
        max-width: 220px;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text-muted);
        font-size: 0.7rem;
        padding: 0.2rem 0.35rem;
        cursor: pointer;
        outline: none;
    }
    .picker:hover:not(:disabled) {
        color: var(--text);
    }
    .picker:disabled {
        opacity: 0.5;
        cursor: default;
    }
    .custom {
        display: flex;
        gap: 0.3rem;
        align-items: center;
    }
    .custom input {
        width: 280px;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font-family: var(--font-mono);
        font-size: 0.7rem;
        padding: 0.25rem 0.45rem;
        outline: none;
    }
    .custom button {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 5px;
        font-size: 0.68rem;
        padding: 0.15rem 0.45rem;
        cursor: pointer;
    }
    .custom button:hover {
        color: var(--text);
    }
</style>
