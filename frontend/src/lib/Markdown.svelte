<script>
    import { renderMarkdown } from './markdown.js'

    let { text = '' } = $props()
    const html = $derived(renderMarkdown(text))
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags — DOMPurify'd -->
<div class="markdown">{@html html}</div>
