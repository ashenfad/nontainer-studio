// Thin HTTP client over the studio API.

export async function api(path, body, method) {
    const res = await fetch(path, {
        method: method ?? (body === undefined ? 'GET' : 'POST'),
        headers: { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.error || res.statusText)
    return data
}

export function fileUrl(session, path) {
    return `/api/sessions/${session}/file?path=${encodeURIComponent(path)}`
}

// N parallel raw-body uploads; the workspace lock serializes the
// writes safely and each lands as its own commit.
export async function uploadFiles(session, fileList) {
    return Promise.all(
        [...fileList].map(async (f) => {
            const res = await fetch(
                `/api/sessions/${session}/upload?name=${encodeURIComponent(f.name)}`,
                { method: 'POST', body: f },
            )
            const data = await res.json()
            if (!res.ok) throw new Error(data.error || res.statusText)
            return data.path
        }),
    )
}

// SSE over fetch (EventSource is GET-only-no-abort; fetch gives us the
// AbortController and full control of reconnect cursors). Calls
// onEvent(event) for each event; resolves when the stream ends.
export async function followEvents(session, since, onEvent, signal) {
    const res = await fetch(`/api/sessions/${session}/events?since=${since}`, {
        signal,
    })
    if (!res.ok) throw new Error(`events feed: ${res.status}`)
    const reader = res.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let i
        while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i)
            buf = buf.slice(i + 2)
            for (const line of chunk.split('\n')) {
                if (!line.startsWith('data: ')) continue
                // parse defensively: a poison line would otherwise throw,
                // resubscribe from the SAME cursor, refetch the same line,
                // and wedge the follower in a permanent retry loop
                let ev
                try {
                    ev = JSON.parse(line.slice(6))
                } catch {
                    console.warn('skipping malformed event line:', line)
                    continue
                }
                onEvent(ev)
            }
        }
    }
}
