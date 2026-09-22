// Per-session runtime state — the agex-studio pattern: one runtime per
// session, kept alive across foreground switches, each consuming its
// own SSE follower. The shell is a PROJECTION of the foreground
// runtime; backgrounded sessions keep streaming (that's what makes the
// rail's busy/unseen dots and instant session switching work).
//
// The server's event schema (all events also ride a `cursor`):
//   {type:'user',   text, head, from_queue?}
//                                    — head = pre-turn workspace commit (edit
//                                      anchor); from_queue names the queued
//                                      messages this turn was started with
//   {type:'text',   delta}           — streamed reply tokens
//   {type:'thinking', delta}         — native model reasoning (when the
//                                      model/provider exposes it)
//   {type:'tool_start', name, args}
//   {type:'tool_end',   name, result}
//   {type:'artifact', name, path, kind} — a ui = {...} artifact the turn
//                                      produced; server-harvested from the
//                                      tool result's [ui artifacts: ...] note
//   {type:'publish', token, version, title, url, head, tree}
//                                    — a version of an app was published:
//                                      a transcript LANDMARK, and an anchor
//                                      the session can be restored to
//   {type:'delegate', name, status, text}
//                                    — a delegate this session forked has
//                                      answered; the text carries its own
//                                      provenance header and is what the
//                                      model was sent this turn too
//   {type:'interject', id, text}     — a message the human queued while
//                                      the turn was running, now delivered
//                                      to the model with a tool result. No
//                                      `head`: the turn began before it was
//                                      said, so it is not an edit anchor
//   {type:'notice', text}            — uploads, ...
//   {type:'error',  message}
//   {type:'done',   run_id, head}    — turn boundary
//   {type:'truncate', to}            — an edit cut the transcript at seq `to`:
//                                      drop that event and everything after
//                                      from the projection (the log keeps them)
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

// Every app on the store (GET /api/apps), newest publish first — the
// rail's Published section. Store-wide rather than per-session on
// purpose: an app outlives the session that made it, and the ones whose
// origin is gone are exactly the ones no per-session view can reach.
// `loaded` distinguishes "no apps" from "not fetched yet": before the
// first fetch lands, every publish marker would otherwise read as an
// app that has been removed (see MessageList's status).
export const published = $state({ apps: [], loaded: false })

