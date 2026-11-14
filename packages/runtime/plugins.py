"""Find an adapter class by its name.

A built-in adapter is the module datahandles.adapters.NAME. An adapter from a separate installed
package declares an entry point in the group `datahandles.adapters`, for example in its
pyproject.toml:

    [project.entry-points."datahandles.adapters"]
    git = "my_package.git_adapter:Adapter"

A built-in name wins over an entry point with the same name.
"""
import importlib
import importlib.util
from importlib.metadata import entry_points

from ..core import HandleError

GROUP = "datahandles.adapters"


def adapter_class(kind: str):
    """Return the adapter class for `kind`, or raise INVALID_INPUT."""
    if not kind.isidentifier():
        raise HandleError("INVALID_INPUT", f"no adapter {kind!r}")
    if importlib.util.find_spec(f"datahandles.adapters.{kind}") is not None:
        return importlib.import_module(f"datahandles.adapters.{kind}").Adapter
    found = entry_points(group=GROUP, name=kind)
    if not found:
        raise HandleError("INVALID_INPUT", f"no adapter {kind!r}: not built in, and no installed package declares it in the entry point group {GROUP}")
    return next(iter(found)).load()
