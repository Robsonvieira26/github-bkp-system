from pathlib import Path
from typing import Annotated, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.db import Database, DuplicateRepoError, utc_now
from app.names import parse_repo_name
from app.notifier import format_size

TEMPLATES_DIR = Path(__file__).parent / "templates"

MESSAGES = {
    "added": "Repositório adicionado.",
    "removed": "Repositório removido (cópia local mantida).",
    "sync_started": "Sincronização iniciada.",
    "sync_busy": "Sincronização já em execução.",
    "invalid": "Nome inválido. Use owner/repo ou a URL do GitHub.",
    "duplicate": "Repositório já está na lista.",
    "not_found": "Repositório não encontrado.",
}


class SchedulerLike(Protocol):
    def is_running(self) -> bool: ...

    def trigger_now(self) -> bool: ...


def _redirect(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/?msg={msg}", status_code=303)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(
    db: Database, scheduler: SchedulerLike, allowed_hosts: list[str] | None = None
) -> FastAPI:
    app = FastAPI(title="github-bkp", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def block_cross_site(request: Request, call_next):
        if request.method not in SAFE_METHODS:
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            cross_site = site is not None and site not in ("same-origin", "none")
            if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
                cross_site = True
            if cross_site:
                return PlainTextResponse("cross-site request blocked", status_code=403)
        return await call_next(request)

    if allowed_hosts is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.filters["size"] = format_size

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, msg: str | None = None):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "repos": db.list_repos(),
                "message": MESSAGES.get(msg or ""),
                "running": scheduler.is_running(),
            },
        )

    @app.post("/repos")
    def add_repo(full_name: Annotated[str, Form()] = ""):
        try:
            name = parse_repo_name(full_name)
        except ValueError:
            return _redirect("invalid")
        try:
            db.add_repo(name)
        except DuplicateRepoError:
            return _redirect("duplicate")
        return _redirect("added")

    @app.post("/repos/{repo_id}/delete")
    def delete_repo(repo_id: int):
        if not db.disable_repo(repo_id):
            return _redirect("not_found")
        db.add_event(repo_id, None, "removed", None, utc_now())
        return _redirect("removed")

    @app.post("/sync")
    def sync_now():
        return _redirect("sync_started" if scheduler.trigger_now() else "sync_busy")

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        recent = db.recent_runs(50)
        events = {run.id: db.events_for_run(run.id) for run in recent}
        return templates.TemplateResponse(
            request, "runs.html", {"runs": recent, "events": events}
        )

    @app.get("/health")
    def health():
        last = db.last_finished_run()
        return {"ok": True, "last_run": last.finished_at if last else None}

    return app
