import base64
import subprocess
from pathlib import Path

import pytest

from app import mirror
from app.mirror import GitError, ensure_mirror, mirror_path, repo_size, sync_mirror
from tests.gitutil import git, has_ref, rev

TODAY = "2026-10-23"


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirrors" / "owner" / "repo.git"


def test_mirror_path():
    assert mirror_path(Path("/data/repos"), "owner/repo") == Path("/data/repos/owner/repo.git")


def test_ensure_mirror_clones_with_restricted_refspec(remote, mirror_dir):
    assert ensure_mirror(mirror_dir, remote.url, None) is True
    assert rev(mirror_dir, "refs/heads/main") == rev(remote.work, "HEAD")
    fetch = git("config", "--get-all", "remote.origin.fetch", cwd=mirror_dir).split()
    assert fetch == ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert ensure_mirror(mirror_dir, remote.url, None) is False


def test_ensure_mirror_bad_url_raises(tmp_path, mirror_dir):
    with pytest.raises(GitError):
        ensure_mirror(mirror_dir, (tmp_path / "nope.git").as_uri(), None)


def test_token_not_written_to_disk(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, "super-secret-token")
    sync_mirror(mirror_dir, "super-secret-token", TODAY)
    assert "super-secret-token" not in (mirror_dir / "config").read_text()


