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

import { untrack } from 'svelte'

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

/** forget a deleted session's runtime (stops any follower) */
export function dropRuntime(name) {
    const rt = runtimes.get(name)
    if (rt) rt.setForeground(false)
    runtimes.delete(name)
}

// -- the session rail's list (server view + live overlay) -------------------

export const rail = $state({ sessions: [], unseen: [] })

// unseen detection rides the rail poll (busy -> idle transition on a
// backgrounded session), because background runtimes hold NO event
// stream: browsers cap HTTP/1.1 at ~6 connections per origin, and a
// never-ending SSE per visited session exhausts the pool — after which
// every new fetch (including the chat POST) queues silently forever.
let _prevBusy = new Map()
let _foregroundName = null

export function setForegroundName(name) {
    _foregroundName = name
    // untracked: callers are effects, and a tracked read-then-write of
    // rail.unseen would make them retrigger themselves forever
    untrack(() => {
        rail.unseen = rail.unseen.filter((n) => n !== name)
    })
}

// what the server's env unlocks (GET /api/models) — loaded once
export const catalog = $state({ providers: [], default: null })

export async function loadCatalog() {
    try {
        const data = await api('/api/models')
        catalog.providers = data.providers
        catalog.default = data.default
    } catch {
        /* picker just stays hidden */
    }
}

export async function refreshSessions() {
    try {
        const data = await api('/api/sessions')
        rail.sessions = data.sessions
        for (const s of data.sessions) {
            const finished = _prevBusy.get(s.name) && !s.busy
            if (finished && s.name !== _foregroundName)
                if (!rail.unseen.includes(s.name))
                    rail.unseen = [...rail.unseen, s.name]
            _prevBusy.set(s.name, s.busy)
        }
    } catch {
        /* transient; next poll wins */
    }
}

export async function ensureSession(name) {
    await api('/api/sessions', { name })
    await refreshSessions()
    const rt = getRuntime(name)
    rt.markOpen() // the follower waits for this — no 404 race on create
    return rt
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

    foreground = false // set via setForeground; gates the SSE stream
    cursor = 0
    #turnArts = []
    #ctl = null
    #following = false

    constructor(name) {
        this.name = name
        this.#opened = new Promise((resolve) => (this.#markOpen = resolve))
    }

    #opened
    #markOpen
    /** the session exists server-side — safe to follow. Called by
     * ensureSession after POST /api/sessions succeeds, so the follower
     * doesn't race session creation. */
    markOpen() {
        this.#markOpen()
    }

    /** Only the FOREGROUND session holds a live SSE stream — browsers
     * cap connections per origin (~6 on HTTP/1.1), so one stream per
     * visited session would eventually starve every other request.
     * Backgrounding aborts the stream; re-foregrounding resumes from
     * the cursor, so nothing is missed. */
    setForeground(fg) {
        this.foreground = fg
        if (fg) {
            this.unseen = false
            this.#startFollow()
        } else {
            this.#following = false
            this.#ctl?.abort()
        }
    }

    #startFollow() {
        if (this.#following) return
        this.#following = true
        this.#followLoop()
    }

    async #followLoop() {
        // wait for creation, but never forever: a failed create-POST
        // must not permanently mute the session (with_session lazily
        // opens known sessions, so following early is safe)
        await Promise.race([this.#opened, new Promise((r) => setTimeout(r, 3000))])
        while (this.#following) {
            this.#ctl = new AbortController()
            try {
                this.connected = true
                await followEvents(
                    this.name,
                    this.cursor,
                    (ev) => this.#apply(ev),
                    this.#ctl.signal,
                )
            } catch {
                /* dropped or aborted; resubscribe from cursor */
            }
            this.connected = false
            if (this.#following)
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
            // tool calls can run in PARALLEL (several starts, then the
            // ends) — pair by name first, oldest open call wins
            let tool =
                items.find(
                    (i) => i.kind === 'tool' && i.running && i.name === ev.name,
                ) ?? items.find((i) => i.kind === 'tool' && i.running)
            if (!tool) {
                tool = { kind: 'tool', name: ev.name, args: '', running: false }
                items.push(tool)
            }
            tool.result = ev.result
            tool.running = false
            this.#harvest(ev.result, tool)
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
    #harvest(result, tool) {
        if (typeof result !== 'string') return
        const note = result.match(ARTIFACT_NOTE)
        if (note)
            for (const pair of note[1].split(', ')) {
                const [name, path] = pair.split(' -> ')
                if (path) this.#turnArts.push({ name, path })
            }
        // workspace images in the result (test_app screenshots, saved
        // plots) ride WITH the tool call — they render inside its
        // expandable timeline, not in the transcript. The agent
        // referencing an image in prose is what puts it inline.
        const images = result.match(IMAGE_PATHS) || []
        const paths = [...new Set(images)].filter((p) => !p.startsWith('/ui/'))
        if (paths.length) tool.images = paths
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
