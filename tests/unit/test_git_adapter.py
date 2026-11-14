"""The git adapter: a repository that is not a dataset, through the same handles, grants, and derived handles."""
import subprocess
import threading
import time

import pytest

from datahandles import Client, HandleError, Runtime
from datahandles.adapters.git import Adapter
from datahandles.runtime.adapter import Call


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("first\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "Add the README")
    (repo / "README.md").write_text("second\n")
    (repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(20000)))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Change the README and add a big file")
    (tmp_path / "outside.txt").write_text("secret\n")
    return repo


def test_a_repository_handle_reads_commits_files_and_diffs(tmp_path, repo):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    r = paul.register("repo", adapter="git", source={"path": str(repo)})
    head = git(repo, "rev-parse", "HEAD")
    assert r.info["type"] == "repository" and r.info["version"] == head
    log = r.invoke("repo.log")
    assert [c["subject"] for c in log["commits"]] == ["Change the README and add a big file", "Add the README"]
    assert r.invoke("repo.files") == {"files": ["README.md", "big.txt"], "total": 2, "truncated": False}
    assert r.invoke("repo.read", {"path": "README.md"})["text"] == "second\n"
    diff = r.invoke("repo.diff", {"path": "README.md"})
    assert "-first" in diff["diff"] and "+second" in diff["diff"] and not diff["truncated"]
    # An output that would pass the size limit stops and says so.
    big = r.invoke("repo.diff")
    assert big["truncated"] and big["bytes"] > 65536
    assert r.call("repo.diff")["metadata"]["bytes_transferred"] <= 65536


def test_a_handle_pinned_to_a_commit_does_not_follow_new_commits(tmp_path, repo):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    r = paul.register("repo", adapter="git", source={"path": str(repo)})
    old = r.invoke("repo.at", {"rev": "HEAD~1"})
    assert old.info["kind"] == "result" and old.describe()["consistency"] == "snapshot"
    (repo / "README.md").write_text("third\n")
    git(repo, "commit", "-q", "-am", "Third")
    assert old.invoke("repo.read", {"path": "README.md"})["text"] == "first\n"
    assert r.invoke("repo.read", {"path": "README.md"})["text"] == "third\n"
    assert old.observe()["version"] == git(repo, "rev-parse", "HEAD~2")


def test_a_grant_limits_what_a_second_principal_can_do_with_the_repository(tmp_path, repo):
    runtime = Runtime(tmp_path / "state")
    r = Client(runtime, "paul").register("repo", adapter="git", source={"path": str(repo)})
    r.grant("bob", ["repo.log"])
    bob = Client(runtime, "bob").resolve(r.id)
    assert bob.invoke("repo.log", {"n": 1})["commits"][0]["subject"].startswith("Change")
    with pytest.raises(HandleError) as e:
        bob.invoke("repo.read", {"path": "README.md"})
    assert e.value.code == "CAPABILITY_DENIED"


@pytest.mark.parametrize("bad", [{"path": "../outside.txt"}, {"path": "--output=/tmp/x"}])
def test_a_path_cannot_leave_the_repository_or_pass_as_an_option(tmp_path, repo, bad):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    r = paul.register("repo", adapter="git", source={"path": str(repo)})
    with pytest.raises(HandleError) as e:
        r.invoke("repo.read", bad)
    assert e.value.code == "INVALID_INPUT"
    with pytest.raises(HandleError) as e:
        r.invoke("repo.at", {"rev": "--output=/tmp/x"})
    assert e.value.code == "INVALID_INPUT"


def test_a_cancel_kills_the_git_process(repo, monkeypatch):
    adapter = Adapter()
    ref = adapter.open({"path": str(repo)})
    real = subprocess.Popen
    # A git call that would take 30 s: the process is `sleep 30` in place of git.
    monkeypatch.setattr("datahandles.adapters.git.subprocess.Popen", lambda args, **kw: real(["sleep", "30"], **kw))
    call = Call()
    threading.Timer(0.2, call.cancel, args=("cancel",)).start()
    t0 = time.perf_counter()
    with pytest.raises(HandleError) as e:
        adapter.invoke(ref, "repo.log", {}, eager=False, call=call)
    assert e.value.code == "JOB_CANCELLED" and time.perf_counter() - t0 < 5