def test_sync_fast_forward_preserves_nothing(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    new = remote.commit("second")
    remote.push("main")
    assert sync_mirror(mirror_dir, None, TODAY) == []
    assert rev(mirror_dir, "refs/heads/main") == new
    assert not has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/main")


def test_sync_fetches_new_branch_and_tag(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    git("tag", "v1", cwd=remote.work)
    remote.push("v1")
    git("branch", "dev", cwd=remote.work)
    remote.push("dev")
    sync_mirror(mirror_dir, None, TODAY)
    assert has_ref(mirror_dir, "refs/tags/v1")
    assert has_ref(mirror_dir, "refs/heads/dev")


def test_sync_preserves_deleted_branch(remote, mirror_dir):
    git("checkout", "-b", "feature", cwd=remote.work)
    feat = remote.commit("feature work")
    remote.push("feature")
    git("checkout", "main", cwd=remote.work)
    ensure_mirror(mirror_dir, remote.url, None)

    remote.push("--delete", "feature")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/heads/feature"]
    assert not has_ref(mirror_dir, "refs/heads/feature")
    assert rev(mirror_dir, f"refs/backup/{TODAY}/heads/feature") == feat


def test_sync_preserves_force_pushed_branch(remote, mirror_dir):
    lost = remote.commit("will be lost")
    remote.push("main")
    ensure_mirror(mirror_dir, remote.url, None)

    git("reset", "--hard", "HEAD~1", cwd=remote.work)
    rewritten = remote.commit("rewritten")
    remote.push("--force", "main")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/heads/main"]
    assert rev(mirror_dir, "refs/heads/main") == rewritten
    assert rev(mirror_dir, f"refs/backup/{TODAY}/heads/main") == lost


def test_sync_preserves_moved_tag(remote, mirror_dir):
    git("tag", "v1", cwd=remote.work)
    remote.push("v1")
    old = rev(remote.work, "v1")
    ensure_mirror(mirror_dir, remote.url, None)

    remote.commit("after tag")
    git("tag", "-f", "v1", cwd=remote.work)
    remote.push("--force", "v1")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/tags/v1"]
    assert rev(mirror_dir, f"refs/backup/{TODAY}/tags/v1") == old


def test_backup_refs_survive_later_syncs_and_get_suffix(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    for i in range(2):
        git("branch", "tmp", cwd=remote.work)
        remote.push("tmp")
        sync_mirror(mirror_dir, None, TODAY)
        remote.push("--delete", "tmp")
        git("branch", "-D", "tmp", cwd=remote.work)
        remote.commit(f"move main {i}")
        remote.push("main")
        sync_mirror(mirror_dir, None, TODAY)

    sync_mirror(mirror_dir, None, TODAY)
    assert has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/tmp")
    assert has_ref(mirror_dir, f"refs/backup/{TODAY}/heads/tmp-2")


def test_incoming_refs_cleaned(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    sync_mirror(mirror_dir, None, TODAY)
    refs = git("for-each-ref", "--format=%(refname)", cwd=mirror_dir).split()
    assert not [r for r in refs if r.startswith("refs/incoming/")]


def test_repo_size(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    assert repo_size(mirror_dir) > 0


def _fake_run(result=None, exc=None):
    def fake(args, **kwargs):
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args, *result)

    return fake


def test_git_timeout_becomes_git_error(monkeypatch, tmp_path):
    exc = subprocess.TimeoutExpired(["git", "fetch"], 1)
    monkeypatch.setattr(mirror.subprocess, "run", _fake_run(exc=exc))
    with pytest.raises(GitError, match="git fetch excedeu o tempo limite"):
        mirror._git(["fetch"], cwd=tmp_path)


def test_git_missing_becomes_git_error(monkeypatch, tmp_path):
    monkeypatch.setattr(mirror.subprocess, "run", _fake_run(exc=FileNotFoundError("git")))
    with pytest.raises(GitError, match="git não encontrado"):
        mirror._git(["fetch"], cwd=tmp_path)


def test_git_error_redacts_token(monkeypatch, tmp_path):
    token = "ghp_secret123"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    stderr = f"fatal: bad header Authorization: Basic {basic} for {token}".encode()
    monkeypatch.setattr(mirror.subprocess, "run", _fake_run(result=(128, b"", stderr)))
    with pytest.raises(GitError) as info:
        mirror._git(["fetch"], cwd=tmp_path, token=token)
    message = str(info.value)
    assert token not in message
    assert basic not in message
    assert "***" in message


def test_ensure_mirror_is_atomic_when_config_fails(remote, mirror_dir, monkeypatch):
    real_git = mirror._git

    def failing_git(args, **kwargs):
        if args[0] == "config":
            raise GitError("git config falhou: boom")
        return real_git(args, **kwargs)

    monkeypatch.setattr(mirror, "_git", failing_git)
    with pytest.raises(GitError):
        ensure_mirror(mirror_dir, remote.url, None)
    assert not mirror_dir.exists()

    monkeypatch.setattr(mirror, "_git", real_git)
    assert ensure_mirror(mirror_dir, remote.url, None) is True
    fetch = git("config", "--get-all", "remote.origin.fetch", cwd=mirror_dir).split()
    assert fetch == ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert git("config", "remote.origin.mirror", cwd=mirror_dir).strip() == "false"
    assert not mirror_dir.with_name(mirror_dir.name + ".tmp").exists()


def test_ensure_mirror_removes_stale_temp_dir(remote, mirror_dir):
    stale = mirror_dir.with_name(mirror_dir.name + ".tmp")
    (stale / "objects").mkdir(parents=True)
    junk = stale / "objects" / "junk"
    junk.write_text("x")
    junk.chmod(0o444)  # git writes objects read-only; rmtree must cope on Windows
    assert ensure_mirror(mirror_dir, remote.url, None) is True
    assert not stale.exists()
    assert rev(mirror_dir, "refs/heads/main") == rev(remote.work, "HEAD")


def test_sync_preserves_deleted_tag(remote, mirror_dir):
    git("tag", "v1", cwd=remote.work)
    remote.push("v1")
    old = rev(remote.work, "v1")
    ensure_mirror(mirror_dir, remote.url, None)

    remote.push("--delete", "v1")
    preserved = sync_mirror(mirror_dir, None, TODAY)

    assert preserved == ["refs/tags/v1"]
    assert not has_ref(mirror_dir, "refs/tags/v1")
    assert rev(mirror_dir, f"refs/backup/{TODAY}/tags/v1") == old


def test_sync_applies_staged_refs_not_a_second_fetch(remote, mirror_dir, monkeypatch):
    ensure_mirror(mirror_dir, remote.url, None)
    staged = remote.commit("staged")
    remote.push("main")

    real_preserve = mirror._preserve_vanishing_refs

    def preserve_then_force_push(path, today):
        result = real_preserve(path, today)
        # A force-push landing between staging and applying must not reach refs/heads
        # uncompared: the staged tip would be lost without a backup.
        git("reset", "--hard", "HEAD~1", cwd=remote.work)
        remote.commit("raced")
        remote.push("--force", "main")
        return result

    monkeypatch.setattr(mirror, "_preserve_vanishing_refs", preserve_then_force_push)
    assert sync_mirror(mirror_dir, None, TODAY) == []
    assert rev(mirror_dir, "refs/heads/main") == staged


def _backup_refs(path: Path) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/backup/", cwd=path)
    return dict(line.split(" ", 1) for line in out.splitlines())


def test_backup_names_avoid_directory_file_conflicts(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    foo = remote.commit("foo")
    remote.push("HEAD:refs/heads/foo")
    sync_mirror(mirror_dir, None, TODAY)
    remote.push("--delete", "foo")
    assert sync_mirror(mirror_dir, None, TODAY) == ["refs/heads/foo"]

    # refs/backup/<today>/heads/foo now exists, so heads/foo/bar can't go under it.
    bar = remote.commit("foo/bar")
    remote.push("HEAD:refs/heads/foo/bar")
    sync_mirror(mirror_dir, None, TODAY)
    remote.push("--delete", "foo/bar")
    assert sync_mirror(mirror_dir, None, TODAY) == ["refs/heads/foo/bar"]

    backups = _backup_refs(mirror_dir)
    assert backups[f"refs/backup/{TODAY}/heads/foo"] == foo
    assert backups[f"refs/backup/{TODAY}/heads/foo-2/bar"] == bar


def test_backup_names_avoid_file_directory_conflicts(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    bar = remote.commit("foo/bar")
    remote.push("HEAD:refs/heads/foo/bar")
    sync_mirror(mirror_dir, None, TODAY)
    remote.push("--delete", "foo/bar")
    sync_mirror(mirror_dir, None, TODAY)

    foo = remote.commit("foo")
    remote.push("HEAD:refs/heads/foo")
    sync_mirror(mirror_dir, None, TODAY)
    remote.push("--delete", "foo")
    assert sync_mirror(mirror_dir, None, TODAY) == ["refs/heads/foo"]

    backups = _backup_refs(mirror_dir)
    assert backups[f"refs/backup/{TODAY}/heads/foo/bar"] == bar
    assert backups[f"refs/backup/{TODAY}/heads/foo-2"] == foo


def test_retry_after_failed_apply_does_not_duplicate_backup(remote, mirror_dir, monkeypatch):
    lost = remote.commit("will be lost")
    remote.push("main")
    ensure_mirror(mirror_dir, remote.url, None)
    git("reset", "--hard", "HEAD~1", cwd=remote.work)
    remote.commit("rewritten")
    remote.push("--force", "main")

    real_git = mirror._git
    calls = {"apply": 0}

    def flaky_git(args, **kwargs):
        if args[0] == "fetch" and "." in args:
            calls["apply"] += 1
            if calls["apply"] == 1:
                raise GitError("git fetch falhou: boom")
        return real_git(args, **kwargs)

    monkeypatch.setattr(mirror, "_git", flaky_git)
    with pytest.raises(GitError):
        sync_mirror(mirror_dir, None, TODAY)
    # The failed run never got to report the force-push, so the retry still does.
    assert sync_mirror(mirror_dir, None, TODAY) == ["refs/heads/main"]
    assert _backup_refs(mirror_dir) == {f"refs/backup/{TODAY}/heads/main": lost}


def test_same_sha_vanishing_twice_keeps_one_backup(remote, mirror_dir):
    ensure_mirror(mirror_dir, remote.url, None)
    sha = remote.commit("tmp")
    for _ in range(2):
        remote.push("HEAD:refs/heads/tmp")
        sync_mirror(mirror_dir, None, TODAY)
        remote.push("--delete", "tmp")
        sync_mirror(mirror_dir, None, TODAY)
    assert _backup_refs(mirror_dir) == {f"refs/backup/{TODAY}/heads/tmp": sha}


def test_ensure_mirror_unwritable_parent_is_git_error(remote, mirror_dir, monkeypatch):
    def deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(GitError, match="não foi possível criar .*Permission denied"):
        ensure_mirror(mirror_dir, remote.url, None)
