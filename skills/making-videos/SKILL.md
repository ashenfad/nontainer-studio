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
mkdir -p /workspace/app
cp /workspace/skills/making-videos/references/index.html /workspace/app/index.html
cp /workspace/skills/making-videos/references/video.html /workspace/app/video.html
```

`index.html` is the player page and rarely needs more than a title.
Keep its `sandbox-origin="opaque"`: without it the player frames the
composition on the page's own origin, and every `test_app` run logs a
browser warning that the sandbox could be escaped.
`video.html` is the composition, and it is where the work is: a title
scene in CSS, a bar chart that Anime.js grows, and a closing card, with
a crossfade between each. Keep its scene structure when you change the
content; it already does the things below that are easy to get wrong.

A third, `narrated-scene.html`, is one scene in a file of its own,
carrying its own narration: the shape to copy when a video is split into
files (**Scenes in separate files**, below).

What `vendor/` holds is listed in
`/workspace/skills/building-apps/references/vendor.md`. For video:
`hyperframes.runtime.js`, `hyperframes-player.js` and `anime.min.js`.
Two guides adapted from HyperFrames go deeper than this page:

```sh
cat /workspace/skills/making-videos/references/hyperframes/animejs.md        # before writing Anime.js: v4 API, splitText, seeded random
cat /workspace/skills/making-videos/references/hyperframes/css-animations.md # CSS keyframes: fill, delays, staggers, loops
cat /workspace/skills/making-videos/references/narrated-scene.html           # a scene with its narration, in a file of its own
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
- **Sound and video clips carry `crossorigin`:** `<audio class="clip"
  src="music.mp3" crossorigin …>`. The player frames the composition on
  an opaque origin (see `index.html`), so without the attribute the
  runtime cannot route the sound through Web Audio. It falls back to
  plain playback, which loses fades, effects, groups and gain above 1,
  and logs `runtime_web_audio_bypass`. With it, the app serves the file
  in a way the runtime can use, and nothing is lost.

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

## Narration, music and generated images

If your Python has `media` (the primer describes it), a video can be
narrated and illustrated.

- **Write the whole script, then voice it in one call.** One line per
  scene, spoken together:
  ```python
  clips = media.speech([
      {"text": "[warmly] Meet nontainer-studio.", "path": "app/audio/s1.wav"},
      {"text": "Every turn is a commit.", "path": "app/audio/s2.wav"},
  ])
  ```
  `media.speech` writes each file itself, as WAV, so name it `*.wav`.
  Each result's `seconds` is that line's length.
- **Two path bases.** `media` paths count from `/workspace`
  (`app/audio/s1.wav`); paths in the HTML count from the composition,
  which is in `app/` (`audio/s1.wav`). Copying one into the other is a
  missing file.
- **The voice sets the length.** A scene lasts its line's `seconds`
  plus about 0.4s before the voice starts and a second after it ends,
  so the picture follows the voice rather than cutting it off.
- **Place each line as a clip in its scene:** `<audio class="clip"
  src="audio/s1.wav" data-start="3.4" data-duration="3.2" crossorigin>`,
  with the `crossorigin` the rule above asks for. In one file,
  `data-start` is the video's time: 0.4s after its scene starts. In a
  scene file of its own it counts from that scene's start, as
  `narrated-scene.html` shows.
- **Music goes under the voice, not over it.** Compose a bed with
  `media.music`, naming the genre, instruments, tempo and mood, and "no
  vocals" when it plays under narration:
  ```python
  bed = media.music("Warm lo-fi bed for a product video: soft keys, "
                    "brushed drums, 85 bpm, no vocals", "app/audio/bed.mp3")
  ```
  It writes an MP3, so name it `*.mp3`. A clip is about 30 seconds. For
  a longer video, pass `length="song"` and ask for the video's length in
  the prompt ("a 90-second instrumental"). What comes back is only
  roughly that long, so use the `seconds` it returns.
