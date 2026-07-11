<script>
    import { renderMarkdown } from './markdown.js'
    import { viewFile } from './viewer.svelte.js'

    let { text = '' } = $props()
    const html = $derived(renderMarkdown(text))

    // Workspace paths the agent links in prose ("see [report](/report.md)")
    // open the file modal instead of navigating. Raw href, not the
    // resolved URL: root-relative means "a file in this workspace".
    function onclick(e) {
        const a = e.target.closest('a')
        if (!a) return
        const href = a.getAttribute('href') ?? ''
        if (href.startsWith('/') && !href.startsWith('//')) {
            e.preventDefault()
            viewFile(href)
        }
    }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<!-- eslint-disable-next-line svelte/no-at-html-tags — DOMPurify'd -->
<div class="markdown" {onclick}>{@html html}</div>
