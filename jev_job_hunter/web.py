"""`jjh web`: local site — drop a resume, watch LinkedIn cards stream in, download Excel.

Stdlib only (ThreadingHTTPServer + Server-Sent Events). Binds 127.0.0.1; one hunt at a time.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

from jev_job_hunter.profile import DEFAULT_PROFILE, normalize_profile
from jev_job_hunter.questions import LINKEDIN_MAX_CARDS, LINKEDIN_MAX_OPEN

INDEX = Path(__file__).with_name("web") / "index.html"
MAX_PDF = 20 * 1024 * 1024


class Hunt:
    """Everything the page needs, rebuilt from run_linkedin's events."""

    def __init__(self):
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.cards: dict[str, dict] = {}
        self.running = False
        self.stop = threading.Event()
        self.meta: dict = {}
        self.xlsx = ""

    def reset(self, meta: dict) -> None:
        with self.cond:
            self.events, self.cards, self.xlsx = [], {}, ""
            self.meta, self.running = meta, True
            self.stop.clear()

    def on_event(self, kind: str, data: dict) -> None:
        with self.cond:
            if kind == "card":
                self.cards[data["id"]] = dict(data)
            elif kind == "triage":
                for cid, v in data["scores"].items():
                    if cid in self.cards:
                        self.cards[cid]["open"] = v
                for cid in data["picked"]:
                    if cid in self.cards:
                        self.cards[cid]["opened"] = True
            elif kind == "fit" and data["id"] in self.cards:
                self.cards[data["id"]].update(data)
            elif kind == "done":
                self.running = False
                self.xlsx = data.get("xlsx") or ""
            self.events.append({"kind": kind, "data": data, "ts": time.time()})
            self.cond.notify_all()

    def snapshot(self) -> tuple[list[dict], list[dict]]:
        with self.cond:
            cards = [dict(c) for c in self.cards.values()]
        return [c for c in cards if "fit" in c], cards


def _load_saved(root: Path) -> dict | None:
    p = root / DEFAULT_PROFILE
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        prof = normalize_profile(data, str(p))
        return {"candidate": prof["candidate"], "search": prof["search"]}
    except (SystemExit, yaml.YAMLError):
        return None


def _save_profile(root: Path, prof: dict) -> None:
    p = root / DEFAULT_PROFILE
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"candidate": prof["candidate"], "search": prof["search"]}
    p.write_text("# Written by jjh web. No contact info: everything under `candidate` is sent to Jev.\n"
                 + yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")


def make_handler(root: Path, hunt: Hunt, runner=None):
    from jev_job_hunter.linkedin import hunt_meta, run_linkedin, save_results
    from jev_job_hunter.resume import ResumeError, extract_profile, has_credentials

    runner = runner or run_linkedin

    class Handler(BaseHTTPRequestHandler):
        server_version = "jjh-web"

        def log_message(self, fmt, *args):  # keep the terminal for hunt output
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self, limit: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("request too large")
            return self.rfile.read(n)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                body = INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/state":
                matches, cards = hunt.snapshot()
                self._json({
                    "claude": has_credentials(),
                    "jev": bool((os.environ.get("TYPESAFE_API_KEY") or "").strip()),
                    "running": hunt.running, "profile": _load_saved(root),
                    "cards": cards, "xlsx": hunt.xlsx,
                    "defaults": {"max_open": LINKEDIN_MAX_OPEN, "max_cards": LINKEDIN_MAX_CARDS},
                })
            elif url.path == "/api/events":
                since = int((parse_qs(url.query).get("since") or ["0"])[0])
                last = (self.headers.get("Last-Event-ID") or "").strip()  # EventSource auto-reconnect
                self._events(max(since, int(last)) if last.isdigit() else since)
            elif url.path == "/api/export.xlsx":
                self._export()
            else:
                self.send_error(404)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                if path == "/api/resume":
                    self._resume()
                elif path == "/api/hunt":
                    self._hunt()
                elif path == "/api/stop":
                    hunt.stop.set()
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _resume(self):
            pdf = self._body(MAX_PDF)
            if not has_credentials():
                self._json({"error": "ANTHROPIC_API_KEY is not set in .env — fill in the profile by hand, "
                                     "or add the key and restart jjh web."}, 400)
                return
            try:
                raw = extract_profile(pdf)
                prof = normalize_profile(raw, "(resume)")
            except ResumeError as e:
                self._json({"error": str(e)}, 400)
                return
            except SystemExit as e:
                self._json({"error": str(e).removeprefix("ERROR: ")}, 400)
                return
            _save_profile(root, prof)
            self._json({"profile": {"candidate": prof["candidate"], "search": prof["search"]}})

        def _hunt(self):
            req = json.loads(self._body(1024 * 1024) or b"{}")
            if hunt.running:
                self._json({"error": "a hunt is already running"}, 409)
                return
            try:
                prof = normalize_profile(req.get("profile") or {}, "(web)")
            except SystemExit as e:
                self._json({"error": str(e).removeprefix("ERROR: ")}, 400)
                return
            _save_profile(root, prof)
            mock = bool(req.get("mock")) or not (os.environ.get("TYPESAFE_API_KEY") or "").strip()
            s = prof["search"]
            hunt.reset(hunt_meta(prof, s["queries"], s["location"], s["posted"], s["remote"], mock))

            def work():
                try:
                    runner(root, profile={"candidate": prof["candidate"], "search": s},
                           max_cards=int(req.get("max_cards") or LINKEDIN_MAX_CARDS),
                           max_open=int(req.get("max_open") or LINKEDIN_MAX_OPEN),
                           mock=mock, on_event=hunt.on_event, stop=hunt.stop)
                except BaseException as e:  # surface anything to the page, never kill the server
                    hunt.on_event("error", {"msg": f"{type(e).__name__}: {e}"})
                finally:
                    if hunt.running:
                        hunt.on_event("done", {"xlsx": "", "stopped": "hunt ended unexpectedly"})

            threading.Thread(target=work, daemon=True).start()
            self._json({"ok": True, "mock": mock})

        def _events(self, since: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            i = since
            try:
                while True:
                    with hunt.cond:
                        if i >= len(hunt.events):
                            hunt.cond.wait(timeout=15)
                        batch = hunt.events[i:]
                    if not batch:
                        self.wfile.write(b": ping\n\n")
                    for ev in batch:
                        i += 1
                        self.wfile.write(f"id: {i}\nevent: {ev['kind']}\ndata: "
                                         f"{json.dumps(ev['data'], ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _export(self):
            matches, cards = hunt.snapshot()
            if not cards:
                self.send_error(404, "no results yet")
                return
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = save_results(root / "results", matches, cards, hunt.meta, stamp=f"web-{stamp}")
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> None:
    hunt = Hunt()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(root, hunt))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"  JevScout web  {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        hunt.stop.set()
    finally:
        httpd.server_close()