export async function refreshApps() {
    try {
        published.apps = (await api('/api/apps')).apps
        published.loaded = true
    } catch {
        /* transient; the next poll wins */
    }
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

/** The human's rename. Outranks the agent's title from here on; an
 * empty title clears the override, falling back to whatever the agent
 * last suggested. Identity (the slug) is untouched — nothing moves. */
export async function renameSession(name, title) {
    await api(`/api/sessions/${name}/title`, { title })
    await refreshSessions()
}

/** Branch a session into a new one. The workspace fork is O(1) and
 * carries the conversation with the files, so `conversation: 'inherit'`
 * (the default) opens the child where the parent stands — same
 * transcript, same memory, its own universe from here on. 'fresh' keeps
 * the files and starts the chat over. Returns the minted name. */
export async function forkSession(name, conversation = 'inherit') {
    const child = await api(`/api/sessions/${name}/fork`, { conversation })
    await refreshSessions()
    getRuntime(child.name).markOpen()
    return child.name
}

/** Mint a new session. The SERVER names it (a slug like sleepy-meerkat):
 * identity is never typed, so the agent is free to title it later.
 * Returns the minted name — the caller switches to it. */
export async function createSession() {
    const { name } = await api('/api/sessions', {})
    await refreshSessions()
    getRuntime(name).markOpen()
    return name
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
     * the preview iframe and files tab refresh off it */
    version = $state(0)
    attachments = $state([])
    lastError = $state(null)
    /** latest model-call context size: {input_tokens, cached_tokens} */
    usage = $state(null)
    /** messages typed while the agent was working, still waiting to be
     * delivered. The SERVER holds the queue — they are the session's,
     * not this tab's — so a reload seeds this from it. */
    queued = $state([])
    /** this session's published apps, most recently published first
     * (GET /api/sessions/{name}/apps) — each with its versions and how
     * far the live workspace has moved since the one being served */
    apps = $state([])

    foreground = false // set via setForeground; gates the SSE stream
    cursor = 0
    #turnArts = []
    // Ids whose delivery arrived before the POST that queued them came
    // back. The SSE event and the response race, and the loser must not
    // put a message back on screen as waiting after the agent has read
    // it — nothing later would clear it.
    #landed = new Set()
    #ctl = null
    #following = false

    constructor(name) {
        this.name = name
        this.#opened = new Promise((resolve) => (this.#markOpen = resolve))
    }

    #opened
    #markOpen
    #exists = false
    /** the session exists server-side — safe to follow. Called by
     * ensureSession after POST /api/sessions succeeds, so the follower
     * doesn't race session creation. */
    markOpen() {
        this.#exists = true
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
            this.syncQueue()
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
            } catch (e) {
                // A 404 on a CONFIRMED session is unambiguous — sessions
                // are manifest-driven and lazily reopened, so "unknown"
                // means DELETED (from another tab): stop, don't spin on
                // retries forever. Before markOpen it may just be a slow
                // create-POST — keep the old retry behavior.
                if (this.#exists && String(e?.message).includes('events feed: 404'))
                    this.#following = false
                /* otherwise dropped or aborted; resubscribe from cursor */
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
            // a turn STARTED from the queue says which messages it took
            if (ev.from_queue?.length) this.#stopWaiting(ev.from_queue)
            this.messages.push({
                role: 'user',
                text: ev.text,
                head: ev.head ?? null,
                seq: ev.cursor ?? null, // its event-log position: the edit handle
            })
        } else if (ev.type === 'interject') {
            // A queued message reached the model, appended to a tool
            // result. It stops waiting and becomes an ordinary user
            // bubble — but the turn carrying it is still running, so
            // `busy` and the turn's artifacts are left exactly as they
            // are, and the agent message so far is closed off: what
            // comes next is its answer to this.
            this.#stopWaiting([ev.id])
            const open = this.messages.at(-1)
            if (open?.role === 'agent') open.streaming = false
            this.messages.push({
                role: 'user',
                text: ev.text,
                mid_turn: true, // delivered INTO a turn: no rewind anchor
                head: null,
                seq: null,
            })
        } else if (ev.type === 'text') {
            const items = this.#agentItems()
            const last = items.at(-1)
            if (last?.kind === 'text') last.text += ev.delta
            else items.push({ kind: 'text', text: ev.delta })
        } else if (ev.type === 'thinking') {
            const items = this.#agentItems()
            const last = items.at(-1)
            if (last?.kind === 'thinking') last.text += ev.delta
            else items.push({ kind: 'thinking', text: ev.delta })
        } else if (ev.type === 'tool_start') {
            this.#agentItems().push({
                kind: 'tool',
                name: ev.name,
                args: ev.args,
                result: null,
                running: true,
            })
        } else if (ev.type === 'tool_end') {
            // tool calls can run in PARALLEL (several starts, then the
            // ends) — pair by name first, oldest open call wins. The
            // search spans the whole turn rather than the message being
            // written: a message the human interjected splits the
            // agent's message in two, and the call this result belongs
            // to opened before the split.
            const open = this.#openTools()
            let tool = open.find((i) => i.name === ev.name) ?? open[0]
            if (!tool) {
                tool = { kind: 'tool', name: ev.name, args: '', running: false }
                this.#agentItems().push(tool)
            }
            tool.result = ev.result
            tool.running = false
            this.#harvest(ev.result, tool)
        } else if (ev.type === 'artifact') {
            // first-class artifact event (server-harvested). The done-time
            // Jupyter rule consumes #turnArts; #addArt dedupes by path so a
            // post-change log — which carries BOTH this event and the note
            // in the tool result — never double-appends the artifact.
            this.#addArt(ev.name, ev.path)
        } else if (ev.type === 'title') {
            // the studio named the session from its transcript. Re-read
            // the rail rather than display ev.title: the server resolves
            // user > generated, and a human title outranks this one —
            // only the server knows.
            refreshSessions()
        } else if (ev.type === 'truncate') {
            // an edit rewound the session: only user messages carry a
            // seq, and everything after the cut derives from events
            // after it — slice at the first user message at/past `to`
            const at = this.messages.findIndex(
                (m) => m.seq != null && m.seq >= ev.to,
            )
            if (at !== -1) this.messages.splice(at)
            this.version++
        } else if (ev.type === 'publish') {
            // a landmark, not a notice: it names a version that still
            // exists, so it stays openable and stays restorable
            this.messages.push({
                role: 'publish',
                token: ev.token,
                version: ev.version,
                title: ev.title,
                url: ev.url,
                head: ev.head,
                seq: ev.cursor ?? null,
            })
            this.version++
            this.loadApps()
        } else if (ev.type === 'usage') {
            this.usage = {
                input_tokens: ev.input_tokens,
                cached_tokens: ev.cached_tokens ?? 0,
            }
        } else if (ev.type === 'delegate') {
            // Its own kind of message, not a notice: a delegate's answer
            // is content the agent acted on, and the rail's waiting count
            // clears the moment this arrives.
            this.messages.push({
                role: 'delegate',
                name: ev.name,
                status: ev.status,
                text: ev.text,
            })
            this.version++
            refreshSessions()
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
            // read across the turn, not just its last message: an
            // interjection splits one reply in two, and an artifact the
            // agent cited before the split is referenced all the same
            const turn = this.#turnMessages()
            const msg = turn.at(-1)
            if (msg) {
                const prose = turn
                    .flatMap((m) => m.items)
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

    /** this turn's agent messages, oldest first. Usually one — a
     * message the human interjected splits it, and what came before
     * the split is still this turn's. The turn's own `user` message
     * is the boundary; an interjected one is not. */
    #turnMessages() {
        const out = []
        for (let i = this.messages.length - 1; i >= 0; i--) {
            const msg = this.messages[i]
            if (msg.role === 'user' && !msg.mid_turn) break
            if (msg.role === 'agent') out.unshift(msg)
        }
        return out
    }

    /** the tool calls of this turn still waiting for a result, in
     * arrival order */
    #openTools() {
        return this.#turnMessages().flatMap((m) =>
            m.items.filter((i) => i.kind === 'tool' && i.running),
        )
    }

    /** These queued messages have reached the agent: drop their
     * bubbles. An id with no bubble yet is REMEMBERED instead — its
     * delivery beat the response to the POST that queued it, and that
     * response must not then show it as waiting. */
    #stopWaiting(ids) {
        const landed = new Set(ids)
        const kept = this.queued.filter((q) => !landed.has(q.id))
        for (const q of this.queued) landed.delete(q.id)
        for (const id of landed) this.#landed.add(id)
        this.queued = kept
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

    /** record a turn artifact, idempotent by path — the server now emits
     * an `artifact` event AND leaves the note in the tool result, so both
     * paths can fire for the same artifact; keep only the first. */
    #addArt(name, path) {
        if (path && !this.#turnArts.some((a) => a.path === path))
            this.#turnArts.push({ name, path })
    }

    /** pull ui artifacts + workspace images out of a tool result */
    #harvest(result, tool) {
        if (typeof result !== 'string') return
        // LEGACY replay-only fallback: pre-change jsonl transcripts have no
        // `artifact` events, so recover them from the [ui artifacts: ...]
        // note here. Post-change logs also carry the note, but #addArt's
        // path-dedupe prevents doubles. Removable once old transcripts stop
        // mattering.
        const note = result.match(ARTIFACT_NOTE)
        if (note)
            for (const pair of note[1].split(', ')) {
                const [name, path] = pair.split(' -> ')
                this.#addArt(name, path)
            }
        // workspace images in the result (test_app screenshots, saved
        // plots) ride WITH the tool call — they render inside its
        // expandable timeline, not in the transcript. The agent
        // referencing an image in prose is what puts it inline.
        const images = result.match(IMAGE_PATHS) || []
        const paths = [...new Set(images)].filter(
            (p) => !p.startsWith('/workspace/ui/'),
        )
        if (paths.length) tool.images = paths
    }

    // -- verbs ---------------------------------------------------------------

    /** Send a message. While a turn is running it is QUEUED rather
     * than refused: the agent reads it with its next tool result, and
     * nothing interrupts what it is doing. */
    async send(text) {
        let message = text.trim()
        if (!message) return false
        if (this.attachments.length) {
            message = `[attached: ${this.attachments.join(', ')}]\n${message}`
            this.attachments = []
        }
        try {
            const res = await api(`/api/sessions/${this.name}/chat`, { message })
            // ...unless it has already been delivered: the event can
            // beat this response, and then there is nothing to wait for
            if (res.queued && !this.#landed.delete(res.queued))
                this.queued = [...this.queued, { id: res.queued, text: message }]
            return true
        } catch (e) {
            this.messages.push({ role: 'error', text: e.message })
            return false
        }
    }

    /** Take a queued message back, while it is still waiting.
     * Delivery cannot be undone, so one the agent has already read
     * answers 404 — and the server's list is what stands. */
    async withdraw(id) {
        try {
            await api(`/api/sessions/${this.name}/queue/${id}`, undefined, 'DELETE')
            this.queued = this.queued.filter((q) => q.id !== id)
            return true
        } catch {
            await this.syncQueue()
            return false
        }
    }

    /** What is still waiting, as the server has it — the queue lives
     * there, so this is how a reload (or a second tab) catches up. */
    async syncQueue() {
        try {
            const info = await api(`/api/sessions/${this.name}`)
            this.queued = info.queued ?? []
        } catch {
            /* transient; the next send or foreground wins */
        }
    }

    /** gracefully stop the running turn (agno cancels at the next
     * cancellation point; partial work stays in files AND agent
     * memory) */
    async stop() {
        if (!this.busy) return false
        try {
            await api(`/api/sessions/${this.name}/cancel`, {})
            return true
        } catch {
            return false // raced the turn's end; nothing to stop
        }
    }

    /** edit an earlier prompt: the server rewinds files + agent
     * memory to before that turn, truncates the visible transcript,
     * and runs the edited message as a fresh turn */
    async edit(seq, message) {
        if (!message.trim() || this.busy) return false
        try {
            await api(`/api/sessions/${this.name}/edit`, { seq, message })
            this.version++
            return true
        } catch (e) {
            this.messages.push({ role: 'error', text: e.message })
            return false
        }
    }

    // -- apps: publications of this session ----------------------------------

    /** refresh the session's apps. Swallows failures on purpose: this
     * runs off every version tick, and a blip must not replace a good
     * list with an empty one. */
    async loadApps() {
        try {
            const data = await api(`/api/sessions/${this.name}/apps`)
            this.apps = data.apps
        } catch {
            /* keep what we had */
        }
    }

    /** after anything that CHANGES an app, not merely reads one: the
     * session's own list and the rail's store-wide one are two
     * projections of the same rows, and a caller that refreshes one
     * leaves the other showing stale counts — or a clickable row for an
     * app that no longer exists — until the next poll. */
    async syncApps() {
        await Promise.all([this.loadApps(), refreshApps()])
    }

    /** publish a version. `name` names it (blank = the server's vN).
     * The marker arrives on the event feed, so nothing here touches the
     * transcript. */
    async publish(body = {}) {
        const made = await api(`/api/sessions/${this.name}/publish`, body)
        await this.syncApps()
        return made
    }

    /** restore to one of this session's own publishes: files, agent
     * memory and title go back to where that version was published, and
     * the transcript is cut after the marker (which survives). */
    async restoreTo(seq) {
        if (this.busy) return false
        try {
            await api(`/api/sessions/${this.name}/restore`, { seq })
            this.version++
            return true
        } catch (e) {
            this.messages.push({ role: 'error', text: e.message })
            return false
        }
    }
}

/** What one name IS: title, model, busy, and — when somebody forked
 * it — the row its parent sees (`delegate`, else null). The rail's list
 * answers this for every session it shows; a delegate has no row there,
 * so a shell that lands on `?session=<child>` has only the name. */
export async function loadSession(name) {
    return await api(`/api/sessions/${name}`)
}

/** What a session delegated, and what became of each one. Asked for
 * when the human opens the rail's listing rather than polled: the ⑂
 * badge is on the session list already, and this is the detail behind
 * it. */
export async function loadDelegates(name) {
    return (await api(`/api/sessions/${name}/delegates`)).delegates
}

/** Keep a delegate's branch from the retention sweep, or let it go
 * again. Returns the row as the server reads it from here on — a keep
 * is recorded whether or not a live job took the flag, and an un-keep
 * is that record alone. */
export async function setDelegateKept(name, child, kept) {
    const res = await api(`/api/sessions/${name}/delegates/${child}/keep`, { kept })
    return res.delegate
}
