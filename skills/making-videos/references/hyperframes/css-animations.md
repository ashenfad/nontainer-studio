> Adapted from HyperFrames v0.8.92, `skills/hyperframes-animation/adapters/css-animations.md`.
> Apache-2.0, Copyright 2026 HeyGen, Inc.; the license is `LICENSE` beside this file.
> Changed for this workspace: the comparison with other animation libraries,
> `data-track-index`, the CLI validation step and the links are removed, and
> a note on stacking two animations on one property is added.

# CSS Animations for HyperFrames

HyperFrames can seek CSS keyframe animations through its `css` runtime adapter. Use this for entrances, simple repeated motifs, background motion, shimmer, glow, masks, and non-sequenced decoration.

For scene choreography with many dependent steps, an Anime.js timeline is usually clearer. CSS animations work best when the motion belongs to one element and has a fixed duration.

## Contract

- Put the animated element in the DOM before runtime initialization finishes.
- Put timed elements inside a clip (or give them a `data-start`) so local animation time matches the clip.
- Use finite `animation-duration` and `animation-iteration-count` because the negative-delay fallback cannot represent unbounded duration in environments without WAAPI-backed CSS animations.
- Prefer `animation-fill-mode: both` so seeked states hold before and after active motion.
- Avoid wall-clock JavaScript, hover-triggered state, and class toggles that depend on user events.

The adapter discovers elements with computed `animation-name`, seeks their browser `Animation` handles when available, and falls back to pausing with negative `animation-delay`.

## Basic Pattern

```html
<div id="pulse-ring" class="clip pulse-ring" data-start="0" data-duration="4"></div>

<style>
  .pulse-ring {
    width: 280px;
    height: 280px;
    border: 4px solid rgba(255, 255, 255, 0.7);
    border-radius: 50%;
    animation-name: pulse-ring;
    animation-duration: 1200ms;
    animation-timing-function: cubic-bezier(0.2, 0, 0, 1);
    animation-iteration-count: 3;
    animation-fill-mode: both;
  }

  @keyframes pulse-ring {
    from {
      opacity: 0;
      transform: scale(0.82);
    }
    35% {
      opacity: 1;
    }
    to {
      opacity: 0;
      transform: scale(1.18);
    }
  }
</style>
```

## Stagger Pattern

Use CSS custom properties to avoid duplicating keyframes:

```html
<div class="clip dots" data-start="1" data-duration="3">
  <span style="--i: 0"></span>
  <span style="--i: 1"></span>
  <span style="--i: 2"></span>
</div>

<style>
  .dots span {
    display: inline-block;
    width: 18px;
    height: 18px;
    margin-right: 10px;
    border-radius: 50%;
    background: currentColor;
    animation: dot-pop 900ms ease-out both;
    animation-delay: calc(var(--i) * 120ms);
  }

  @keyframes dot-pop {
    from {
      opacity: 0;
      transform: translateY(18px) scale(0.75);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }
</style>
```

## One animation per property

Two animations on one element that both set `opacity` (a fade in and a later fade out, say) do not combine: with `fill-mode: both`, the later one in the list holds its first frame over the whole clip and the earlier one never shows. Put the second on a wrapper element instead; opacities multiply through nesting. The reference composition's `.scene-in` and `.scene-out` wrappers are this pattern.

## Good Uses

- Entrances: a line of text rising in, a card popping up, a staggered list.
- Scene crossfades, on wrapper elements.
- Decorative loops with a known repeat count.
- Mask, glow, shimmer, grain, and subtle parallax layers.

## Avoid

- Infinite CSS animations. Prefer a finite iteration count covering the visible duration.
- Animating layout properties like `top`, `left`, `width`, or `height` when transforms work.
- Relying on hover, focus, scroll, or media queries to trigger render-critical motion.
- Changing animation classes after startup unless another deterministic timeline controls that change.

## Composition Duration

CSS-only compositions have no timeline object, so the runtime can infer duration from the longest running animation's computed end time (`animation-delay` + `animation-duration` × finite `animation-iteration-count`, per element with `data-start` added as an offset). An infinite animation has no finite end time, so nothing can be inferred from it. Write `data-duration` on the root `[data-composition-id]` element either way: it is the one length every part of the video agrees on.

## Checking it

There is no HyperFrames CLI here. Check a composition with `test_app`, seeking to the times that matter and scrubbing backwards, as the **Checking it** section of `/workspace/skills/making-videos/SKILL.md` shows.
