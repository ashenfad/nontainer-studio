// marked + DOMPurify + highlight.js, bundled (no CDN at runtime for
// the shell itself — agents' apps are a different tier).
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import markdown from 'highlight.js/lib/languages/markdown'

hljs.registerLanguage('python', python)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('markdown', markdown)

const ALIASES = { js: 'javascript', ts: 'typescript', py: 'python', sh: 'bash', shell: 'bash' }

marked.setOptions({
    breaks: true,
    highlight: undefined, // we drive hljs via the renderer below
})

const renderer = {
    code({ text, lang }) {
        const name = ALIASES[lang] || lang
        const body = hljs.getLanguage(name)
            ? hljs.highlight(text, { language: name }).value
            : escapeHtml(text)
        return `<pre><code class="hljs">${body}</code></pre>`
    },
}
marked.use({ renderer })

function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

export function renderMarkdown(text) {
    return DOMPurify.sanitize(marked.parse(text ?? ''))
}

export function highlightCode(text, lang = 'python') {
    const name = ALIASES[lang] || lang
    if (!hljs.getLanguage(name)) return escapeHtml(text)
    return hljs.highlight(text, { language: name }).value
}
