// Line diff for rendering an edit: old-then-new reading order for
// replacements. Ported from agex-studio's event-utils, with a bound.
//
// The alignment is a longest-common-subsequence table of
// (old lines + 1) × (new lines + 1) Int32 cells, so two sides of ten
// thousand short lines each would ask for 400 MB and freeze the tab on
// a click. Two things keep it in bounds. Lines both sides share at the
// start and at the end are stripped before the table is built — the
// everyday edit is a few lines somewhere in a long file, and that alone
// brings it down to the edit's own size. What is left is aligned line
// by line only while the table fits MAX_CELLS; past that the middle is
// shown as one block removed and one block added. A coarser answer
// than the aligned one, and the honest one, where the alternative is a
// page that does not come back.

export const MAX_CELLS = 4_000_000

/** `{lines, bounded}`: `lines` are `{type: 'context'|'removed'|'added',
 * text}` in reading order; `bounded` says the middle was too large to
 * align and is shown as a removed block followed by an added block. */
export function lineDiff(aText, bText) {
    // an empty side has no lines, not one empty line: a file being
    // added reads as its lines arriving, without a blank line leaving
    const a = aText === '' ? [] : aText.split('\n')
    const b = bText === '' ? [] : bText.split('\n')
    let head = 0
    while (head < a.length && head < b.length && a[head] === b[head]) head++
    let tail = 0
    while (
        tail < a.length - head &&
        tail < b.length - head &&
        a[a.length - 1 - tail] === b[b.length - 1 - tail]
    )
        tail++
    const midA = a.slice(head, a.length - tail)
    const midB = b.slice(head, b.length - tail)
    const bounded = (midA.length + 1) * (midB.length + 1) > MAX_CELLS
    const lines = []
    for (let i = 0; i < head; i++) lines.push({ type: 'context', text: a[i] })
    if (bounded) {
        for (const text of midA) lines.push({ type: 'removed', text })
        for (const text of midB) lines.push({ type: 'added', text })
    } else {
        for (const line of lcs(midA, midB)) lines.push(line)
    }
    for (let i = a.length - tail; i < a.length; i++)
        lines.push({ type: 'context', text: a[i] })
    return { lines, bounded }
}

function lcs(a, b) {
    const m = a.length
    const n = b.length
    const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1))
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (a[i - 1] === b[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1
            else dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1])
        }
    }
    const out = []
    let i = m
    let j = n
    while (i > 0 && j > 0) {
        if (a[i - 1] === b[j - 1]) {
            out.push({ type: 'context', text: a[i - 1] })
            i--
            j--
        } else if (dp[i - 1][j] > dp[i][j - 1]) {
            out.push({ type: 'removed', text: a[i - 1] })
            i--
        } else {
            out.push({ type: 'added', text: b[j - 1] })
            j--
        }
    }
    while (i > 0) out.push({ type: 'removed', text: a[--i] })
    while (j > 0) out.push({ type: 'added', text: b[--j] })
    return out.reverse()
}
