"""Synthetic fixture loading.

Fixture files are trusted project data authored for tests/demos.  Every ID is
synthetic; there are no real people, credentials, or key material.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import FixtureError
from .store import Store


def load_fixture_dict(store: Store, fixture: dict[str, Any]) -> dict[str, Any]:
    commands = fixture.get("commands")
    if not isinstance(commands, list):
        raise FixtureError("fixture must contain a commands list")

    results = []
    applied = 0
    replayed = 0
    expected_errors = 0

    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            raise FixtureError(f"fixture command {index} must be an object")
        expected_error = item.pop("expect_error", None)
        command = dict(item)
        response = store.dispatch(command)
        results.append(response)

        if expected_error is not None:
            if response.get("ok"):
                raise FixtureError(
                    "fixture command was expected to fail but succeeded",
                    {"index": index, "expected_error": expected_error, "command": command},
                )
            actual = response["error"]["code"]
            if actual != expected_error:
                raise FixtureError(
                    "fixture error code mismatch",
                    {"index": index, "expected": expected_error, "actual": actual},
                )
            expected_errors += 1
        elif not response.get("ok"):
            raise FixtureError(
                "fixture command failed",
                {"index": index, "command": command, "response": response},
            )
        elif response.get("replayed"):
            replayed += 1
        else:
            applied += 1

    return {
        "ok": True,
        "fixture_id": fixture.get("fixture_id", "synthetic"),
        "commands": len(commands),
        "applied": applied,
        "replayed": replayed,
        "expected_errors_verified": expected_errors,
        "results": results,
    }


def load_fixture_file(store: Store, path: str | Path) -> dict[str, Any]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        fixture = json.load(fh)
    summary = load_fixture_dict(store, fixture)
    summary["fixture_path"] = str(p)
    return summary
