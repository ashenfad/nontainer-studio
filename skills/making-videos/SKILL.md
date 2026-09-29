---
name: making-videos
description: make a video as an app — an HTML composition of timed scenes animated with CSS and Anime.js, played by a player page with a scrubber; how to check frames with test_app
---

# Making videos in this workspace

A video here is an app. You write an HTML **composition**: a fixed-size
stage, scenes that are shown for a stretch of time, and animations. The
HyperFrames runtime runs it on a clock of its own, so it can be played,
paused and scrubbed to any moment and always shows the same frame there.
A player page shows it with controls, and it previews and publishes like
any other app.

Two things this is not:

- **Not an MP4.** Nothing here renders a video file. The published app
  is the video; a human who wants a file can screen-record it.
- **Not GSAP.** HyperFrames' own examples animate with GSAP, and GSAP is
  not here (its license rules it out). `gsap` is undefined; loading it
  from a CDN fails, because apps have no network. Animate with CSS
  keyframes or Anime.js, below.

## Do this first

Two reference files make a working 10-second video. Read them, then copy
them into the app and change the content:

```sh
cat /workspace/skills/making-videos/references/video.html
cat /workspace/skills/making-videos/references/index.html
cp /workspace/skills/making-videos/references/index.html /workspace/app/index.html
cp /workspace/skills/making-videos/references/video.html /workspace/app/video.html
```

`index.html` is the player page and rarely needs more than a title.
`video.html` is the composition, and it is where the work is: a title
scene in CSS, a bar chart that Anime.js grows, and a closing card.

What `vendor/` holds is listed in
`/workspace/skills/building-apps/references/vendor.md`. For video:
`hyperframes.runtime.js`, `hyperframes-player.js` and `anime.min.js`.

## The composition

```html
<script src="vendor/hyperframes.runtime.js"></script>   <!-- first, always -->
<script src="vendor/anime.min.js"></script>

<div id="root" data-composition-id="main" data-start="0" data-duration="10"
     data-width="1920" data-height="1080">
  <div class="scene clip" data-start="0" data-duration="3">…</div>
  <div class="scene clip" data-start="3" data-duration="4">…</div>
</div>
```

- **The runtime tag is not optional.** A composition without it loads,
  and the player then tries to fetch the runtime from the internet,
  which fails here: the video sits on its first frame and never moves.
- **The root's `data-duration` is the video's length** in seconds. Write
  it in the HTML: it is read once, before any script runs, so a script
  that changes it later changes nothing.
- **A clip is any element with `data-start` and `data-duration`,** shown
  from its start for its duration and hidden before and after. Scenes
  are clips. Give them `class="clip"` as well: the runtime does not read
  it, but it is the convention, and the reference's `.scene` rule is what
  makes a scene fill the stage. Clips may overlap in time; which one is
  in front is CSS `z-index`. (HyperFrames examples also carry
  `data-track-index`. That is a lane in HyperFrames' own editor, which
  is not here, and the runtime ignores it.)
- **The runtime owns a clip's visibility.** Never animate `display` or
  `visibility` on a clip element; fade its content with `opacity`, or
  animate a child.
- **The stage is fixed.** `html, body` are exactly `data-width` ×
  `data-height` pixels with `overflow: hidden`, and scenes are
  `position: absolute; inset: 0`. The player scales the whole stage to
  fit, so size text in px for 1920×1080, not in vw or %.
- **Fonts and images come from the app.** A web font is a link to the
  internet, so use the system font stack (`system-ui, sans-serif`) or a
  font file in the workspace, and put images under `app/`.

## Animating

Three ways, all seeked by the runtime. Pick per element; one composition
can use all of them.

**CSS keyframes, for entrances and simple motion.** Timed relative to
the clip they are in: an animation inside a scene that starts at 3s
begins at 3s. Use `both` so the element holds its first frame before the
animation and its last after it, and `animation-delay` to stagger.

```css
.title { animation: rise 1.2s ease-out both; }
.subtitle { animation: rise 1.2s ease-out 0.4s both; }
@keyframes rise { from { opacity: 0; transform: translateY(40px); } to { opacity: 1; transform: none; } }
```

