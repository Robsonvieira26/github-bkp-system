import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 30 * 60
INCOMING = "refs/incoming/"
FETCH_REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
STAGING_REFSPECS = ("+refs/heads/*:refs/incoming/heads/*", "+refs/tags/*:refs/incoming/tags/*")
APPLY_REFSPECS = ("+refs/incoming/heads/*:refs/heads/*", "+refs/incoming/tags/*:refs/tags/*")


class GitError(Exception):
    pass


def mirror_path(repos_dir: Path, full_name: str) -> Path:
    owner, repo = full_name.split("/", 1)
    return repos_dir / owner / f"{repo}.git"


def _basic_auth(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _run(
    args: list[str],
    cwd: Path | None = None,
    token: str | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        # Passed via env so the token never lands in .git/config or the process list.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {_basic_auth(token)}"
    # Bytes in/out: text mode would translate LF to CRLF on stdin on Windows,
    # which breaks `update-ref --stdin`.
    # Callers only handle GitError, so every way git can fail must surface as one.
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            input=input.encode() if input is not None else None,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} excedeu o tempo limite") from None
    except FileNotFoundError:
        raise GitError("git não encontrado") from None
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode(errors="replace"),
        proc.stderr.decode(errors="replace"),
    )


def _git(args: list[str], cwd: Path | None = None, token: str | None = None,
         input: str | None = None) -> str:
    proc = _run(args, cwd=cwd, token=token, input=input)
    if proc.returncode != 0:
        # The message ends up in the DB and in emails: never let the token leak there.
        stderr = proc.stderr
        if token:
            stderr = stderr.replace(_basic_auth(token), "***").replace(token, "***")
        raise GitError(f"git {args[0]} falhou: {stderr.strip()[:500]}")
    return proc.stdout


def _list_refs(path: Path, *prefixes: str) -> dict[str, str]:
    out = _git(["for-each-ref", "--format=%(objectname) %(refname)", *prefixes], cwd=path)
    refs = {}
    for line in out.splitlines():
        sha, name = line.split(" ", 1)
        refs[name] = sha
    return refs


def _delete_refs(path: Path, prefix: str) -> None:
    refs = _list_refs(path, prefix)
    if refs:
        _git(["update-ref", "--stdin"], cwd=path,
             input="".join(f"delete {name}\n" for name in refs))


def _is_ancestor(path: Path, old: str, new: str) -> bool:
    proc = _run(["merge-base", "--is-ancestor", old, new], cwd=path)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError(f"git merge-base falhou: {proc.stderr.strip()[:500]}")


def _unique_ref(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _force_remove_readonly(func, path, exc) -> None:
    # git writes pack/object files read-only; on Windows rmtree can't delete them as-is.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def ensure_mirror(path: Path, url: str, token: str | None) -> bool:
    """Clone a bare mirror if missing. Returns True when a new clone was created."""
    if (path / "HEAD").exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Build the mirror in a sibling dir and move it into place only when fully configured.
    # A clone left half-configured (default +refs/*:refs/*, mirror=true) would look ready
    # by HEAD alone, and its next `fetch --prune` would delete refs/backup/*.
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp, onexc=_force_remove_readonly)
    _git(["clone", "--mirror", url, str(tmp)], token=token)
    _git(["config", "--unset-all", "remote.origin.fetch"], cwd=tmp)
    for spec in FETCH_REFSPECS:
        _git(["config", "--add", "remote.origin.fetch", spec], cwd=tmp)
    _git(["config", "remote.origin.mirror", "false"], cwd=tmp)
    _delete_refs(tmp, "refs/pull/")
    try:
        os.replace(tmp, path)
    except OSError as exc:
        raise GitError(f"não foi possível mover o mirror para {path}: {exc}") from exc
    return True


def _preserve_vanishing_refs(path: Path, today: str) -> list[str]:
    local = _list_refs(path, "refs/heads/", "refs/tags/")
    incoming = _list_refs(path, INCOMING)
    existing_backups = set(_list_refs(path, "refs/backup/"))
    preserved = []
    for name, sha in sorted(local.items()):
        short = name.removeprefix("refs/")
        new = incoming.get(INCOMING + short)
        if new == sha:
            continue
        if new is not None and name.startswith("refs/heads/") and _is_ancestor(path, sha, new):
            continue
        target = _unique_ref(f"refs/backup/{today}/{short}", existing_backups)
        _git(["update-ref", target, sha], cwd=path)
        existing_backups.add(target)
        preserved.append(name)
    return preserved


def sync_mirror(path: Path, token: str | None, today: str) -> list[str]:
    """Update the mirror, first copying refs that would vanish to refs/backup/<today>/.

    Returns the original names of the preserved refs.
    """
    # --no-tags: tag auto-following would write refs/tags/* before we compare.
    # --refmap= : otherwise git also updates refs/heads/* via the configured refspec
    # (opportunistic update), which would overwrite refs before we compare.
    _git(
        ["fetch", "--prune", "--no-tags", "--refmap=", "origin", *STAGING_REFSPECS],
        cwd=path,
        token=token,
    )
    try:
        preserved = _preserve_vanishing_refs(path, today)
        # Apply exactly what was staged and compared, from the repo itself: a second
        # network fetch could pick up a force-push that happened after the comparison
        # and overwrite a tip that was never backed up. --prune drops local heads/tags
        # missing from refs/incoming; refs/backup/* is outside these refspecs.
        _git(["fetch", "--prune", "--no-tags", ".", *APPLY_REFSPECS], cwd=path)
    finally:
        _delete_refs(path, INCOMING)
    return preserved


def repo_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
