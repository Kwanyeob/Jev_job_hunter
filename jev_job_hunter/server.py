"""`jjh serve`: the invite-only web service friends use from a browser.

Starlette app + the scheduler thread. A user arrives through their invite link, gets a signed
session cookie, and from then on sees only their own folder (data/users/<id>/): profile, watch
state, cumulative Excel. Resume PDFs are read in memory and never written to disk.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from jev_job_hunter.profile import normalize_profile
from jev_job_hunter.questions import LINKEDIN_OPEN_THRESHOLD
from jev_job_hunter.scheduler import MAX_QUERIES, STATE_KEY, Hub, Scheduler
from jev_job_hunter.users import SESSION_DAYS, User, Users, make_session, read_session, secret_key
from jev_job_hunter.watch import WatchState, dismissed, set_dismissed, write_state_xlsx
from jev_job_hunter.web import _load_saved, _save_profile

COOKIE = "jjh_session"
APP_HTML = Path(__file__).with_name("web") / "app.html"
MAX_PDF = 20 * 1024 * 1024
LISTINGS_SHOWN = 300
PING_EVERY = 15  # seconds of silence before an SSE keep-alive comment


def lang(request: Request) -> str:
    """'ko' or 'en': the page's language switch (cookie), else the browser's language."""
    chosen = request.cookies.get("jjh_lang", "")
    if chosen in ("ko", "en"):
        return chosen
    return "ko" if request.headers.get("accept-language", "").lower().startswith("ko") else "en"


def L(request: Request, ko: str, en: str) -> str:
    return ko if lang(request) == "ko" else en


def _page(text: str) -> str:
    return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>Shortlist</title><body style='font:15px system-ui;max-width:520px;margin:15vh auto;padding:0 16px'>"
            f"<h1 style='font-size:20px'>Shortlist</h1><p>{text}</p></body>")


def _clean(prof: dict) -> dict:
    search = {**prof["search"], "queries": prof["search"]["queries"][:MAX_QUERIES]}
    return {"candidate": prof["candidate"], "search": search}


def create_app(data_dir: Path, *, scheduler: Scheduler | None = None, hub: Hub | None = None,
               extract=None, reader_ready=None, public_url: str = "", start_scheduler: bool = False) -> Starlette:
    from jev_job_hunter.resume import ResumeError, extract_profile, has_credentials

    users = Users(data_dir)
    key = secret_key(data_dir)
    hub = hub or Hub()
    scheduler = scheduler or Scheduler(users, hub)
    extract = extract or extract_profile
    reader_ready = reader_ready or has_credentials
    secure = public_url.startswith("https://")

    def user_of(request: Request) -> User:
        user = read_session(key, users, request.cookies.get(COOKIE, ""))
        if user is None:
            raise HTTPException(401, L(request, "로그인이 필요합니다. 받은 초대 링크로 다시 접속해 주세요.",
                                       "Please sign in again with your invite link."))
        return user

    def invite(request: Request):
        user = users.by_token(request.path_params["token"])
        if user is None:
            return HTMLResponse(_page(L(request, "이 초대 링크는 유효하지 않습니다. 보낸 사람에게 새 링크를 요청해 주세요.",
                                        "This invite link is not valid. Ask the person who sent it for a new one.")), 403)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, make_session(key, user), max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=secure, samesite="lax")
        return resp

    def home(request: Request):
        try:
            user_of(request)
        except HTTPException:
            return HTMLResponse(_page(L(request, "초대받은 사람만 쓸 수 있습니다. 받은 초대 링크로 접속해 주세요.",
                                        "This site is invite-only. Open it with the invite link you received.")), 401)
        return HTMLResponse(APP_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    def state(request: Request):
        user = user_of(request)
        return JSONResponse({
            "user": user.name, "profile": _load_saved(users.dir(user.id)), "watch": scheduler.status(user.id),
            "reader": reader_ready(), "jev": bool((os.environ.get("TYPESAFE_API_KEY") or "").strip()),
            "max_queries": MAX_QUERIES,
        })

    def results(request: Request):
        user = user_of(request)
        root = users.dir(user.id)
        st, gone = WatchState.load(root, STATE_KEY), dismissed(root)
        scored = {m["id"]: m for m in st.matches if m["id"] not in gone}
        rows = [{**c, **scored.get(c["id"], {})} for c in st.listings if c["id"] not in gone]
        plan = scheduler.eval_plan(user.id, st)
        for r in rows:  # jobs without a fit yet: when they get one, or why they won't
            if not r.get("href"):  # a few search cards carry no link; the posting's page is known by its id
                r["href"] = f"https://www.linkedin.com/jobs/view/{r['id']}"
            if not isinstance(r.get("fit"), (int, float)):
                op = r.get("open")
                r["eval"] = plan.get(r["id"]) or (None if not isinstance(op, (int, float))
                                                  else {"state": "skipped"} if op < LINKEDIN_OPEN_THRESHOLD
                                                  else {"state": "dropped"})
        rows.sort(key=lambda r: (r.get("first_seen") or "", r.get("fit") or -1, r.get("open") or 0), reverse=True)
        return JSONResponse({"rows": rows[:LISTINGS_SHOWN], "total": len(rows), "scored": len(scored),
                             "last_at": st.cycles[-1]["at"] if st.cycles else None})

    async def resume(request: Request):
        user = user_of(request)
        if int(request.headers.get("content-length") or 0) > MAX_PDF:
            return JSONResponse({"error": L(request, "파일이 너무 큽니다 (최대 20MB).", "The file is too large (20 MB max).")}, 413)
        if not reader_ready():
            return JSONResponse({"error": L(request, "지금은 이력서 자동 읽기를 쓸 수 없습니다. 프로필을 직접 입력해 주세요.",
                                            "Resume reading is not available right now. Please fill in the profile yourself.")}, 503)
        pdf = await request.body()
        try:
            raw = await run_in_threadpool(extract, pdf)
            prof = _clean(normalize_profile(raw, "(resume)"))
        except ResumeError as e:
            return JSONResponse({"error": L(request, f"이력서를 읽지 못했습니다: {e}", f"Could not read the resume: {e}")}, 400)
        except SystemExit as e:
            return JSONResponse({"error": str(e).removeprefix("ERROR: ")}, 400)
        _save_profile(users.dir(user.id), prof)
        return JSONResponse({"profile": prof})

    async def profile(request: Request):
        user = user_of(request)
        try:
            prof = _clean(normalize_profile(await request.json(), "(web)"))
        except json.JSONDecodeError:
            return JSONResponse({"error": L(request, "잘못된 요청입니다.", "Bad request.")}, 400)
        except SystemExit:
            return JSONResponse({"error": L(request, "희망 직무를 하나 이상 입력해 주세요.", "Enter at least one target role.")}, 400)
        _save_profile(users.dir(user.id), prof)
        return JSONResponse({"profile": prof})

    async def watch(request: Request):
        """{"enabled": bool} turns the watch on or off; {"every": "30-60" | ...} picks the search interval."""
        user = user_of(request)
        body = await request.json()
        if "every" in body:
            try:
                scheduler.set_every(user.id, str(body["every"]))
            except ValueError:
                return JSONResponse({"error": L(request, "잘못된 검색 간격입니다.", "Unknown search interval.")}, 400)
        if "enabled" in body:
            on = bool(body["enabled"])
            if on and _load_saved(users.dir(user.id)) is None:
                return JSONResponse({"error": L(request, "먼저 프로필을 저장해 주세요.", "Save your profile first.")}, 400)
            scheduler.set_enabled(user.id, on)
        return JSONResponse(scheduler.status(user.id))

    def run_now(request: Request):
        user = user_of(request)
        if _load_saved(users.dir(user.id)) is None:
            return JSONResponse({"error": L(request, "먼저 프로필을 저장해 주세요.", "Save your profile first.")}, 400)
        ready = scheduler.status(user.id)["run_now_at"]
        if ready:
            return JSONResponse({"error": L(request, f"방금 검색했어요. {ready[11:16]}부터 다시 찾을 수 있어요.",
                                            f"You just searched. You can search again at {ready[11:16]}.")}, 429)
        scheduler.run_now(user.id)
        return JSONResponse(scheduler.status(user.id))

    async def events(request: Request):
        user = user_of(request)
        hint = request.headers.get("last-event-id") or request.query_params.get("since") or "0"
        last = int(hint) if hint.isdigit() else 0

        async def stream():
            nonlocal last
            quiet = 0
            while not await request.is_disconnected():
                batch = hub.since(user.id, last)
                for e in batch:
                    last = e["id"]
                    yield f"id: {e['id']}\nevent: {e['kind']}\ndata: {json.dumps(e['data'], ensure_ascii=False)}\n\n"
                quiet = 0 if batch else quiet + 1
                if quiet >= PING_EVERY:
                    quiet = 0
                    yield ": ping\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def dismiss(request: Request):
        """Knock jobs out of the user's list (or bring them back with "undo"). They stay seen,
        so a later cycle never adds them again."""
        user = user_of(request)
        body = await request.json()
        ids = [str(i) for i in body.get("ids") or [] if str(i).strip()]
        if not ids:
            return JSONResponse({"error": L(request, "지울 공고가 없습니다.", "No jobs to remove.")}, 400)
        gone = set_dismissed(users.dir(user.id), ids, on=not body.get("undo"))
        return JSONResponse({"dismissed": len(gone)})

    def export(request: Request):
        user = user_of(request)
        root = users.dir(user.id)
        st = WatchState.load(root, STATE_KEY)
        if not st.listings:
            return JSONResponse({"error": L(request, "아직 결과가 없습니다.", "No results yet.")}, 404)
        search = (_load_saved(root) or {}).get("search") or {}
        meta = {"user": user.name, "exported": date.today().isoformat(),
                "queries": search.get("queries") or [], "location": search.get("location") or ""}
        scored_only = request.query_params.get("only") == "scored"  # Jev's matches, no All listings sheet
        path = write_state_xlsx(root, st, meta, scored_only=scored_only, listings_first=not scored_only)  # fresh, so knocked-out jobs are left out
        name = f"shortlist-{'scored-' if scored_only else ''}{date.today().isoformat()}.xlsx"
        return FileResponse(path, filename=name)

    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, exc.status_code)

    @asynccontextmanager
    async def lifespan(app):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = Starlette(routes=[
        Route("/", home), Route("/invite/{token}", invite),
        Route("/api/state", state), Route("/api/results", results),
        Route("/api/resume", resume, methods=["POST"]), Route("/api/profile", profile, methods=["POST"]),
        Route("/api/watch", watch, methods=["POST"]), Route("/api/run-now", run_now, methods=["POST"]),
        Route("/api/dismiss", dismiss, methods=["POST"]),
        Route("/api/events", events), Route("/api/export.xlsx", export),
    ], exception_handlers={HTTPException: http_error}, lifespan=lifespan)
    app.state.users, app.state.scheduler, app.state.hub = users, scheduler, hub
    return app


def serve(data_dir: Path, host: str = "127.0.0.1", port: int = 8000, public_url: str = "") -> None:
    import uvicorn

    from jev_job_hunter import linkedin
    linkedin.REQUEST_LOG = data_dir / "linkedin-requests.jsonl"  # every LinkedIn request, all users
    app = create_app(data_dir, public_url=public_url, start_scheduler=True)
    print(f"  Shortlist service  http://{host}:{port}/  data {data_dir}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*", log_level="warning")
