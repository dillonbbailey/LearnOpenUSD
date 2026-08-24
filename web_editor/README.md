# USD Scene Editor (browser)

A local web app for inspecting and editing OpenUSD stages: 3D viewport,
scenegraph tree, property inspector, composition tooling, a live USDA pane, and
a Python console wired to the open stage.

The browser is only the front end. All USD work happens in a small Python
server using the real `pxr` API, so composition, variants, instancing and value
resolution behave exactly as they do in usdview — no reimplemented subset.

```
browser (three.js viewport, tree, inspector, console)
        │  JSON over HTTP
        ▼
local Python server  ──►  pxr / usd-core
```

## Running it

From the repo root:

```bash
uv run python web_editor/server.py
```

That opens <http://localhost:8000>. Useful flags:

```bash
uv run python web_editor/server.py --open /path/to/scene.usda   # load a file at startup
uv run python web_editor/server.py --port 8080                  # different port
uv run python web_editor/server.py --no-browser                 # don't auto-launch
```

No new dependencies — it uses `tornado` and `numpy`, both already in this
project's environment, plus `usd-core`.

**three.js loads from a CDN** (`unpkg.com`) via an import map, so the first load
needs network access. Everything else is local.

## What it does

**View and navigate**
- Orbit / pan / zoom viewport; `F` frames the selection, double-click a tree row
  to frame it
- Meshes, and the implicit surfaces (Sphere, Cube, Cylinder, Cone, Capsule, Plane)
- Scenegraph tree showing type, authored composition arcs, and instance proxies;
  inactive prims are struck through, invisible ones italicized
- Click geometry in the viewport to select it, or click a tree row
- `#/World/Ball` in the URL deep-links to a prim — shareable and bookmarkable

**Edit**
- Create, delete, and rename prims; change prim type and kind
- Translate / rotate / scale via `UsdGeom.XformCommonAPI`
- Edit scalar and vector attribute values inline
- Toggle `active` and `instanceable`; set the stage's default prim
- Save, Save As (`.usda` text or `.usdc` binary, chosen by extension)
- Undo / redo (`Ctrl-Z` / `Ctrl-Shift-Z`), including changes made from the console

**Composition**
- Add references, payloads, inherits, and specializes
- Add sublayers to the root layer
- Create variant sets and switch variant selections live
- **Prim stack** pane showing which layers contribute opinions, strongest first
- Click any attribute name for its per-layer opinion stack — the winning opinion
  is marked, which makes "why is this value what it is?" answerable directly
- Switch the **edit target** so authoring lands in the layer you choose

**Live USDA pane**
- Root layer, any single layer in the stack, or the fully flattened composed
  stage — updates after every edit

**Python console**
- Runs against the live stage with `stage`, `Usd`, `UsdGeom`, `UsdShade`,
  `UsdUtils`, `Sdf`, `Gf`, `Tf`, `Vt`, and `session` in scope
- Persists state between submissions like a REPL; `↑`/`↓` walk history
- Enter runs, Shift+Enter inserts a newline
- Expression results echo; every submission is undoable

## Security

**This server runs arbitrary Python and reads and writes local files.** The
Python console is a deliberate feature, not an oversight — but it means anyone
who can reach the port controls the machine.

It binds to `127.0.0.1` by default. `--host` exists for container and WSL port
forwarding and prints a warning when used. Do not expose it to an untrusted
network, and do not put it behind a public reverse proxy.

## Layout

| File | Role |
| --- | --- |
| `server.py` | Tornado HTTP shell; one JSON route per operation |
| `usd_bridge.py` | All `pxr` interaction: `EditorSession` owns the open stage |
| `static/index.html` | Layout and three.js import map |
| `static/app.js` | Tree, inspector, composition pane, USDA pane, console |
| `static/viewport.js` | three.js scene, picking, framing |
| `static/style.css` | Dark theme |

Undo is snapshot-based: the root layer is serialized to a string before each
mutation (50 deep). USD has no native undo stack, and full-layer snapshots are
more predictable than a command log at this size.

## Known limitations

- **Materials and shading are not rendered.** Geometry draws with
  `primvars:displayColor`, falling back to flat grey. There is no UsdPreviewSurface
  or texture support in the viewport.
- **Array-valued attributes are read-only** in the inspector. Use the Python
  console to author them.
- **Point instancer prototypes** reuse geometry only when the prototype prim was
  itself drawn; otherwise instances render as unit boxes.
- **No time slider.** Everything evaluates at the default time code.
- **No live reload** if the file changes on disk underneath you.
- **Rename and delete** operate on the current edit target, so they fail on prims
  defined in a stronger or referenced layer. The error message says so.
- Cameras and lights appear in the tree and inspector but draw no viewport gizmo.
