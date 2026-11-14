"""Opaque identifiers: a prefix and a ULID. An identifier holds no storage location and no secret."""
import os
import time

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    n = (time.time_ns() // 1_000_000 << 80) | int.from_bytes(os.urandom(10), "big")
    return prefix + "_" + "".join(_B32[(n >> s) & 31] for s in range(125, -1, -5))


def edit_distance(a: str, b: str) -> int:
    """The number of single-character insertions, deletions, and changes that turn `a` into `b`."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]