**Anime.js, for anything sequenced.** A timeline of steps, staggers and
numbers that count. Two rules the runtime depends on:

1. `autoplay: false`, and push every timeline or animation onto
   `window.__hfAnime`. The runtime seeks only what is registered; an
   unregistered animation plays once on its own clock and ignores the
   scrubber.
2. **Positions are the video's time, in milliseconds.** Unlike CSS,
   Anime.js does not know which scene it is in: a step for the scene that
   starts at 3s goes at `3000` or later.

```js
const tl = anime.createTimeline({ autoplay: false });
tl.add('#q1', { height: [0, 230], duration: 1200, ease: 'outCubic' }, 3300)
  .add('#q2', { height: [0, 365], duration: 1200, ease: 'outCubic' }, 3550);
window.__hfAnime = window.__hfAnime || [];
window.__hfAnime.push(tl);
```

This is Anime.js **v4**. `anime` is a namespace: `anime.animate(targets,
params)`, `anime.createTimeline()`, `anime.stagger(100)`. The v3 form
`anime({ targets, ... })` does not exist and throws *anime is not a
function*. Easings are `'outCubic'`, `'inOutQuad'`, `'linear'`, not v3's
`'easeOutCubic'`.

**WAAPI (`element.animate(...)`)** also works, and like Anime.js it runs
on the video's clock: `delay` is the absolute start in milliseconds. Use
`fill: 'both'`.

## What breaks scrubbing

The runtime can show any moment only if every frame is a function of the
time. These are not, and each one shows up as a video that plays once
and then scrubs wrong:

- `setTimeout`, `setInterval` and `requestAnimationFrame` loops that move
  things. Put the motion in a CSS animation or an Anime.js timeline.
- `Math.random()` and `Date.now()` when drawing. Use fixed values, or a
  seeded generator written out in the page.
- Anime.js animations with `autoplay` left on or not pushed onto
  `window.__hfAnime`.
- Loops that never end: CSS `animation-iteration-count: infinite`,
  Anime.js `loop: true`. Give a pulse a finite count that fits its scene.
- `<video>` and `<audio>` are timed by the runtime when they are clips
  with `data-start`; do not call `.play()` on them yourself.

## Scenes in separate files

A composition can pull a scene from another file:

```html
<div data-composition-id="chart" data-composition-src="chart.html"
     data-start="3" data-duration="4"></div>
```

where `chart.html` holds a `<template>` wrapping a `<div
data-composition-id="chart" …>`. Prefer ONE file for a video under a
minute or so. When you do split, remember the timing rule: CSS inside
the sub-composition is timed from its start, but Anime.js and WAAPI
inside it still use the video's absolute time (3000 for a scene at 3s).

## Checking it

`test_app` loads the player page by default. Go to the composition
itself to ask it questions: `window.__player` is the runtime's handle,
with `seek(seconds)`, `getTime()` and `getDuration()`. Seek, then assert
what should be on screen and take a screenshot to look at the frame:

```json
[
  {"goto": "video.html"},
  {"assert": "window.__playerReady === true"},
  {"eval": "window.__player.getDuration()"},
  {"eval": "window.__player.seek(1.5)"},
  {"assert": "getComputedStyle(document.querySelector('#scene-title')).visibility === 'visible'"},
  {"screenshot": true},
  {"eval": "window.__player.seek(6)"},
  {"assert": "parseFloat(getComputedStyle(document.querySelector('#q4')).height) > 500"},
  {"screenshot": true}
]
```

Check at least the middle of each scene and the last second. A
screenshot is where layout problems show — text off the stage, a scene
still visible under the next one — so look at them, not only at the
asserts. Then `{"goto": "index.html"}` once, to see the player itself
load.

## Done

A video is done when:

1. `test_app` passed seeking into every scene, with a screenshot of each,
   and your report says what you checked.
2. The player page loads it with no rejected requests.
3. Every animation is CSS, WAAPI or a registered Anime.js timeline, and
   the root's `data-duration` covers the last scene.
