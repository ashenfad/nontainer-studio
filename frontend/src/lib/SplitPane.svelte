<script>
    // Chat | side-pane splitter: pointer-drag, persisted ratio, and a
    // transparent overlay during drag so the preview iframe doesn't
    // swallow pointermove (the agex-studio lesson).
    let { left, right } = $props()

    const KEY = 'nontainer-studio-split'
    let ratio = $state(
        Math.min(0.75, Math.max(0.25, parseFloat(localStorage.getItem(KEY)) || 0.5)),
    )
    let dragging = $state(false)
    let container = $state(null)

    function down(e) {
        e.preventDefault()
        dragging = true
        const move = (ev) => {
            const rect = container.getBoundingClientRect()
            ratio = Math.min(
                0.8,
                Math.max(0.2, (ev.clientX - rect.left) / rect.width),
            )
        }
        const up = () => {
            dragging = false
            localStorage.setItem(KEY, String(ratio))
            window.removeEventListener('pointermove', move)
            window.removeEventListener('pointerup', up)
            // Plotly and friends relayout on resize
            window.dispatchEvent(new Event('resize'))
        }
        window.addEventListener('pointermove', move)
        window.addEventListener('pointerup', up)
    }
</script>

<div class="split" bind:this={container}>
    <div class="pane" style="flex: {ratio} 1 0">
        {@render left()}
    </div>
    <div
        class="handle"
        class:dragging
        onpointerdown={down}
        role="separator"
        aria-orientation="vertical"
        tabindex="-1"
    ></div>
    <div class="pane" style="flex: {1 - ratio} 1 0">
        {@render right()}
    </div>
    {#if dragging}<div class="drag-shield"></div>{/if}
</div>

<style>
    .split {
        display: flex;
        flex: 1;
        min-height: 0;
        /* a flex item's min-width defaults to its content width — one
           long unwrapped pre line would inflate the whole split past
           the viewport instead of scrolling inside its block */
        min-width: 0;
        position: relative;
    }
    .pane {
        display: flex;
        flex-direction: column;
        min-width: 0;
        min-height: 0;
    }
    .handle {
        width: 5px;
        cursor: col-resize;
        background: var(--border);
        flex-shrink: 0;
        transition: background 0.15s;
    }
    .handle:hover,
    .handle.dragging {
        background: var(--accent);
    }
    .drag-shield {
        position: absolute;
        inset: 0;
        z-index: 50;
        cursor: col-resize;
    }
</style>
