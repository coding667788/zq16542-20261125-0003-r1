"""Canonical JSON and digest helpers.

The implementation deliberately uses only the Python standard library.  All
digests are SHA-256 over UTF-8 canonical JSON so that receipts and replays are
stable across processes and operating systems.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_text(value: Any) -> str:
    """Return deterministic JSON text.

    Keys are sorted, insignificant whitespace is removed, and non-ASCII
    characters are emitted literally.  The result is still UTF-8 encoded by
    callers.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_text(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


def short_hash(digest: str, length: int = 8) -> str:
    return digest[:length]
