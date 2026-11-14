"""Opaque identifiers: a prefix and a ULID. An identifier holds no storage location and no secret."""
import os
import time

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    n = (time.time_ns() // 1_000_000 << 80) | int.from_bytes(os.urandom(10), "big")
    return prefix + "_" + "".join(_B32[(n >> s) & 31] for s in range(125, -1, -5))
