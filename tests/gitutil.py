import subprocess
from pathlib import Path

_IDENTITY = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "init.defaultBranch=main",
]


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout


def rev(repo: Path, ref: str) -> str:
    return git("rev-parse", ref, cwd=repo).strip()


def has_ref(repo: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], cwd=repo, capture_output=True
    )
    return proc.returncode == 0


class RemoteRepo:
    """A bare repo at <base>/<owner>/<repo>.git standing in for GitHub, plus a work clone."""

    def __init__(self, base: Path, full_name: str = "owner/repo"):
        self.base = base
        self.full_name = full_name
        self.bare = base / f"{full_name}.git"
        self.bare.parent.mkdir(parents=True)
        git("init", "--bare", str(self.bare), cwd=base)
        self.work = base / "work"
        git("clone", str(self.bare), str(self.work), cwd=base)
        self.commit("initial")
        self.push("main")

    @property
    def base_url(self) -> str:
        return self.base.as_uri()

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.full_name}.git"

    def commit(self, message: str) -> str:
        (self.work / "file.txt").write_text(message)
        git("add", "-A", cwd=self.work)
        git("commit", "-m", message, cwd=self.work)
        return rev(self.work, "HEAD")

    def push(self, *args: str) -> None:
        git("push", "origin", *args, cwd=self.work)
