from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_PATH = ROOT / "fixtures" / "synthetic.json"

from escrowclock.fixtures import load_fixture_file  # noqa: E402
from escrowclock.store import Store  # noqa: E402


class TempStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="escrowclock-test-")
        self.db_path = Path(self._temp_dir.name) / "escrowclock.db"
        self.store = Store(self.db_path)
        self.fixture_summary = load_fixture_file(self.store, FIXTURE_PATH)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()
        super().tearDown()


def fresh_fixture_store() -> tuple[tempfile.TemporaryDirectory, Path, Store]:
    temp_dir = tempfile.TemporaryDirectory(prefix="escrowclock-func-")
    db_path = Path(temp_dir.name) / "escrowclock.db"
    store = Store(db_path)
    load_fixture_file(store, FIXTURE_PATH)
    return temp_dir, db_path, store
