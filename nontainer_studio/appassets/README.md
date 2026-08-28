# appassets — browser libraries served to agent-authored apps

These files are **not** part of studio's own frontend (that's `frontend/`,
built into `static/`). They are served *to the apps an agent builds*,
through `AppsConfig.static_assets`, at `vendor/…`.

They are committed for the same reason the frontend build is: a
deployment should need nothing from the network. That is what makes an
air-gapped studio work — an agent on a locally-hosted model with no
internet can still write a chart that renders.

| file | source | version | sha256 |
|---|---|---|---|
| `plotly.min.js` | `https://cdn.plot.ly/plotly-3.7.0.min.js` | 3.7.0 | `8ef4c6ab1369f0019611cbcd2d5b8aafef23e5d19ef58c39d4b4249831fe2180` |
| `tailwind.js` | `https://cdn.tailwindcss.com/3.4.17` | 3.4.17 | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` |

Both are MIT licensed.

## Regenerating

```sh
./scripts/fetch-appassets.sh     # then verify the printed checksums
```

The script carries the reasoning for each pin. Two worth repeating,
because they are the kind of choice that looks arbitrary later:

**Plotly 3.7.0, not 4.x.** 4.0.0 removed the `scattermapbox` /
`choroplethmapbox` trace names. Those are what a model writes from
training data, so an agent would produce valid-looking code that fails
only here, with no hint that the name is the problem. 3.x keeps them
alongside the new `map` names.

**The full build, not `plotly-basic` or `plotly-cartesian`.** The apps
notes point agents at `scattergeo`/`choropleth` for maps, and only the
full build carries geo traces. A partial bundle would save ~3.6MB and
buy a cliff the agent cannot see coming.

## After changing anything here

Update `frontend_notes` in `nontainer_studio/sessions.py`. The bytes and
the sentence that describes them are one decision: a library the agent
isn't told about may as well not be here, and one it's told about that
isn't here is worse.
