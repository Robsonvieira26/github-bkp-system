from pathlib import Path
from typing import Annotated, Protocol

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import Database, DuplicateRepoError
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
}


class SchedulerLike(Protocol):
    def is_running(self) -> bool: ...

    def trigger_now(self) -> bool: ...


def _redirect(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/?msg={msg}", status_code=303)


def create_app(db: Database, scheduler: SchedulerLike) -> FastAPI:
    app = FastAPI(title="github-bkp", docs_url=None, redoc_url=None, openapi_url=None)
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
    def add_repo(full_name: Annotated[str, Form()]):
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
        db.disable_repo(repo_id)
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
        last = db.last_run()
        return {"ok": True, "last_run": last.finished_at if last else None}

    return app
