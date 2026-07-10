// Per-session runtime state — the agex-studio pattern: one runtime per
// session, kept alive across foreground switches, each consuming its
// own SSE follower. The shell is a PROJECTION of the foreground
// runtime; backgrounded sessions keep streaming (that's what makes the
// rail's busy/unseen dots and instant session switching work).
//
// The server's event schema (all events also ride a `cursor`):
//   {type:'user',   text, head}      — head = pre-turn workspace commit (undo anchor)
//   {type:'text',   delta}           — streamed reply tokens
//   {type:'tool_start', name, args}
//   {type:'tool_end',   name, result}
//   {type:'notice', text}            — uploads, restores, ...
//   {type:'error',  message}
//   {type:'done',   run_id, head}    — turn boundary
//
// Folding: each turn becomes [user message, agent message]; the agent
// message is an ordered item list — text runs, tool events, images,
// trailing ui artifacts — that MessageList renders.

import { api, followEvents } from './api.js'

const runtimes = new Map() // deliberately non-reactive (agex lesson)

export function getRuntime(name) {
    let rt = runtimes.get(name)
    if (!rt) {
        rt = new SessionRuntime(name)
        runtimes.set(name, rt)
    }
    return rt
}

// status without instantiating (rail dots for never-opened sessions)
export function peekRuntime(name) {
    return runtimes.get(name)
}

// -- the session rail's list (server view + live overlay) -------------------

export const rail = $state({ sessions: [] })

export async function refreshSessions() {
    try {
        const data = await api('/api/sessions')
        rail.sessions = data.sessions
    } catch {
        /* transient; next poll wins */
    }
}

export async function ensureSession(name) {
    await api('/api/sessions', { name })
    await refreshSessions()
    return getRuntime(name)
}

// ---------------------------------------------------------------------------

const ARTIFACT_NOTE = /\[ui artifacts: ([^\]]+)\]/
const IMAGE_PATHS = /\/[\w./-]+\.(?:png|jpe?g|gif|webp)\b/g

export class SessionRuntime {
    messages = $state([])
    busy = $state(false)
    unseen = $state(false)
    connected = $state(false)
    /** bumps whenever the workspace likely changed (done / notice) —
     * preview iframe, files tab, and history rail refresh off it */
    version = $state(0)
    attachments = $state([])
    lastError = $state(null)

    foreground = false // set by the shell; gates the unseen dot
    cursor = 0
    #turnArts = []

    constructor(name) {
        this.name = name
        this.#follow()
    }

    // -- the SSE follower: replay from cursor, then live; reconnect forever
    async #follow() {
        for (;;) {
            try {
                this.connected = true
                await followEvents(this.name, this.cursor, (ev) => this.#apply(ev))
            } catch {
                /* dropped; resubscribe from cursor */
            }
            this.connected = false
            await new Promise((r) => setTimeout(r, 1500))
        }
    }

    #apply(ev) {
        if (typeof ev.cursor === 'number') this.cursor = ev.cursor + 1
        if (ev.type === 'user') {
            this.busy = true
            this.#turnArts = []
            this.messages.push({ role: 'user', text: ev.text, head: ev.head ?? null })
        } else if (ev.type === 'text') {
            const items = this.#agentItems()
            const last = items.at(-1)
            if (last?.kind === 'text') last.text += ev.delta
            else items.push({ kind: 'text', text: ev.delta })
        } else if (ev.type === 'tool_start') {
            this.#agentItems().push({
                kind: 'tool',
                name: ev.name,
                args: ev.args,
                result: null,
                running: true,
            })
        } else if (ev.type === 'tool_end') {
            const items = this.#agentItems()
            const open = items.findLast((i) => i.kind === 'tool' && i.running)
            if (open) {
                open.result = ev.result
                open.running = false
            } else {
                items.push({
                    kind: 'tool',
                    name: ev.name,
                    args: '',
                    result: ev.result,
                    running: false,
                })
            }
            this.#harvest(ev.result, items)
        } else if (ev.type === 'notice') {
            this.messages.push({ role: 'notice', text: ev.text })
            this.version++
        } else if (ev.type === 'error') {
            this.messages.push({ role: 'error', text: ev.message })
        } else if (ev.type === 'done') {
            this.busy = false
            this.version++
            // Jupyter's rule: outputs always show. Artifacts the reply
            // didn't reference render after it instead of vanishing.
            const msg = this.messages.findLast((m) => m.role === 'agent')
            if (msg) {
                const prose = msg.items
                    .filter((i) => i.kind === 'text')
                    .map((i) => i.text)
                    .join('\n')
                for (const a of this.#turnArts)
                    if (!prose.includes(a.path))
                        msg.items.push({ kind: 'artifact', name: a.name, path: a.path })
                msg.streaming = false
            }
            this.#turnArts = []
            if (!this.foreground) this.unseen = true
            refreshSessions()
        }
    }

    /** the streaming agent message's item list (created on first use —
     * turns may open with a tool call before any prose) */
    #agentItems() {
        let msg = this.messages.at(-1)
        if (msg?.role !== 'agent' || !msg.streaming) {
            msg = { role: 'agent', items: [], streaming: true }
            this.messages.push(msg)
        }
        return msg.items
    }

    /** pull ui artifacts + workspace images out of a tool result */
    #harvest(result, items) {
        if (typeof result !== 'string') return
        const note = result.match(ARTIFACT_NOTE)
        if (note)
            for (const pair of note[1].split(', ')) {
                const [name, path] = pair.split(' -> ')
                if (path) this.#turnArts.push({ name, path })
            }
        // surface other workspace images (test_app screenshots, saved
        // plots) inline — served raw via the file endpoint
        const images = result.match(IMAGE_PATHS) || []
        for (const path of [...new Set(images)])
            if (!path.startsWith('/ui/')) items.push({ kind: 'image', path })
    }

    // -- verbs ---------------------------------------------------------------

    async send(text) {
        let message = text.trim()
        if (!message || this.busy) return false
        if (this.attachments.length) {
            message = `[attached: ${this.attachments.join(', ')}]\n${message}`
            this.attachments = []
        }
        try {
            await api(`/api/sessions/${this.name}/chat`, { message })
            return true
        } catch (e) {
            this.messages.push({ role: 'error', text: e.message })
            return false
        }
    }

    /** restore to a user message's pre-turn head (the inline undo) or
     * to an explicit checkpoint id (the history rail) */
    async restore(checkpoint) {
        try {
            await api(`/api/sessions/${this.name}/restore`, { checkpoint })
            this.version++
            return true
        } catch (e) {
            this.messages.push({ role: 'error', text: e.message })
            return false
        }
    }
}