- **Place the bed as a quiet clip across the video,** fading in and out:
  `<audio class="clip" src="audio/bed.mp3" data-start="0"
  data-duration="24" data-volume="0.25" data-fade-in="1"
  data-fade-out="2" crossorigin>`. `data-volume` runs from 0 to 1, and
  about 0.25 keeps the voice clear over it. The fades are in seconds. A
  `data-duration` shorter than the music ends it there, after its fade.
- **A song with vocals comes back with its lyrics,** each with the
  second it starts (`{"at": 2.2, "line": "..."}`), ready to time
  captions to.
- **Generate pictures with `transparent=True`** for anything that sits
  on a background: characters, icons, objects. Pass earlier images as
  `references` to keep a character the same from scene to scene, and
  look at each with `view_image` before placing it.
- **Keep words in the HTML,** not in generated images: image models
  misspell, and HTML text stays sharp at any size and animates.

## Scenes in separate files

A composition can pull a scene from another file:

```html
<div data-composition-id="chart" data-composition-src="chart.html"
     data-start="3" data-duration="4"></div>
```

where `chart.html` holds a `<template>` wrapping a `<div
data-composition-id="chart" …>`. Prefer ONE file for a video under a
minute or so. When you do split, start from `narrated-scene.html`, and
remember:

- **Timing.** CSS animations and sound or video clips inside the scene
  file count from the scene's start, wherever it is placed. Anime.js and
  WAAPI inside it still use the video's time (3000 for a scene at 3s).
  A scene written before its place is known animates with CSS only.
- **Style.** Every scene file lands in the same page. Its own rules are
  scoped to its id (`[data-composition-id="chart"] .title`) and its
  keyframes carry the id in their names; the wrappers, fonts and
  colours come from the composition.
- **Length.** The scene's `data-duration` is written twice, in the file
  and where the composition places it. Keep the two the same.

<!--if:delegation-->
## Long videos with delegates

A video of more than a minute, or more than five or six scenes, can be
built a scene per delegate, in parallel. Each delegate is a whole agent
on your model, so use three to six of them, not twenty, and build a
short video yourself.

This is for the session directing the whole video. **If you are a
delegate given a scene, build it yourself:** a scene is the unit of the
work, and splitting it again only multiplies the cost.

1. **Set what the scenes share, first.** Write the composition: its
   styles (fonts, colours, the scene wrappers), the root, and a plan of
   the scenes with their narration. Pick the voice. Delegates start from
   your files as they are when you ask, so this has to be written
   before you ask.
2. **One delegate per scene, each owning its files:**
   `app/<id>.html`, `app/audio/<id>*.wav`, `app/img/<id>*.png`. A
   delegate must not touch the composition or another scene's files;
   that is what keeps bringing the work back conflict-free.
3. **A task that stands alone.** A delegate starts without this
   conversation, so its task says everything: the scene's id and its
   narration, the voice, that its file starts from
   `skills/making-videos/references/narrated-scene.html`, to build the
   scene itself without delegating, to animate with CSS only, to voice
   the line with `media.speech` and size the scene
   from `seconds`, to check its scene with `test_app` (`goto` the
   composition with the scene placed, or the scene file), and to reply
   with the scene's length and the files it wrote.
4. **Then end your turn,** saying what you are waiting for. Each answer
   starts your next turn on its own; do not poll. As each arrives, take
   its work with `ws-git merge <delegate>`: it touched only its own
   files, so the merge is clean. (`ws-git checkout <delegate> --
   <paths>` takes exactly the files it listed, and no more; naming a
   folder makes that folder match the delegate's, which drops what the
   other scenes put there.)
5. **Stitch.** Place each scene in the composition with
   `data-composition-src`, its `data-start` the running total of the
   lengths before it less 0.5s for each crossfade, and set the root's
   `data-duration` to the end of the last. Then check the whole video
   as below.
<!--endif-->

## Checking it

