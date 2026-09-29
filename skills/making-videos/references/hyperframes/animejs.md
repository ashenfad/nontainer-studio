> Adapted from HyperFrames v0.8.92, `skills/hyperframes-animation/adapters/animejs.md`.
> Apache-2.0, Copyright 2026 HeyGen, Inc.; the license is `LICENSE` beside this file.
> Changed for this workspace: Anime.js loads from `vendor/`; the module-build
> section, the CLI validation step, the links and the comparisons with other
> animation libraries are removed; the last two sections are added.

# Anime.js for HyperFrames

HyperFrames can seek Anime.js instances through its `animejs` runtime adapter. The composition owns the animation objects; HyperFrames owns the clock.

**This page targets v4 (4.5.0, the build in `vendor/`).** v4 is a hard break from v3 — there is no callable `anime()`, `easing:` is now `ease:`, and ease names lost their `ease` prefix. Writing v3 from memory produces a composition that throws or silently animates nothing.

## Contract

- Create animations or timelines synchronously during composition initialization.
- Set `autoplay: false` so Anime.js does not advance on its own clock.
- Register every returned animation or timeline on `window.__hfAnime` — **explicitly. There is no working auto-discovery on v4** (see Avoid).
- Use finite durations and loop counts.
- Avoid callbacks that mutate DOM based on wall-clock time, network state, or unseeded randomness.

The adapter seeks every registered instance with `instance.seek(timeMs)`, where `timeMs` is HyperFrames time **in milliseconds** (`ctx.time` seconds × 1000). It also calls `pause()` and `play()` on each instance; anything exposing those three methods works, whatever created it.

## Loading v4

```html
<!-- UMD: the global `anime` is a NAMESPACE OBJECT, not a function -->
<script src="vendor/anime.min.js"></script>
```

`anime.animate(...)`, `anime.createTimeline(...)`, `anime.utils.*`, `anime.svg.*`, `anime.stagger(...)`. **Calling `anime(...)` is a TypeError** — every v4 build (UMD and IIFE alike) assigns a namespace object to the global, so v3's `anime({ targets })` form cannot work no matter which v4 file you load.

## Basic Pattern

```html
<script>
  const anim = anime.animate(".mark", {
    x: 280, // v4 shorthand for translateX
    rotate: "1turn",
    opacity: [0, 1],
    duration: 1200,
    ease: "outExpo", // NOT easing: "easeOutExpo"
    autoplay: false,
  });

  window.__hfAnime = window.__hfAnime || [];
  window.__hfAnime.push(anim);
</script>
```

## Timeline Pattern

`createTimeline` replaces `anime.timeline`, and `add()` takes **targets as its first argument** — `add(targets, parameters, position)`:

```html
<script>
  const tl = anime.createTimeline({
    autoplay: false,
    defaults: { ease: "outCubic" }, // per-timeline defaults, not a bare `easing`
  });

  tl.add(".title", { y: [40, 0], opacity: [0, 1], duration: 650 });
  tl.add(".accent", { scaleX: [0, 1], duration: 450 }, 250); // 250 = time position

  window.__hfAnime = window.__hfAnime || [];
  window.__hfAnime.push(tl);
</script>
```

Position accepts a number, a label, `"+=250"` / `"-=100"`, `"<"` (previous **end**) and `"<<"` (previous **start**).

## Determinism

v4 ships `createSeededRandom(seed)` — use it instead of `Math.random()` when a composition needs scatter/jitter, so the same frame renders the same on every pass:

```js
const rnd = anime.createSeededRandom(1337);
anime.animate(".dot", { y: () => -40 * rnd(), duration: 800, autoplay: false });
```

`anime.utils.random()` / `randomPick()` / `shuffle()` are **not** seeded — they break frame-to-frame reproducibility.

## Good Uses

- Scene sequencing: Anime.js is the timeline library in this workspace.
- Small SVG and DOM flourishes where Anime.js syntax is compact.
- `splitText` / `scrambleText` for text that types on, splits into words or letters, or resolves from random characters.
- `svg.createDrawable` / `svg.morphTo` / `svg.createMotionPath` line-draw and path work.
- Multiple independent micro-animations pushed into the same registry.

## Avoid

- Leaving `autoplay` at the Anime.js default.
- **Relying on the adapter's `anime.running` auto-discovery — it cannot work on v4.** `running` is not among v4.5.0's exports (verified against the published bundle), so `discover()` returns immediately and any instance you did not `push()` is never seeked. Explicit registration is mandatory, not a nicety.
- `autoplay: onScroll(...)` — there is no scroll in a headless seek render, so the animation would never advance. Drive it off composition time instead.
- `waapi.animate()` for anything the adapter must seek — the adapter seeks via `.seek()`, and whether WAAPI-backed instances honor it is **unverified**. Use the JS engine (`animate`) for rendered compositions; `waapi` is an off-main-thread optimization for live pages.
- `createDraggable`, and any pointer-driven `createAnimatable` loop — input does not exist at render time.
- Infinite loops. Compute a finite repeat count from the composition duration (v4 `loop` counts **repeats**: `loop: 1` plays twice).
- Building animations in timers, promises, event handlers, or after async asset loads.

## Scrubbing backwards

Two things play correctly forwards and then show the wrong frame once the scrubber has moved back past them:

- **Two steps on the same property of the same element**, such as a fade in and a later fade out on one element, or a caret blinking as a run of opacity steps. Use one step with value keyframes, `opacity: [0, 1, 1, 0]`, which spreads the values evenly over the step's duration, or put the second motion on a wrapper element.
- **An element whose CSS disagrees with its step's first value.** Before a step begins, the element shows its CSS value on the first pass, and the step's first value after the scrubber has been past it. Give it the same starting value in CSS.

## Checking it

There is no HyperFrames CLI here. Check a composition with `test_app`, seeking to the times that matter and scrubbing backwards, as the **Checking it** section of `/workspace/skills/making-videos/SKILL.md` shows.
