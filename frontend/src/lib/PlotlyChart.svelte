<script>
    // Renders a *.plotly.json spec artifact. Plotly stays a lazy CDN
    // load (~3MB) — not worth bundling into the shell for a feature
    // only some sessions use. v3 to match plotly.py 6.x's paired
    // renderer major (v2 is EOL; late v2 does decode 6.x's bdata
    // typed arrays, but staying on the generator's major is safer).
    let { url } = $props()

    let node = $state(null)
    let failed = $state(null)

    async function plotly() {
        if (!window.__plotlyPromise) {
            window.__plotlyPromise = import(
                /* @vite-ignore */ 'https://esm.sh/plotly.js-dist-min@3'
            ).then((m) => m.default)
        }
        return window.__plotlyPromise
    }

    $effect(() => {
        if (!node) return
        let dead = false
        const target = node
        Promise.all([plotly(), fetch(url).then((r) => r.json())])
            .then(([Plotly, spec]) => {
                if (dead) return
                // spec tier: WE render, so WE theme — transparent
                // background, shell-appropriate font color
                const layout = {
                    ...spec.layout,
                    paper_bgcolor: 'rgba(0,0,0,0)',
                    plot_bgcolor: 'rgba(0,0,0,0)',
                    font: { ...(spec.layout?.font || {}), color: '#ccc' },
                }
                Plotly.newPlot(target, spec.data, layout, {
                    responsive: true,
                    displaylogo: false,
                })
            })
            .catch((e) => !dead && (failed = e.message))
        return () => {
            dead = true
            target.replaceChildren?.()
        }
    })
</script>

{#if failed}
    <div class="plot-error">plot failed: {failed}</div>
{:else}
    <div class="plotly-fig" bind:this={node}></div>
{/if}

<style>
    .plotly-fig {
        width: 100%;
        min-height: 320px;
        margin: 0.5rem 0;
        border: 1px solid var(--border);
        border-radius: 8px;
        overflow: hidden;
        background: rgba(255, 255, 255, 0.02);
    }
    .plot-error {
        color: var(--error);
        font-size: 0.8rem;
        padding: 0.4rem 0;
    }
</style>
