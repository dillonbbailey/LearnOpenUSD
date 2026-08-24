# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local web server for the browser-based USD scene editor.

A thin Tornado shell over :mod:`usd_bridge`. Every route is a small JSON
request/response pair; all USD work happens in the bridge.

Run it::

    uv run python web_editor/server.py
    uv run python web_editor/server.py --open path/to/scene.usda --port 8080

SECURITY: this server exposes a Python console with full access to the process,
and file-open/save against the local filesystem. It binds to 127.0.0.1 by
design. Do not expose it to a network you do not control -- ``--host`` exists
for container/WSL port-forwarding and warns loudly when used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from pathlib import Path

import tornado.ioloop
import tornado.web
import tornado.websocket

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usd_bridge import EditorError, EditorSession  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# One process, one open stage. The console namespace persists so the REPL keeps
# state between submissions.
SESSION = EditorSession()
CONSOLE_NS: dict = {}


class ApiHandler(tornado.web.RequestHandler):
    """Base handler: JSON body parsing, uniform error shape, no caching."""

    def set_default_headers(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")

    @property
    def body(self) -> dict:
        if not self.request.body:
            return {}
        try:
            return json.loads(self.request.body)
        except json.JSONDecodeError as exc:
            raise tornado.web.HTTPError(400, str(exc)) from exc

    def respond(self, payload: dict | None = None) -> None:
        """Reply with the requested payload plus fresh stage info.

        The client needs stage state (dirty flag, undo availability, layer list)
        after nearly every call, so folding it into each response removes a
        round trip and keeps the UI from drifting out of sync.
        """
        out = dict(payload or {})
        try:
            out["stage"] = SESSION.stage_info()
        except EditorError:
            out["stage"] = None
        self.write(json.dumps(out))

    def write_error(self, status_code: int, **kwargs) -> None:
        message = "Internal error"
        exc_info = kwargs.get("exc_info")
        if exc_info:
            exc = exc_info[1]
            message = getattr(exc, "log_message", None) or str(exc) or message
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({"error": message}))


