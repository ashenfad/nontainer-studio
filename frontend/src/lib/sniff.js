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

/** Is this one renderable card item?
 *
 * Mirrors nontainer's `_is_callout` / `_is_stat` deliberately: two
 * different notions of "is this a card" between server and client is
 * how the two drift. A callout must be TAGGED; a stat is any object
 * carrying label + value.
 */
export function isCardItem(o) {
    if (!o || typeof o !== 'object' || Array.isArray(o)) return false
    if (o.type === 'callout' && ('title' in o || 'body' in o)) return true
    return 'label' in o && 'value' in o
}

/** Does this .json body hold cards that lost their `.cards.json` suffix?
 *
 * nontainer < 0.4.1 rendered a LONE tagged callout as raw JSON — the
 * item was perfect, only the list wrapper was missing. 0.4.1 fixed the
 * writer, but artifacts already committed keep their `.json` path, and
 * kind is derived from the suffix. This is the display-side rescue, the
 * same trick `looksLikePlotly` plays for `fig.write_json('/ui/x.json')`.
 *
 * A BARE object must be a tagged callout. `{label, value}` on its own
 * is too ordinary a shape to claim — the server declines to adopt it
 * for the same reason, and a client that disagreed would render
 * someone's config as a stat tile.
 */
export function looksLikeCards(text) {
    if (!text) return false
    const trimmed = text.trimStart()
    if (trimmed[0] !== '{' && trimmed[0] !== '[') return false
    let o
    try {
        o = JSON.parse(trimmed)
    } catch {
        return false // cards payloads are small; a truncated one is not worth guessing
    }
    const all = (a) => Array.isArray(a) && a.length > 0 && a.every(isCardItem)
    if (all(o)) return true
    if (o && typeof o === 'object' && all(o.items)) return true
    return !!o && o.type === 'callout' && ('title' in o || 'body' in o)
}
