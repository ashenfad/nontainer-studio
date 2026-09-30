// How the transcript names the agent's work, in plain words: "Ran 2
// commands, edited 1 file", "Thought for 12s", "Worked for 3m". The
// components only lay these out; the wording lives here so the group
// line, the step lines and the turn summary say one thing the same way.

const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`

// A tool's step line: a verb, what it acted on, and whether that
// subject reads as code.
export function stepLine(tool) {
    const args = tool.args && typeof tool.args === 'object' ? tool.args : {}
    const first = (text) =>
        String(text ?? '')
            .split('\n')
            .find((line) => line.trim()) ?? ''
    switch (tool.name) {
        case 'terminal':
            return { verb: 'Ran', subject: first(args.command), code: true }
        case 'run_python':
            return { verb: 'Ran Python', subject: first(args.code), code: true }
        case 'file_write':
            return { verb: 'Wrote', subject: args.path ?? '', code: true }
        case 'file_edit':
            return { verb: 'Edited', subject: args.path ?? '', code: true }
        case 'view_image':
            return { verb: 'Viewed', subject: args.path ?? '', code: true }
        case 'test_app': {
            const result = typeof tool.result === 'string' ? tool.result : ''
            const verdict = result.startsWith('test_app: PASS')
                ? 'passed'
                : result.startsWith('test_app: FAIL')
                  ? 'failed'
                  : ''
            const steps = Array.isArray(args.actions) ? plural(args.actions.length, 'step') : ''
            return {
                verb: 'Tested the app',
                subject: [verdict, steps].filter(Boolean).join(' · '),
                code: false,
            }
        }
        case 'sessions':
            return { verb: 'Sessions', subject: args.action ?? '', code: false }
        default:
            return { verb: `Used ${tool.name}`, subject: '', code: false }
    }
}

// What a run of tools did, as phrases in the order the kinds first
// appear: [{verb: 'Ran', rest: '2 commands'}, {verb: 'edited', rest: '1 file'}].
// Files count once however often they were written.
export function groupPhrases(tools) {
    const order = []
    const seen = new Map()
    for (const t of tools) {
        if (!seen.has(t.name)) {
            seen.set(t.name, [])
            order.push(t.name)
        }
        seen.get(t.name).push(t)
    }
    const paths = (ts) =>
        new Set(ts.map((t) => (t.args && typeof t.args === 'object' ? t.args.path : null)))
            .size
    const phrase = (name, ts) => {
        const n = ts.length
        switch (name) {
            case 'terminal':
                return ['Ran', plural(n, 'command')]
            case 'run_python':
                return ['Ran', n === 1 ? 'Python' : `Python ${n} times`]
            case 'file_write':
                return ['Wrote', plural(paths(ts), 'file')]
            case 'file_edit':
                return ['Edited', plural(paths(ts), 'file')]
            case 'view_image':
                return ['Viewed', plural(n, 'image')]
            case 'test_app':
                return ['Tested', n === 1 ? 'the app' : `the app ${n} times`]
            case 'sessions':
                return ['Used', n === 1 ? 'sessions' : `sessions ${n} times`]
            default:
                return ['Used', n === 1 ? name : `${name} ${n} times`]
        }
    }
    return order.map((name, i) => {
        const [verb, rest] = phrase(name, seen.get(name))
        return { verb: i === 0 ? verb : verb.toLowerCase(), rest }
    })
}

// "briefly", "for 12s", "for 3m 5s". Null when either end is unknown:
// transcripts from before events carried a time say only "Thought".
export function duration(start, end) {
    if (typeof start !== 'number' || typeof end !== 'number') return null
    const s = Math.max(0, end - start)
    if (s < 2) return 'briefly'
    if (s < 60) return `for ${Math.round(s)}s`
    const m = Math.floor(s / 60)
    const rest = Math.round(s - m * 60)
    return rest ? `for ${m}m ${rest}s` : `for ${m}m`
}