def api(func):
    """Turn EditorError into a 400 with a message the UI can display."""
    def wrapper(self: ApiHandler, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except EditorError as exc:
            raise tornado.web.HTTPError(400, str(exc)) from exc
        except tornado.web.HTTPError:
            raise
        except Exception as exc:  # unexpected -- surface it rather than 500 blind
            raise tornado.web.HTTPError(400, f"{type(exc).__name__}: {exc}") from exc
    return wrapper


class StageHandler(ApiHandler):
    @api
    def get(self):
        self.respond({"scenegraph": SESSION.scenegraph()})

    @api
    def post(self):
        action = self.body.get("action")
        if action == "new":
            SESSION.new_stage()
        elif action == "open":
            SESSION.open(self.body["path"])
        elif action == "save":
            SESSION.save(self.body.get("path"))
        elif action == "undo":
            SESSION.undo()
        elif action == "redo":
            SESSION.redo()
        elif action == "editTarget":
            SESSION.set_edit_target(self.body["identifier"])
        elif action == "defaultPrim":
            SESSION.set_default_prim(self.body["path"])
        else:
            raise EditorError(f"Unknown stage action: {action}")
        self.respond({"scenegraph": SESSION.scenegraph()})


class GeometryHandler(ApiHandler):
    @api
    def get(self):
        self.write(json.dumps(SESSION.geometry()))


class PrimHandler(ApiHandler):
    @api
    def get(self):
        self.write(json.dumps(SESSION.prim_detail(self.get_argument("path"))))

    @api
    def post(self):
        body = self.body
        action = body.get("action")
        selection = body.get("path")

        if action == "create":
            selection = SESSION.create_prim(body.get("parent", "/"), body["name"],
                                            body.get("type", "Xform"))
        elif action == "delete":
            SESSION.delete_prim(body["path"])
            selection = None
        elif action == "rename":
            selection = SESSION.rename_prim(body["path"], body["name"])
        elif action == "transform":
            SESSION.set_transform(body["path"], body.get("translate"),
                                  body.get("rotate"), body.get("scale"))
        elif action == "attribute":
            SESSION.set_attribute(body["path"], body["name"], body["value"])
        elif action == "metadata":
            SESSION.set_metadata(body["path"], body["key"], body["value"])
        else:
            raise EditorError(f"Unknown prim action: {action}")

        self.respond({"scenegraph": SESSION.scenegraph(), "selection": selection})


class CompositionHandler(ApiHandler):
    @api
    def get(self):
        self.write(json.dumps(SESSION.attribute_stack(
            self.get_argument("path"), self.get_argument("attribute"))))

    @api
    def post(self):
        body = self.body
        action = body.get("action")
        if action == "arc":
            SESSION.add_arc(body["path"], body.get("assetPath", ""),
                            body["arc"], body.get("primPath", ""))
        elif action == "sublayer":
            SESSION.add_sublayer(body["assetPath"])
        elif action == "addVariantSet":
            SESSION.add_variant_set(body["path"], body["name"],
                                    body.get("variants", []))
        elif action == "setVariant":
            SESSION.set_variant(body["path"], body["name"], body["variant"])
        else:
            raise EditorError(f"Unknown composition action: {action}")
        self.respond({"scenegraph": SESSION.scenegraph()})


class UsdaHandler(ApiHandler):
    @api
    def get(self):
        text = SESSION.usda(self.get_argument("mode", "root"),
                            self.get_argument("identifier", None))
        self.write(json.dumps({"text": text}))


class PythonHandler(ApiHandler):
    @api
    def post(self):
        result = SESSION.run_python(self.body.get("code", ""), CONSOLE_NS)
        self.respond(result)


class BrowseHandler(ApiHandler):
    """Minimal server-side file picker; the browser cannot list directories."""

    @api
    def get(self):
        raw = self.get_argument("path", "") or str(Path.cwd())
        directory = Path(raw).expanduser()
        if directory.is_file():
            directory = directory.parent
        if not directory.is_dir():
            raise EditorError(f"Not a directory: {directory}")
        directory = directory.resolve()

        entries = []
        for child in directory.iterdir():
            if child.name.startswith("."):
                continue
            try:
                # Unreadable entries (foreign mounts, restricted dirs) must not
                # take down the whole listing.
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir or child.suffix.lower() in (".usd", ".usda", ".usdc", ".usdz"):
                entries.append({"name": child.name, "path": str(child), "dir": is_dir})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        self.write(json.dumps({
            "cwd": str(directory),
            "parent": str(directory.parent) if directory.parent != directory else None,
            "entries": entries,
        }))


class ChangeSocket(tornado.websocket.WebSocketHandler):
    """Pushes USD change notices to every connected browser.

    Keeps tabs in sync with each other and, more importantly, surfaces changes
    the client never asked for -- a long-running Python console script, or an
    edit made from a second window.
    """

    clients: set["ChangeSocket"] = set()

    def open(self, *args) -> None:
        ChangeSocket.clients.add(self)
        # Tell the newcomer where the stage currently stands so it can decide
        # whether its initial fetch is already stale.
        try:
            self.write_message(json.dumps(
                {"type": "hello", "revision": SESSION.stage_info()["revision"]}))
        except Exception:
            pass

    def on_close(self) -> None:
        ChangeSocket.clients.discard(self)

    def on_message(self, message) -> None:
        """Only used as a keepalive ping; all real traffic goes over HTTP."""

    @classmethod
    def broadcast(cls, payload: dict) -> None:
        message = json.dumps(payload)
        for client in list(cls.clients):
            try:
                client.write_message(message)
            except Exception:
                cls.clients.discard(client)


# The IOLoop is captured at startup so notice callbacks -- which fire on
# whichever thread performed the edit -- can hop back onto it safely.
MAIN_LOOP: tornado.ioloop.IOLoop | None = None
_flush_scheduled = False
DEBOUNCE_SECONDS = 0.05


def _flush_changes() -> None:
    global _flush_scheduled
    _flush_scheduled = False
    record = SESSION.drain_change()
    if record:
        ChangeSocket.broadcast({"type": "change", **record})


def _schedule_flush() -> None:
    """Coalesce a burst of notices into one broadcast."""
    global _flush_scheduled
    if _flush_scheduled:
        return
    _flush_scheduled = True
    tornado.ioloop.IOLoop.current().call_later(DEBOUNCE_SECONDS, _flush_changes)


def _on_stage_changed() -> None:
    # add_callback is the thread-safe entry point into the loop.
    if MAIN_LOOP is not None:
        MAIN_LOOP.add_callback(_schedule_flush)


class IndexHandler(tornado.web.RequestHandler):
    def get(self):
        self.set_header("Cache-Control", "no-store")
        self.render(str(STATIC / "index.html"))


def make_app() -> tornado.web.Application:
    return tornado.web.Application(
        [
            (r"/", IndexHandler),
            (r"/api/stage", StageHandler),
            (r"/api/geometry", GeometryHandler),
            (r"/api/prim", PrimHandler),
            (r"/api/composition", CompositionHandler),
            (r"/api/usda", UsdaHandler),
            (r"/api/python", PythonHandler),
            (r"/api/browse", BrowseHandler),
            (r"/ws", ChangeSocket),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": str(STATIC)}),
        ],
        template_path=str(STATIC),
        debug=False,
    )


async def main() -> None:
    global MAIN_LOOP
    parser = argparse.ArgumentParser(description="Browser-based USD scene editor.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default localhost -- see SECURITY note)")
    parser.add_argument("--open", dest="open_path", default=None,
                        help="USD file to load at startup")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not launch a browser window")
    args = parser.parse_args()

    MAIN_LOOP = tornado.ioloop.IOLoop.current()
    SESSION.on_change = _on_stage_changed

    if args.open_path:
        try:
            SESSION.open(args.open_path)
            print(f"Opened {SESSION.path}", flush=True)
        except EditorError as exc:
            print(f"Could not open {args.open_path}: {exc}", file=sys.stderr)

    if args.host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: binding to {args.host}. This server runs arbitrary "
              f"Python and reads/writes local files. Anyone who can reach this "
              f"port controls this machine.", file=sys.stderr)

    make_app().listen(args.port, address=args.host)
    url = f"http://{args.host}:{args.port}"
    # flush: stdout is block-buffered when redirected to a file or pipe, which
    # would otherwise swallow the banner for anyone running this under `>log`.
    print(f"USD scene editor: {url}   (Ctrl-C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
