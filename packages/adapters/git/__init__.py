"""A git repository, read-only. The adapter runs the `git` program; it never changes the repository.

A repository handle follows the current HEAD (consistency `latest`). `repo.at` derives a handle
pinned to one commit (consistency `snapshot`). Git itself resolves each path inside a commit, so
no path can leave the repository. The adapter uses only commits and trees, never the work tree,
and turns off external diff programs and text conversion.
"""
import json
import subprocess
from pathlib import Path

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HandleError, capability
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

_REV = {"type": "string", "minLength": 1, "description": "A commit: a sha, a branch, a tag, or HEAD~2."}
_PATH = {"type": "string", "description": "A path in the repository. Empty: the whole repository."}
_CAPS = [
    capability("repo.log", "List commits, newest first. The output stops at the size limit and says so.",
               {"n": {"type": "integer", "minimum": 1, "default": 20}, "path": _PATH}),
    capability("repo.files", "List the file paths in the commit. The output stops at the size limit and says so.", {"path": _PATH}),
    capability("repo.read", "Read a byte range of one file as text.", {
        "path": _PATH,
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        # A read of more bytes than the output limit can never pass the limit.
        "length": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_OUTPUT_BYTES, "default": 4096},
    }, required=["path"]),
    capability("repo.diff", "Show the changes of one commit, or between two commits. The output stops at the size limit and says so.",
               {"from": _REV, "path": _PATH}),
    capability("repo.at", "Derive a handle pinned to one commit. No data goes to the caller.",
               {"rev": _REV}, required=["rev"], returns="handle"),
]
# Bytes kept for the JSON keys around a list or a text in a truncated output.
_ENVELOPE = 256


class Adapter(ResourceAdapter):
    name = "git"

    def __init__(self, state_dir=None):
        pass

    def lock_key(self, ref: ResourceRef):
        return ref.locator["path"]  # read-only: a repository does not touch another

    def _git(self, path: str, *args: str, call=None) -> bytes:
        proc = subprocess.Popen(["git", "--no-pager", "-C", path, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if call is not None:
            call.on_cancel(proc.kill)  # a cancel or a time limit stops the git process
        out, err = proc.communicate()
        if call is not None:
            call.check()
        if proc.returncode != 0:
            raise HandleError("INVALID_INPUT", f"git {args[0]}: {err.decode(errors='replace').strip()}")
        return out

    def _commit(self, path: str, rev: str, call=None) -> str:
        # After --end-of-options, git reads a rev that starts with "-" as a rev, never as an option.
        return self._git(path, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}", call=call).decode().strip()

    def _rev(self, ref: ResourceRef, call=None) -> str:
        return ref.locator["rev"] or self._commit(ref.locator["path"], "HEAD", call)

    def open(self, source: dict) -> ResourceRef:
        path = Path(source["path"]).resolve(strict=True)
        top = self._git(str(path), "rev-parse", "--show-toplevel").decode().strip()
        return ResourceRef("repository", {"path": top, "rev": None}, self._commit(top, "HEAD"))

    def describe(self, ref: ResourceRef) -> dict:
        pinned = ref.locator["rev"]
        return {"description": f"A git repository, pinned to commit {pinned}." if pinned else "A git repository at its current HEAD.",
                "schema": {"name": Path(ref.locator["path"]).name}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        return {"version": self._rev(ref)}

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        repo, rev = ref.locator["path"], self._rev(ref, call)
        path = input.get("path", "")
        scope = ["--", path] if path else []
        if capability == "repo.at":
            commit = self._commit(repo, input["rev"], call)
            return Derived(ResourceRef("repository", {"path": repo, "rev": commit}, commit), kind="result", consistency="snapshot")
        if capability == "repo.read":
            data = self._git(repo, "cat-file", "blob", f"{rev}:{path}", call=call)
            offset, length = input.get("offset", 0), input.get("length", 4096)
            part = data[offset:offset + length]
            return Value({"text": part.decode("utf-8", "replace"), "offset": offset, "bytes": len(part),
                          "size": len(data), "eof": offset + len(part) >= len(data)}, rev)
        budget = DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE
        if capability == "repo.diff":
            if "from" in input:
                text = self._git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", self._commit(repo, input["from"], call), rev, *scope, call=call)
            else:
                text = self._git(repo, "show", "--format=", "--no-ext-diff", "--no-textconv", "--no-color", rev, *scope, call=call)
            text = text.decode("utf-8", "replace")
            cut = text
            while len(json.dumps(cut).encode()) > budget:
                cut = cut[: len(cut) * 3 // 4]
            return Value({"diff": cut, "truncated": len(cut) < len(text), "bytes": len(text.encode())}, rev)
        if capability == "repo.log":
            raw = self._git(repo, "log", f"-n{input.get('n', 20)}", "--format=%H%x1f%an%x1f%aI%x1f%s", rev, *scope, call=call)
            items = [dict(zip(("commit", "author", "date", "subject"), line.split("\x1f"))) for line in raw.decode("utf-8", "replace").splitlines()]
            key = "commits"
        elif capability == "repo.files":
            raw = self._git(repo, "ls-tree", "-r", "--name-only", rev, *scope, call=call)
            items, key = raw.decode("utf-8", "replace").splitlines(), "files"
        else:
            raise HandleError("CAPABILITY_NOT_FOUND", capability)
        kept = []
        for item in items:
            budget -= len(json.dumps(item).encode()) + 2
            if budget < 0:
                break
            kept.append(item)
        return Value({key: kept, "total": len(items), "truncated": len(kept) < len(items)}, rev)
