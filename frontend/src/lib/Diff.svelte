<script>
    // Two sides of a file as a line diff. Shared on purpose: an edit in
    // the transcript and an unpublished change in the changes tab are
    // the same thing seen from two places, and rendering them
    // differently would make a reader work out that they match.
    import { lineDiff } from './diff.js'

    // Text, both of them. A side the file does not have (it was added,
    // or it was removed) is the empty string, which reads as the whole
    // file arriving or leaving — which is what happened.
    let { old = '', new: latest = '' } = $props()

    const lines = $derived(lineDiff(old ?? '', latest ?? ''))
</script>

<pre class="diff">{#each lines as line, i (i)}<span
            class="diff-{line.type}"
            >{line.type === 'removed'
                ? '− '
                : line.type === 'added'
                  ? '+ '
                  : '  '}{line.text}
</span>{/each}</pre>

<style>
    .diff {
        font-size: 0.72rem;
        background: rgba(255, 255, 255, 0.05);
        border-radius: 6px;
        padding: 0.4rem 0.4rem;
        margin: 0.25rem 0 0;
        overflow-x: auto;
        max-height: 260px;
        overflow-y: auto;
        /* no wrapping: a soft-wrapped code line breaks the column the
           +/− markers line up in */
        white-space: pre;
        line-height: 1.45;
    }
    .diff span {
        display: block;
        /* highlight bars span the scrolled width, not just the viewport */
        width: max-content;
        min-width: 100%;
    }
    .diff-removed {
        background: color-mix(in srgb, var(--error) 14%, transparent);
        color: color-mix(in srgb, var(--error) 70%, var(--text));
    }
    .diff-added {
        background: color-mix(in srgb, var(--success) 12%, transparent);
        color: color-mix(in srgb, var(--success) 65%, var(--text));
    }
    .diff-context {
        color: var(--text-muted);
    }
</style>
