// nontainer-studio frontend — no-build HTM + Preact, the same tier the
// agents use for their apps. Chat with inline images, live preview +
// publish, files tab, history rail, background sessions.
import { h, render } from "https://esm.sh/preact@10";
import { useEffect, useRef, useState } from "https://esm.sh/preact@10/hooks";
import htm from "https://esm.sh/htm@3";

const html = htm.bind(h);

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function uploadFiles(session, fileList) {
  // N parallel raw-body uploads; the workspace lock serializes the
  // writes safely and each lands as its own commit.
  return Promise.all(
    [...fileList].map(async (f) => {
      const res = await fetch(
        `/api/sessions/${session}/upload?name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data.path;
    }),
  );
}

// SSE over fetch (EventSource is GET-only and we want AbortController).
async function followEvents(session, since, onEvent, signal) {
  const res = await fetch(`/api/sessions/${session}/events?since=${since}`, {
    signal,
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      for (const line of chunk.split("\n"))
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

// -- markdown + artifact rendering -----------------------------------

let mdLibs;
async function renderMarkdown(text) {
  mdLibs ??= Promise.all([
    import("https://esm.sh/marked@12"),
    import("https://esm.sh/dompurify@3"),
  ]);
  const [{ marked }, { default: DOMPurify }] = await mdLibs;
  return DOMPurify.sanitize(marked.parse(text));
}

function Markdown({ text }) {
  const [out, setOut] = useState("");
  useEffect(() => {
    let dead = false;
    renderMarkdown(text).then((h2) => !dead && setOut(h2));
    return () => (dead = true);
  }, [text]);
  return html`<div class="md" dangerouslySetInnerHTML=${{ __html: out }}></div>`;
}

function PlotlyFig({ url }) {
  const ref = useRef(null);
  useEffect(() => {
    let dead = false;
    (async () => {
      const [{ default: Plotly }, spec] = await Promise.all([
        import("https://esm.sh/plotly.js-dist-min@2"),
        fetch(url).then((r) => r.json()),
      ]);
      if (dead || !ref.current) return;
      const dark = matchMedia("(prefers-color-scheme: dark)").matches;
      // spec tier: WE render, so WE theme — transparent background,
      // shell-appropriate font color
      const layout = {
        ...spec.layout,
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { ...(spec.layout?.font || {}), color: dark ? "#ccc" : "#333" },
      };
      Plotly.newPlot(ref.current, spec.data, layout, {
        responsive: true,
        displaylogo: false,
      });
    })();
    return () => (dead = true);
  }, [url]);
  return html`<div class="plotly-fig" ref=${ref}></div>`;
}

function DataTable({ url }) {
  const [table, setTable] = useState(null);
  useEffect(() => {
    fetch(url).then((r) => r.json()).then(setTable);
  }, [url]);
  if (!table) return html`<div class="hint">…</div>`;
  return html`
    <div class="table-wrap">
      <table>
        <thead><tr>${table.columns.map((c) => html`<th>${c}</th>`)}</tr></thead>
        <tbody>
          ${table.data.map(
            (row) => html`<tr>${row.map((v) => html`<td>${String(v)}</td>`)}</tr>`,
          )}
        </tbody>
      </table>
      ${table.total > table.data.length &&
      html`<div class="hint">showing ${table.data.length} of ${table.total} rows</div>`}
    </div>
  `;
}

function HtmlFrame({ url }) {
  const [doc, setDoc] = useState(null);
  useEffect(() => {
    fetch(url).then((r) => r.text()).then(setDoc);
  }, [url]);
  if (doc === null) return html`<div class="hint">…</div>`;
  const prelude =
    "<style>:root{color-scheme:light dark}body{font-family:ui-sans-serif,system-ui;margin:0.5rem}</style>";
  return html`<iframe
    class="html-art"
    sandbox="allow-scripts"
    srcdoc=${prelude + doc}
  ></iframe>`;
}

function JsonBlock({ url }) {
  const [text, setText] = useState(null);
  useEffect(() => {
    fetch(url).then((r) => r.text()).then(setText);
  }, [url]);
  return html`<details class="json-art" open>
    <summary>json</summary>
    <pre>${text ?? "…"}</pre>
  </details>`;
}

function Artifact({ session, path, name }) {
  const url = `/api/sessions/${session}/file?path=${encodeURIComponent(path)}`;
  if (/\.plotly\.json$/.test(path)) return html`<${PlotlyFig} url=${url} />`;
  if (/\.table\.json$/.test(path)) return html`<${DataTable} url=${url} />`;
  if (/\.(png|jpe?g|gif|webp)$/i.test(path))
    return html`<img class="shot" src=${url} title=${path} />`;
  if (/\.html$/.test(path)) return html`<${HtmlFrame} url=${url} />`;
  if (/\.json$/.test(path)) return html`<${JsonBlock} url=${url} />`;
  if (/\.txt$/.test(path))
    return html`<${JsonBlock} url=${url} />`;
  return html`<a href=${url} target="_blank" rel="noopener">${name || path}</a>`;
}

// agent prose with markdown-image references to workspace artifacts
// spliced in as live renderers (the code-interpreter URI idiom)
function AgentMessage({ text, session }) {
  const parts = [];
  const re = /!\[([^\]]*)\]\((\/[^)\s]+)\)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push({ md: text.slice(last, m.index) });
    parts.push({ name: m[1], path: m[2] });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ md: text.slice(last) });
  return html`<div class="msg agent">
    ${parts.map((p) =>
      p.md !== undefined
        ? html`<${Markdown} text=${p.md} />`
        : html`<${Artifact} session=${session} path=${p.path} name=${p.name} />`,
    )}
  </div>`;
}

function SessionRail({ sessions, active, onSwitch, onCreate }) {
  const [name, setName] = useState("");
  return html`
    <nav class="rail">
      <div class="rail-title">sessions</div>
      ${sessions.map(
        (s) => html`
          <button
            class="rail-item ${s.name === active ? "active" : ""}"
            onClick=${() => onSwitch(s.name)}
          >
            <span class="dot ${s.busy ? "busy" : ""}"></span> ${s.name}
          </button>
        `,
      )}
      <form
        class="rail-new"
        onSubmit=${(e) => {
          e.preventDefault();
          if (name.trim()) onCreate(name.trim());
          setName("");
        }}
      >
        <input
          placeholder="new session…"
          value=${name}
          onInput=${(e) => setName(e.target.value)}
        />
      </form>
    </nav>
  `;
}

function Transcript({ log, session }) {
  const bottom = useRef(null);
  useEffect(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), [log]);
  return html`
    <div class="log">
      ${log.map((m) => {
        if (m.kind === "tool")
          return html`<div class="tool">
            <details><summary>${m.name}</summary><pre>${m.text}</pre></details>
          </div>`;
        if (m.kind === "notice") return html`<div class="notice">${m.text}</div>`;
        if (m.kind === "image" || m.kind === "artifact")
          return html`<${Artifact} session=${session} path=${m.path} name=${m.name} />`;
        if (m.kind === "agent")
          return html`<${AgentMessage} text=${m.text} session=${session} />`;
        return html`<div class="msg ${m.kind}">${m.text}</div>`;
      })}
      <div ref=${bottom}></div>
    </div>
  `;
}

function History({ session, busy, onChanged }) {
  const [entries, setEntries] = useState([]);
  const [head, setHead] = useState(null);
  const [error, setError] = useState(null);

  async function refresh() {
    try {
      const data = await api(`/api/sessions/${session}/history`);
      setEntries(data.history);
      setHead(data.head);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    refresh();
  }, [session, busy]);

  async function restore(id) {
    try {
      await api(`/api/sessions/${session}/restore`, { checkpoint: id });
      await refresh();
      onChanged();
    } catch (e) {
      setError(e.message);
    }
  }

  async function forkFrom(id) {
    const name = prompt(`fork ${session} from ${id.slice(0, 8)} as:`);
    if (!name) return;
    try {
      await api(`/api/sessions/${session}/fork`, { name, checkpoint: id });
      onChanged(name);
    } catch (e) {
      setError(e.message);
    }
  }

  return html`
    <div class="history">
      ${error && html`<div class="error">${error}</div>`}
      <div class="hint">
        restore rewinds files, cache & the agent's memory together — the
        app's <code>db</code> and this transcript keep their history
      </div>
      ${entries.map(
        (c) => html`
          <div class="commit ${c.id === head ? "head" : ""}">
            <code>${c.id.slice(0, 8)}</code>
            <span class="grow">${c.info.tool || c.info.published || ""}</span>
            <button class="small" disabled=${busy} onClick=${() => restore(c.id)}>
              restore
            </button>
            <button class="small" onClick=${() => forkFrom(c.id)}>fork</button>
          </div>
        `,
      )}
    </div>
  `;
}

function Files({ session, version }) {
  const [paths, setPaths] = useState([]);
  const [selected, setSelected] = useState(null);
  const [text, setText] = useState(null);

  useEffect(() => {
    api(`/api/sessions/${session}/files`).then((d) => setPaths(d.files));
  }, [session, version]);

  const isImage = (p) => /\.(png|jpe?g|gif|webp)$/i.test(p);
  const fileUrl = (p) =>
    `/api/sessions/${session}/file?path=${encodeURIComponent(p)}`;

  async function open(path) {
    setSelected(path);
    setText(null);
    if (!isImage(path)) {
      const res = await fetch(fileUrl(path));
      const body = await res.text();
      setText(body.length > 20000 ? body.slice(0, 20000) + "\n…[truncated]" : body);
    }
  }

  return html`
    <div class="files">
      <div class="file-list">
        ${paths.map(
          (p) => html`
            <button
              class="file-item ${p === selected ? "active" : ""}"
              onClick=${() => open(p)}
            >
              ${p}
            </button>
          `,
        )}
      </div>
      <div class="file-view">
        ${selected && isImage(selected) && html`<img class="shot" src=${fileUrl(selected)} />`}
        ${selected && !isImage(selected) && html`<pre>${text ?? "…"}</pre>`}
        ${!selected && html`<div class="hint">select a file</div>`}
      </div>
    </div>
  `;
}

function Preview({ session, version, onReload }) {
  const [published, setPublished] = useState(null);
  useEffect(() => setPublished(null), [session]);

  async function publish() {
    try {
      setPublished(await api(`/api/sessions/${session}/publish`, {}));
    } catch (err) {
      setPublished({ error: err.message });
    }
  }

  return html`
    <div class="preview">
      <div class="side-bar">
        <span>preview: <code>/preview/${session}/</code></span>
        <button class="small" onClick=${onReload}>reload</button>
        <span class="grow"></span>
        ${published?.url &&
        html`<a href=${published.url} target="_blank" rel="noopener"
          >published ↗ (${published.checkpoint?.slice(0, 8)})</a
        >`}
        ${published?.error && html`<span class="error">${published.error}</span>`}
        <button class="small" onClick=${publish} title="freeze a snapshot behind a capability URL">
          publish
        </button>
      </div>
      <iframe
        key=${`${session}:${version}`}
        src=${`/preview/${session}/?v=${version}`}
        sandbox="allow-scripts allow-forms"
      ></iframe>
    </div>
  `;
}

function App() {
  const [sessions, setSessions] = useState([]);
  const [active, setActive] = useState(
    new URLSearchParams(location.search).get("session") || "demo",
  );
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("preview");
  const [version, setVersion] = useState(0);
  const follower = useRef(null);
  const turnArts = useRef([]);

  async function refreshSessions() {
    const data = await api("/api/sessions");
    setSessions(data.sessions);
  }

  // event -> transcript item; busy tracking; preview bumps on done
  function apply(ev) {
    if (ev.type === "user") {
      setBusy(true);
      turnArts.current = [];
      setLog((l) => [...l, { kind: "user", text: ev.text }]);
    } else if (ev.type === "text") {
      setLog((l) => {
        const last = l[l.length - 1];
        if (last?.kind === "agent")
          return [...l.slice(0, -1), { ...last, text: last.text + ev.delta }];
        return [...l, { kind: "agent", text: ev.delta }];
      });
    } else if (ev.type === "tool_start") {
      setLog((l) => [...l, { kind: "tool", name: ev.name, text: ev.args }]);
    } else if (ev.type === "tool_end") {
      setLog((l) => [...l, { kind: "tool", name: `${ev.name} ✓`, text: ev.result }]);
      // ui artifacts are owned by the reply flow (inline or trailing)
      const note = ev.result.match(/\[ui artifacts: ([^\]]+)\]/);
      if (note)
        for (const pair of note[1].split(", ")) {
          const [name, path] = pair.split(" -> ");
          if (path) turnArts.current.push({ name, path });
        }
      // surface other workspace images (test_app screenshots, saved
      // plots) inline — served raw via the file endpoint
      const images = ev.result.match(/\/[\w./-]+\.(?:png|jpe?g|gif|webp)\b/g) || [];
      for (const path of [...new Set(images)])
        if (!path.startsWith("/ui/"))
          setLog((l) => [...l, { kind: "image", path }]);
    } else if (ev.type === "error") {
      setLog((l) => [...l, { kind: "error", text: ev.message }]);
    } else if (ev.type === "notice") {
      setLog((l) => [...l, { kind: "notice", text: ev.text }]);
      setVersion((v) => v + 1);
    } else if (ev.type === "done") {
      setBusy(false);
      setVersion((v) => v + 1);
      refreshSessions();
      // Jupyter's rule: outputs always show. Artifacts the reply
      // didn't reference render after it instead of vanishing.
      const pending = turnArts.current.splice(0);
      setLog((l) => {
        const lastUser = l.map((m) => m.kind).lastIndexOf("user");
        const prose = l
          .slice(lastUser + 1)
          .filter((m) => m.kind === "agent")
          .map((m) => m.text)
          .join("\n");
        const unreferenced = pending.filter((a) => !prose.includes(a.path));
        return [
          ...l,
          ...unreferenced.map((a) => ({ kind: "artifact", ...a })),
        ];
      });
    }
  }

  // (re)subscribe on session switch: abort the old follower, replay the
  // new session's transcript from 0, then follow live. The turn keeps
  // running server-side either way — that's the whole point.
  useEffect(() => {
    follower.current?.abort();
    const ctl = new AbortController();
    follower.current = ctl;
    setLog([]);
    setBusy(false);
    turnArts.current = [];
    api("/api/sessions", { name: active })
      .then(refreshSessions)
      .then(() =>
        followEvents(active, 0, apply, ctl.signal).catch(() => {}),
      );
    return () => ctl.abort();
  }, [active]);

  useEffect(() => {
    const t = setInterval(refreshSessions, 4000);
    return () => clearInterval(t);
  }, []);

  const [attachments, setAttachments] = useState([]);

  async function attach(fileList) {
    if (!fileList?.length) return;
    try {
      const paths = await uploadFiles(active, fileList);
      setAttachments((a) => [...new Set([...a, ...paths])]);
    } catch (err) {
      setLog((l) => [...l, { kind: "error", text: `upload failed: ${err.message}` }]);
    }
  }

  async function send(e) {
    e.preventDefault();
    const box = e.target.elements.message;
    let message = box.value.trim();
    if (!message || busy) return;
    if (attachments.length) {
      message = `[attached: ${attachments.join(", ")}]\n${message}`;
      setAttachments([]);
    }
    box.value = "";
    try {
      await api(`/api/sessions/${active}/chat`, { message });
    } catch (err) {
      setLog((l) => [...l, { kind: "error", text: err.message }]);
    }
  }

  function switchTo(name) {
    history.replaceState(null, "", `?session=${encodeURIComponent(name)}`);
    setActive(name);
  }

  return html`
    <header>
      <h1>nontainer-studio</h1>
      <span class="session">session: ${active}${busy ? " · running…" : ""}</span>
    </header>
    <main>
      <${SessionRail}
        sessions=${sessions}
        active=${active}
        onSwitch=${switchTo}
        onCreate=${switchTo}
      />
      <div class="chat-pane">
        <${Transcript} log=${log} session=${active} />
        ${attachments.length > 0 &&
        html`<div class="chips">
          ${attachments.map(
            (p) => html`<span class="chip">
              ${p.split("/").pop()}
              <button
                type="button"
                onClick=${() => setAttachments((a) => a.filter((x) => x !== p))}
              >
                ×
              </button>
            </span>`,
          )}
        </div>`}
        <form
          onSubmit=${send}
          onDragOver=${(e) => e.preventDefault()}
          onDrop=${(e) => {
            e.preventDefault();
            attach(e.dataTransfer.files);
          }}
        >
          <label class="attach" title="upload files into /uploads/">
            +
            <input
              type="file"
              multiple
              hidden
              onChange=${(e) => {
                attach(e.target.files);
                e.target.value = "";
              }}
            />
          </label>
          <textarea
            name="message"
            rows="2"
            placeholder="Ask the agent to build something… (drop files to attach)"
            onKeyDown=${(e) => {
              if (e.key === "Enter" && !e.shiftKey) e.target.form.requestSubmit();
            }}
          ></textarea>
          <button disabled=${busy}>${busy ? "running…" : "send"}</button>
        </form>
      </div>
      <div class="side-pane">
        <div class="tabs">
          <button
            class="small ${tab === "preview" ? "active" : ""}"
            onClick=${() => setTab("preview")}
          >
            preview
          </button>
          <button
            class="small ${tab === "files" ? "active" : ""}"
            onClick=${() => setTab("files")}
          >
            files
          </button>
          <button
            class="small ${tab === "history" ? "active" : ""}"
            onClick=${() => setTab("history")}
          >
            history
          </button>
        </div>
        ${tab === "preview" &&
        html`<${Preview}
          session=${active}
          version=${version}
          onReload=${() => setVersion((v) => v + 1)}
        />`}
        ${tab === "files" &&
        html`<${Files} session=${active} version=${version} />`}
        ${tab === "history" &&
        html`<${History}
          session=${active}
          busy=${busy}
          onChanged=${(name) => {
            if (name) switchTo(name);
            else setVersion((v) => v + 1);
          }}
        />`}
      </div>
    </main>
  `;
}

render(html`<${App} />`, document.getElementById("app"));