`test_app` loads the player page by default. Go to the composition
itself to ask it questions: `window.__player` is the runtime's handle,
with `seek(seconds)`, `getTime()` and `getDuration()`. A seek takes
effect at once.

Call `test_app` with **`viewport: "hd"`**. The stage is 1920×1080, and at
the default size every screenshot of it loses its right third. Then one
call checks the whole video:

```json
[
  {"goto": "video.html"},
  {"assert": "window.__playerReady === true"},
  {"eval": "window.__player.getDuration()"},
  {"eval": "window.__player.seek(1.5)"},
  {"assert": "getComputedStyle(document.querySelector('#scene-title')).visibility === 'visible'"},
  {"screenshot": true, "grid": "scenes", "label": "1.5s title"},
  {"eval": "window.__player.seek(3.25)"},
  {"screenshot": true, "grid": "scenes", "label": "3.25s crossfade"},
  {"eval": "window.__player.seek(6)"},
  {"assert": "parseFloat(getComputedStyle(document.querySelector('#q4')).height) > 500"},
  {"screenshot": true, "grid": "scenes", "label": "6s chart"},
  {"eval": "window.__player.seek(9.5)"},
  {"screenshot": true, "grid": "scenes", "label": "9.5s closing"},
  {"eval": "window.__snap = () => [...document.querySelectorAll('#root *')].map(e => { const s = getComputedStyle(e); return [s.opacity, s.transform, s.width, s.height, s.visibility].join(); }).join('|'); window.__player.seek(6); window.__at6 = window.__snap(); true"},
  {"eval": "window.__player.seek(9.5)"},
  {"eval": "window.__player.seek(6)"},
  {"assert": "window.__snap() === window.__at6"}
]
```

The screenshots that name a `grid` come back as **one** image, each frame
captioned with its `label`, and a grid counts once against the
screenshot limit: up to 12 frames, so a long video still fits one call.
What to cover, with the times changed to your video's:

- **The middle of each scene and the last second,** each a frame in the
  grid. Look at the image, not only at the asserts: that is where text
  off the stage and labels running into numbers show.
- **A scene boundary** (3.25 above, inside the first crossfade). Both
  scenes should be partly visible; an empty frame means a cut to black.
- **Scrubbing backwards,** the last four steps: record every element's
  state at a time, visit a later time, come back, and compare. A
  mismatch means something is not a function of the time; the usual
  cause is two Anime.js steps on one property.

A grid's frames are small, and small text and emoji in them can look
broken when they are fine. To read one frame in detail, take a plain
`{"screenshot": true}` at that moment as well, before changing anything
it shows. Then `{"goto": "index.html"}` once, to see the player itself
load.

**Narration** needs checking too, and a paused frame says nothing about
it: after a seek, a clip's `currentTime` reads 0 even when it is fine.
Check that each clip loaded, then play a moment of one scene and read
where its voice is:

```json
[
  {"eval": "[...document.querySelectorAll('audio.clip')].map(a => a.readyState === 4 && !a.error)"},
  {"eval": "window.__player.seek(10.2); window.__player.play(); true"},
  {"wait": 1000},
  {"eval": "[window.__player.getTime(), document.querySelector('#intro-audio').currentTime]"},
  {"eval": "window.__player.pause(); true"}
]
```

Every clip should read `true`. The last answer is the video's time and
the voice's, read at one moment, so they differ by where the voice
starts: for a scene placed at 10s with its voice 0.4s in, by 10.4, as in
`[11.4, 1.0]`.

## Done

A video is done when:

1. `test_app` at `viewport: "hd"` passed seeking into every scene and
   onto a scene boundary, with a grid frame of each, and the backwards
   scrub matched. Your report says what you checked.
2. The player page loads it with no rejected requests.
3. Every animation is CSS, WAAPI or a registered Anime.js timeline, and
   the root's `data-duration` covers the last scene.
4. If it is narrated, every clip loaded, and a played moment put the
   voice where the scene's timing says.
