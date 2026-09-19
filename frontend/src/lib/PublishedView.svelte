<script>
    // A published app in the side pane: the app's own URL in the frame
    // and the version strip beneath it. Two callers render this — the
    // preview's `published` mode and the rail's app view — and they
    // differ only in the bar above it, so the frame, its sandbox and
    // the strip live here once. The strip sits under the frame on
    // purpose: pointing the URL at another version or deleting one
    // shows its result in the frame beside it.
    import PublishedPanel from './PublishedPanel.svelte'

    // `tick` is whatever else should force a reload — a manual reload,
    // and in a live session the runtime's version counter.
    let { app, tick = 0, onSwitch, onChanged } = $props()

    // The app's URL is stable by design, so the CURRENT VERSION has to
    // be in the cache-busting key: making an older version current
    // changes what the URL serves without changing the URL, and a
    // keyed iframe with an identical src would keep showing the old one.
    const src = $derived(`${app.url}?v=${app.current}-${tick}`)
</script>

{#key src}
    <!-- allow-modals: agent apps use alert()/confirm() for error
         surfacing. Still NO allow-same-origin — the app stays an
         opaque origin, unable to reach the studio API. What makes the
         app's own assets load from an opaque origin is the CORS header
         the /apps mount adds (see cors_for_apps). -->
    <iframe title="published app" {src} sandbox="allow-scripts allow-forms allow-modals"
    ></iframe>
{/key}
<PublishedPanel {app} {onSwitch} {onChanged} />

<style>
    iframe {
        flex: 1;
        border: none;
        background: #fff;
        min-height: 0;
    }
</style>
