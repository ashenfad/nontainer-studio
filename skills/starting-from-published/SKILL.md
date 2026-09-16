---
name: starting-from-published
description: start a new app from one the human already published — list the published apps, read the session behind one, take its files or fork a delegate there
---

# Starting from an app the human already published

A closer starting point beats the reference templates. When the ask is
for something like an app the human already has, begin from that app
rather than from a blank page.

## Find it

```
sessions action="published"
```

One line per app, newest first: title, current version, and an **origin
tag**. The tag names the WHOLE session tree as it stood at that publish
— notes, data and uploads, not just the `app/` subtree the share URL
serves. An app whose current version carries no origin tag says so;
there is nothing to start from there.

## Take what you want from it

An origin tag is a ref like any other, so the `ws-git` verbs read it:

```sh
ws-git checkout <tag> -- app/       # that app's files, straight into this session
ws-git worktree add old <tag>       # the whole session under ./old, to read around in
ws-git diff <tag>                   # what yours already differs by
```

`checkout <tag> -- <paths>` makes exactly those paths match the tag, so
name `app/` for the app alone and narrower paths when you want one
handler or one frontend file.

## Or ask the agent that built it

```
sessions action="ask" fork_from="<tag>" inherit="full" task="..."
```

A delegate forked at the tag opens holding that session's tree, and
`inherit="full"` carries its conversation too — you are putting your
task to the agent that built the app, with its memory as of the
publish. `inherit="fresh"` gives you the files and no conversation.

Either way the delegate works on a branch of its own, and its answer
and branch come back to you; nothing it writes touches your files until
you merge or check out.
