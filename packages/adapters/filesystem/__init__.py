"""A read-only directory tree. Every path stays inside the root of the handle, symlinks included."""
import json
import os
from pathlib import Path

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HandleError, capability
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

_REL = {"type": "string", "description": "A path relative to the root of this handle."}
_CAPS = [
    capability("filesystem.stat", "Return the type, size, and version of one path.", {"path": _REL}),
    capability("filesystem.list", "List one directory. The output stops at the size limit and says so.", {"path": _REL}),
    capability("filesystem.read", "Read a byte range of one file as text.", {
        "path": _REL,
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        # A read of more bytes than the output limit can never pass the limit.
        "length": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_OUTPUT_BYTES, "default": 4096},
    }, required=["path"]),
    capability("filesystem.search", "Find lines that contain a text. The output stops at the size limit and says so.", {
        "text": {"type": "string", "minLength": 1},
        "path": _REL,
    }, required=["text"]),
    capability("filesystem.subtree", "Derive a handle to a sub-directory. No data goes to the caller.",
               {"path": _REL}, required=["path"], returns="handle"),
]
# Bytes kept for the JSON keys around the list in a truncated output ({"entries": [], "truncated": true, ...}).
_ENVELOPE = 256


def _version(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns}:{st.st_size}"


class Adapter(ResourceAdapter):
    name = "filesystem"

    def __init__(self, state_dir=None):
        pass

    def open(self, source: dict) -> ResourceRef:
        root = Path(source["path"]).resolve(strict=True)
        if not root.is_dir():
            raise HandleError("INVALID_INPUT", f"{root} is not a directory")
        return ResourceRef("filesystem", {"root": str(root)}, _version(root.stat()))

    def _path(self, ref: ResourceRef, rel: str = "") -> Path:
        root = Path(ref.locator["root"])
        path = (root / rel).resolve()
        if path != root and root not in path.parents:
            raise HandleError("CAPABILITY_DENIED", f"path {rel!r} is outside the handle")
        if not path.exists():
            raise HandleError("INVALID_INPUT", f"no such path: {rel!r}")
        return path

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "A read-only directory tree.", "schema": {"root_name": Path(ref.locator["root"]).name}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _CAPS

    def observe(self, ref: ResourceRef) -> dict:
        # The version of a directory changes when an entry is added or removed, not when a file in it is edited.
        return {"version": _version(self._path(ref).stat())}

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool) -> Value | Derived:
        root, path = Path(ref.locator["root"]), self._path(ref, input.get("path", ""))
        st = path.stat()
        if capability == "filesystem.stat":
            return Value({"type": "directory" if path.is_dir() else "file", "size": st.st_size, "version": _version(st)}, _version(st))
        if capability == "filesystem.subtree":
            if not path.is_dir():
                raise HandleError("INVALID_INPUT", "filesystem.subtree needs a directory")
            return Derived(ResourceRef("filesystem", {"root": str(path)}))
        if capability == "filesystem.read":
            if not path.is_file():
                raise HandleError("INVALID_INPUT", "filesystem.read needs a file")
            offset, length = input.get("offset", 0), input.get("length", 4096)
            with path.open("rb") as f:
                f.seek(offset)
                data = f.read(length)
            return Value({"text": data.decode("utf-8", "replace"), "offset": offset, "bytes": len(data),
                          "size": st.st_size, "eof": offset + len(data) >= st.st_size}, _version(st))
        budget = DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE
        if capability == "filesystem.list":
            if not path.is_dir():
                raise HandleError("INVALID_INPUT", "filesystem.list needs a directory")
            entries, total = [], 0
            for entry in sorted(os.scandir(path), key=lambda e: e.name):
                item = {"name": entry.name, "type": "directory" if entry.is_dir() else "file"}
                budget -= len(json.dumps(item).encode()) + 2
                total += 1
                if budget >= 0:
                    entries.append(item)
            return Value({"entries": entries, "total": total, "truncated": len(entries) < total}, _version(st))
        if capability == "filesystem.search":
            matches, truncated = [], False
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for file in files:
                if root not in file.resolve().parents:
                    continue  # a symlink that leaves the handle
                with file.open("r", encoding="utf-8", errors="replace") as f:
                    for number, line in enumerate(f, 1):
                        if input["text"] in line:
                            item = {"path": str(file.relative_to(root)), "line": number, "text": line.strip()[:200]}
                            budget -= len(json.dumps(item).encode()) + 2
                            if budget < 0:
                                truncated = True
                                break
                            matches.append(item)
                if truncated:
                    break
            return Value({"matches": matches, "truncated": truncated}, _version(st))
        raise HandleError("CAPABILITY_NOT_FOUND", capability)
