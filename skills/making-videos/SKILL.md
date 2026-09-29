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
scene in CSS, a bar chart that Anime.js grows, and a closing card, with
a crossfade between each. Keep its scene structure when you change the
content; it already does the things below that are easy to get wrong.

What `vendor/` holds is listed in
`/workspace/skills/building-apps/references/vendor.md`. For video:
`hyperframes.runtime.js`, `hyperframes-player.js` and `anime.min.js`.
Two guides adapted from HyperFrames go deeper than this page:

```sh
cat /workspace/skills/making-videos/references/hyperframes/animejs.md        # before writing Anime.js: v4 API, splitText, seeded random
cat /workspace/skills/making-videos/references/hyperframes/css-animations.md # CSS keyframes: fill, delays, staggers, loops
```

## The composition

```html
<script src="vendor/hyperframes.runtime.js"></script>   <!-- first, always -->
<script src="vendor/anime.min.js"></script>

<div id="root" data-composition-id="main" data-start="0" data-duration="10"
     data-width="1920" data-height="1080">
  <div class="scene clip" data-start="0" data-duration="3.5">…</div>
  <div class="scene clip" data-start="3" data-duration="4">…</div>   <!-- overlaps by 0.5s -->
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
- **Keep 80px clear at every edge,** as the reference's padding does.
  Text against an edge reads as cut off, and headers, labels and numbers
  that share a scene need room not to run into each other.
- **Fonts come from `vendor/fonts.css`.** System fonts differ from
  machine to machine, so a video set in `system-ui` lays out differently
  for each viewer, and a web font from the internet does not load here.
  Link `vendor/fonts.css`, as the reference does, and pick from its seven
  families; the Fonts section of `vendor.md` says what each is for.
  Video is watched, not read: text 32px or larger and headlines 80px or
  larger on the 1920×1080 stage, and weights far apart (400 against 800).
- **Images come from the app:** put them under `app/`.

## Scenes and transitions

Cut from one scene straight to the next and the frame between them is
empty: the old scene is gone and the new one's content has not faded in
yet. Crossfade instead, as the reference does:

- **Overlap consecutive clips by about 0.5s:** the next scene's
  `data-start` is 0.5s before the previous one ends.
- **Fade the whole scene with two wrappers:** `.scene-in` fades in over
  the first 0.5s of the clip, and `.scene-out` inside it fades out over
  the last 0.5s (`--out` is the clip's duration minus 0.5). Two
  wrappers, not two animations on one element: opacities multiply, and
  one element with two opacity animations gets one of them wrong.
- The last scene holds to the end: its `--out` is its whole duration.

Slides, wipes and zooms work the same way on the wrappers: animate
`transform` or `clip-path` on `.scene-in` and `.scene-out` instead of,
or as well as, `opacity`.

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
3. **One step per property per element.** A second step that animates
   the same property of the same element (a fade in, then a fade out; a
   caret blinking as eight opacity steps) plays forwards and then scrubs
   backwards wrong. Use one step with keyframes, `opacity: [0, 1, 1, 0]`,
   or put the second motion on a wrapper.
4. **Start every animated element in CSS where its step starts.** Before
   a step begins, the element shows its CSS value the first time through
   but the step's first value once the scrubber has been past it. The
   reference's bars are `height: 0` and its labels `opacity: 0` in CSS
   for that reason.

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
- Two Anime.js steps on the same property of the same element. Forwards
  looks right; seek back and the element keeps a value from later.
- An element whose CSS disagrees with its Anime.js step's first value.
  Before the step, it shows one or the other depending on where the
  scrubber has been.
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
with `seek(seconds)`, `getTime()` and `getDuration()`. A seek takes
effect at once. Seek, assert what should be on screen, and take a
screenshot to look at the frame:

```json
[
  {"goto": "video.html"},
  {"assert": "window.__playerReady === true"},
  {"eval": "window.__player.getDuration()"},
  {"eval": "window.__player.seek(1.5)"},
  {"assert": "getComputedStyle(document.querySelector('#scene-title')).visibility === 'visible'"},
  {"screenshot": true},
  {"eval": "window.__player.seek(3.25)"},
  {"screenshot": true},
  {"eval": "window.__player.seek(6)"},
  {"assert": "parseFloat(getComputedStyle(document.querySelector('#q4')).height) > 500"},
  {"screenshot": true},
  {"eval": "window.__snap = () => [...document.querySelectorAll('#root *')].map(e => { const s = getComputedStyle(e); return [s.opacity, s.transform, s.width, s.height, s.visibility].join(); }).join('|'); window.__player.seek(6); window.__at6 = window.__snap(); true"},
  {"eval": "window.__player.seek(9.5)"},
  {"eval": "window.__player.seek(6)"},
  {"assert": "window.__snap() === window.__at6"}
]
```

What to cover, with the times changed to your video's:

- **The middle of each scene and the last second,** each with a
  screenshot. Look at them, not only at the asserts: that is where text
  off the stage and labels running into numbers show.
- **A scene boundary** (3.25 above, inside the first crossfade). Both
  scenes should be partly visible; an empty frame means a cut to black.
- **Scrubbing backwards,** the last four steps: record every element's
  state at a time, visit a later time, come back, and compare. A
  mismatch means something is not a function of the time; the usual
  cause is two Anime.js steps on one property.

Then `{"goto": "index.html"}` once, to see the player itself load.

## Done

A video is done when:

1. `test_app` passed seeking into every scene and onto a scene boundary,
   with a screenshot of each, and the backwards scrub matched. Your report
   says what you checked.
2. The player page loads it with no rejected requests.
3. Every animation is CSS, WAAPI or a registered Anime.js timeline, and
   the root's `data-duration` covers the last scene.
