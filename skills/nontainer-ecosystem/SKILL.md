---
name: nontainer-ecosystem
description: what this workspace is built from — nontainer-studio, nontainer, termish, monkeyfs, sandtrap, kvgit, reprobate and dud; how they fit together, with each one's README. Read it when asked about any of them, or to explain or present them
---

# The stack under this workspace

You are working inside **nontainer-studio**. What you see as a terminal,
a Python tool and versioned files is a handful of small Python
libraries, each doing one job.

```
nontainer-studio   the web workbench: chat, live app preview, publish
└─ nontainer       the workspace: terminal and Python tools over versioned files
   ├─ termish      the terminal: shell commands over a virtual filesystem
   ├─ monkeyfs     routes Python's open() and os calls to that filesystem
   ├─ sandtrap     the sandbox agent Python runs in
   ├─ kvgit        the versioning: commits, branches, merges, tags
   ├─ reprobate    fits any Python value into a size budget for output
   └─ dud          optional: runs the code on a real machine instead
```

## What each one is

- **nontainer-studio** is a local AI workbench. A human chats with an
  agent working in a versioned workspace, and every turn is a commit.
  Editing an earlier message rewinds files, agent memory and transcript
  together. A session can be forked, and anything under
  `/workspace/app` shows as a live preview that can be published as a
  shareable app.
- **nontainer** is the workspace as a library, for any agent loop to
  use: a stateful terminal and a Python tool over files and a cache
  that commit together and fork in O(1). It runs locally by default,
  with no Docker or cloud sandbox, or on a microVM through dud.
- **termish** is a shell written in pure Python: pipes, redirects,
  heredocs and variables over any filesystem object. It is not bash.
  When a command or a flag you expect is missing (`grep -P`, say),
  that is termish, not a broken machine; a session running on dud has
  a real bash instead.
- **monkeyfs** patches `open()`, `os.listdir()`, `os.stat()` and 30-odd
  other stdlib functions, so Python code reaches the workspace's
  virtual filesystem rather than the host's disk.
- **sandtrap** runs Python by rewriting its AST, under a whitelist of
  imports and attributes. It is a walled garden for cooperative code,
  not a defence against a hostile program. It runs code in-process, in
  a worker process (crash protection), or in a worker with kernel-level
  lockdown.
- **kvgit** is git-style versioning over a dict-like key-value store:
  commits, cheap branches, three-way merge, and tags that keep a
  commit alive. It stores to memory, disk, PostgreSQL or a browser's
  IndexedDB. A studio session is a kvgit branch, and the conversation
  lives in the same branch as the files.
- **reprobate** renders any Python value within a character budget,
  degrading from full values to type stubs to counts. It is why a large
  value you print comes back as `<list[str](200)>` rather than 200
  strings.
- **dud** ("a dumb firecracker") runs code in disposable machines: a
  plain subprocess, a vfkit microVM on macOS, or a firecracker microVM
  on Linux. A tree goes in, the code runs against a real filesystem,
  and a diff comes out. Versioning stays with nontainer.

## The READMEs

`references/<name>.md` is each package's README, taken from the copy
installed here; its first line names the version. A package that is not
installed has no file. Links inside point at the source repositories,
which are not in this workspace.

Treat the READMEs as the source of truth over this page. When you
explain or present these projects, say what they say, and leave out
features they do not mention.
