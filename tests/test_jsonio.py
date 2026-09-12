"""Atomic JSON I/O and mtime-cached config files."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from lib.dai.jsonio import AtomicJsonStore, CachedJsonFile, atomic_write_json, load_json


def bump_mtime(path: Path, seconds: int = 10) -> None:
    """Force a distinct mtime (some filesystems have 1 s granularity)."""
    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


class TestAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-jsonio-"))

    def test_creates_parents_and_default_mode(self) -> None:
        path = self.tmp / "deep" / "nested" / "file.json"
        atomic_write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_no_temp_files_left_behind(self) -> None:
        path = self.tmp / "file.json"
        atomic_write_json(path, {"a": 1})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["file.json"])

    def test_overwrite_is_atomic_from_a_reader_view(self) -> None:
        path = self.tmp / "file.json"
        atomic_write_json(path, {"v": 0})
        inode_before = path.stat().st_ino
        atomic_write_json(path, {"v": 1})
        self.assertEqual(json.loads(path.read_text())["v"], 1)
        self.assertNotEqual(path.stat().st_ino, inode_before)  # replaced, not truncated

    def test_custom_mode(self) -> None:
        path = self.tmp / "shared.json"
        atomic_write_json(path, {"a": 1}, mode=0o644)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)


class TestLoadJson(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-jsonio-"))

    def test_missing_returns_default(self) -> None:
        self.assertEqual(load_json(self.tmp / "nope.json", {"fallback": True}), {"fallback": True})
        self.assertIsNone(load_json(self.tmp / "nope.json"))

    def test_corrupt_returns_default_instead_of_raising(self) -> None:
        """Regression: a corrupt state file used to take the worker down."""
        path = self.tmp / "broken.json"
        path.write_text("{not json at all")
        self.assertEqual(load_json(path, {"fallback": True}), {"fallback": True})

    def test_wrong_type_is_returned_as_is(self) -> None:
        path = self.tmp / "list.json"
        path.write_text("[1, 2, 3]")
        self.assertEqual(load_json(path), [1, 2, 3])


class TestAtomicJsonStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-store-"))
        self.store = AtomicJsonStore(self.tmp / "doc.json", {"tokens": {}})

    def test_read_missing_returns_default(self) -> None:
        self.assertEqual(self.store.read(), {"tokens": {}})

    def test_update_persists(self) -> None:
        self.store.update(lambda doc: {**doc, "tokens": {"a": 1}})
        self.assertEqual(json.loads((self.tmp / "doc.json").read_text())["tokens"], {"a": 1})

    def test_concurrent_updates_lose_nothing(self) -> None:
        def add(key: str) -> None:
            def mutate(doc):
                doc.setdefault("tokens", {})[key] = True
                return doc

            self.store.update(mutate)

        threads = [threading.Thread(target=add, args=(f"k{i}",)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.read()["tokens"]), 25)


class TestCachedJsonFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dai-cache-"))
        self.path = self.tmp / "cat.json"
        self.cache = CachedJsonFile(self.path, {"models": []})

    def test_missing_file_uses_default(self) -> None:
        self.assertEqual(self.cache.get(), {"models": []})

    def test_reads_then_caches(self) -> None:
        self.path.write_text(json.dumps({"models": [1, 2]}))
        first = self.cache.get()
        self.assertEqual(first["models"], [1, 2])
        # Same mtime → identical object returned (no re-parse).
        self.assertIs(self.cache.get(), first)

    def test_edit_is_picked_up(self) -> None:
        self.path.write_text(json.dumps({"models": [1]}))
        self.cache.get()
        self.path.write_text(json.dumps({"models": [1, 2, 3]}))
        bump_mtime(self.path)
        self.assertEqual(self.cache.get()["models"], [1, 2, 3])

    def test_corrupt_edit_keeps_last_good_copy(self) -> None:
        self.path.write_text(json.dumps({"models": ["good"]}))
        self.cache.get()
        self.path.write_text("{broken")
        bump_mtime(self.path)
        self.assertEqual(self.cache.get()["models"], ["good"])
        self.assertIn("invalid JSON", self.cache.error or "")

    def test_invalidate_forces_reread(self) -> None:
        self.path.write_text(json.dumps({"models": [1]}))
        self.cache.get()
        self.cache.invalidate()
        self.path.write_text(json.dumps({"models": [9]}))
        self.assertEqual(self.cache.get()["models"], [9])

    def test_as_dict_returns_empty_dict_for_a_non_object_document(self) -> None:
        """Callers index the result; a list/scalar document must not escape."""
        self.path.write_text("[1,2,3]")
        self.assertEqual(self.cache.as_dict(), {})
        self.path.write_text('"a string"')
        bump_mtime(self.path, 20)
        self.assertEqual(self.cache.as_dict(), {})


if __name__ == "__main__":
    unittest.main()
