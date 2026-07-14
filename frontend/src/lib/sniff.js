// Content sniffing for artifacts whose extension undersells them.

/** Does this (possibly truncated) .json body look like a plotly spec?
 * Agents reach for fig.write_json('/ui/x.json') — plain .json, so the
 * suffix-based renderer dispatch would show raw text. Full parse when
 * the body is intact; a structural prefix heuristic when a viewer
 * truncated it (PlotlyChart refetches the full URL itself anyway). */
export function looksLikePlotly(text) {
    if (!text) return false
    const trimmed = text.trimStart()
    if (trimmed[0] !== '{') return false
    try {
        const o = JSON.parse(trimmed)
        return Array.isArray(o.data) && !!o.layout && typeof o.layout === 'object'
    } catch {
        // write_json emits {"data": [...huge...], "layout": ...} — the
        // data key lands early, layout wherever the traces end
        return text.slice(0, 2000).includes('"data"') && text.includes('"layout"')
    }
}
