<script>
    // Per-session model picker: a quiet toolbar button opening an
    // upward menu (the agex-studio composer idiom). Providers come
    // from the server's env (GET /api/models) — the browser chooses
    // models, never touches keys. Switching rebuilds the agent
    // server-side; chat memory carries over (keyed by session).
    import { api } from './api.js'
    import { catalog, rail, refreshSessions } from './runtime.svelte.js'

    let { rt } = $props()

    let open = $state(false)
    let custom = $state(false)
    let customText = $state('')

    const current = $derived(
        rail.sessions.find((s) => s.name === rt.name)?.model ?? catalog.default,
    )
    const label = $derived.by(() => {
        if (!current) return 'model'
        const [, model] = current.split(/:(.*)/)
        return model || current
    })

    $effect(() => {
        if (!open) return
        function onEsc(e) {
            if (e.key === 'Escape') {
                open = false
                custom = false
            }
        }
        document.addEventListener('keydown', onEsc)
        return () => document.removeEventListener('keydown', onEsc)
    })

    async function set(spec) {
        open = false
        custom = false
        if (!spec || spec === current) return
        try {
            await api(`/api/sessions/${rt.name}/model`, { model: spec })
            await refreshSessions()
        } catch (e) {
            rt.messages.push({ role: 'error', text: e.message })
        }
    }
</script>

{#if catalog.providers.length}
    <div class="model-wrap">
        <button
            class="model-btn"
            onclick={() => (open = !open)}
            title={`model for this session (${current ?? 'default'}) — switching keeps the conversation`}
            aria-expanded={open}
            disabled={rt.busy}
        >
            <span class="model-label">{label}</span>
            <svg
                width="10"
                height="10"
                viewBox="0 0 12 12"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
            >
                <polyline points="3 5 6 8 9 5"></polyline>
            </svg>
        </button>
        {#if open}
            <button
                type="button"
                class="menu-backdrop"
                aria-label="Close menu"
                tabindex="-1"
                onclick={() => {
                    open = false
                    custom = false
                }}
            ></button>
            <div class="model-menu" role="menu">
                {#each catalog.providers as p (p.name)}
                    <div class="menu-provider">{p.name}</div>
                    {#each p.models.length ? p.models : [p.default] as m (m)}
                        {@const spec = p.name === 'dummy' ? 'dummy' : `${p.name}:${m}`}
                        <button
                            class="menu-item"
                            class:selected={spec === current}
                            role="menuitem"
                            onclick={() => set(spec)}
                        >
                            {m}
                        </button>
                    {/each}
                {/each}
                {#if custom}
                    <form
                        class="custom-row"
                        onsubmit={(e) => {
                            e.preventDefault()
                            set(customText.trim())
                        }}
                    >
                        <!-- svelte-ignore a11y_autofocus -->
                        <input
                            autofocus
                            bind:value={customText}
                            placeholder="provider:model"
                        />
                        <button type="submit">set</button>
                    </form>
                {:else}
                    <button
                        class="menu-item custom-item"
                        role="menuitem"
                        onclick={() => {
                            custom = true
                            customText = current ?? ''
                        }}
                    >
                        custom…
                    </button>
                {/if}
                <div class="menu-footnote">
                    switching keeps this session's conversation
                </div>
            </div>
        {/if}
    </div>
{/if}

<style>
    .model-wrap {
        position: relative;
    }
    .model-btn {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 0.72rem;
        padding: 0.25rem 0.45rem;
        border-radius: 6px;
        cursor: pointer;
        max-width: 240px;
    }
    .model-btn:hover:not(:disabled) {
        color: var(--text);
        background: var(--surface-hover);
    }
    .model-btn:disabled {
        opacity: 0.5;
        cursor: default;
    }
    .model-label {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .menu-backdrop {
        position: fixed;
        inset: 0;
        background: none;
        border: none;
        z-index: 90;
        cursor: default;
    }
    .model-menu {
        position: absolute;
        bottom: calc(100% + 0.4rem);
        right: 0;
        z-index: 91;
        min-width: 240px;
        max-height: 340px;
        overflow-y: auto;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
        padding: 0.3rem;
        display: flex;
        flex-direction: column;
    }
    /* the column scrolls; items must never be squished to fit */
    .model-menu > * {
        flex-shrink: 0;
    }
    .menu-provider {
        font-size: 0.62rem;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        color: var(--text-muted);
        padding: 0.4rem 0.55rem 0.15rem;
    }
    .menu-item {
        background: none;
        border: none;
        color: var(--text);
        font-size: 0.78rem;
        font-family: var(--font-mono);
        text-align: left;
        padding: 0.32rem 0.55rem;
        border-radius: 6px;
        cursor: pointer;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .menu-item:hover {
        background: var(--surface-hover);
    }
    .menu-item.selected {
        color: var(--accent);
    }
    .custom-item {
        font-family: var(--font-body);
        color: var(--text-muted);
        border-top: 1px solid var(--border);
        border-radius: 0 0 6px 6px;
        margin-top: 0.25rem;
    }
    .custom-row {
        display: flex;
        gap: 0.3rem;
        padding: 0.35rem 0.45rem;
        border-top: 1px solid var(--border);
        margin-top: 0.25rem;
    }
    .custom-row input {
        flex: 1;
        background: var(--input-bg);
        border: 1px solid var(--border);
        border-radius: 6px;
        color: var(--text);
        font-family: var(--font-mono);
        font-size: 0.72rem;
        padding: 0.25rem 0.45rem;
        outline: none;
        min-width: 0;
    }
    .custom-row button {
        background: none;
        border: 1px solid var(--border);
        color: var(--text-muted);
        border-radius: 6px;
        font-size: 0.7rem;
        padding: 0.2rem 0.5rem;
        cursor: pointer;
    }
    .custom-row button:hover {
        color: var(--text);
    }
    .menu-footnote {
        font-size: 0.64rem;
        color: var(--text-muted);
        padding: 0.35rem 0.55rem 0.25rem;
    }
</style>
