// LCS line diff for file_edit rendering (ported from agex-studio's
// event-utils): old-then-new reading order for replacements.

export function lineDiff(aText, bText) {
    const a = aText.split('\n')
    const b = bText.split('\n')
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
