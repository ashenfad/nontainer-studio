// How a delegate is spoken of in the shell: its short name, the gist
// of its answer, and how long its branch has before the sweep.

/** `boss.scout` under `boss` reads as `scout`, the handle its parent
 * gave it, which is all an agent-forked session is called. */
export function leaf(child, parent) {
    return parent && child.startsWith(parent + '.') ? child.slice(parent.length + 1) : child
}

/** The first line of what a delegate said, as plain text: the gist a
 * card shows before it is opened. The answer reaches its parent behind
 * a bracketed provenance header, which is for the agent, not the gist. */
export function answerPreview(text) {
    const lines = (text ?? '').split('\n')
    if (lines[0]?.startsWith('[delegate')) lines.shift()
    for (const raw of lines) {
        const line = raw
            .replace(/^\s*(#+|>|[-*+]|\d+\.)\s+/, '')
            .replace(/[*_`]/g, '')
            .trim()
        if (line) return line
    }
    return ''
}

/** What became of a delegate, in words: "answered", "failed", "ran
 * out of turns". */
export function said(status) {
    if (!status) return 'answered'
    return status === 'capped' ? 'ran out of turns' : status
}

/** How long until a branch that expires at `expires` (epoch seconds)
 * may be swept, as "23h", "40m" or "soon"; null when nothing expires
 * it. Rounded down, so it never promises more time than there is. */
export function expiresIn(expires, now = Date.now() / 1000) {
    if (expires == null) return null
    const mins = Math.floor((expires - now) / 60)
    if (mins < 1) return 'soon'
    if (mins < 60) return `${mins}m`
    const hours = Math.floor(mins / 60)
    return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`
}

/** How long a delegate worked, "45s" or "3m 12s"; null when the row
 * does not say (no job table survived a restart) or it took no time
 * worth saying. */
export function workedFor(row) {
    if (!row?.started || !row?.finished) return null
    const s = Math.round(row.finished - row.started)
    if (s < 1) return null
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}
