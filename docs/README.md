# nontainer-studio docs

- [Configuration](configuration.md) — every environment knob: models and
  providers, server and store, where agent code runs, what the agent is
  allowed to do, delegation caps.
- [Apps](apps.md) — the app-building loop: live preview, publish,
  versions, the `changes` tab, the unit-test tier, the vendored libraries
  that make it work offline, and what changes under a dud executor.
- [What owns what](design.md) — the three kinds of state and why each
  behaves as it does: the workspace, the app `db`, the conversation.
  Also apps as publications, generated titles, mid-turn messages, and
  a2ui egress.
- [Delegation](delegation.md) — the `sessions` tool, how a delegate's
  work comes back, and the budget, depth and retention caps around it.
- [Hacking](hacking.md) — the frontend dev loop, the architecture map,
  the HTTP routes, the event log's event types, and the tests.

## Not yet documented

Real gaps, not a wish list. Each one is something a reader can currently
only get by reading the code.

- **A first five minutes.** Nothing here walks through opening a session,
  asking for an app and publishing it, with screenshots of the panes,
  the rail and the version list. Every page assumes you already know
  what you are looking at.
- **The skills seeded into a session.** `skills/building-apps` and
  `skills/starting-from-published` are what actually teach the agent the
  app loop, and nothing documents what they say or how to author a third
  one. `NONTAINER_STUDIO_SKILLS` is described as a directory without
  saying what a good entry in it looks like.
- **The publish manifest.** The studio's own table — token, route,
  `db` path, versions, origin tags, descriptions — is described by its
  invariants in [What owns what](design.md), but its actual shape on
  disk is not written down anywhere.
- **The CSP a published app runs under.** [Configuration](configuration.md)
  says how to override it and why it is set on the config rather than the
  router. What the derived policy actually contains, and what a handler
  may therefore load, fetch or inline, is not stated.
- **Uploads and file access.** The upload and file routes are in the
  table in [Hacking](hacking.md), but nothing says where uploads land in
  the workspace, how the agent is told about them, or what the 50 MB cap
  means for real data.
- **The a2ui component mapping in detail.** [What owns what](design.md)
  names the catalog and the `Chart` extension. Which artifact kind
  becomes which component, and what a consumer has to implement to render
  a full reply, is only in nontainer's adapter.
