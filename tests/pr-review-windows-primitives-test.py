#!/usr/bin/env python3
"""Exercise native Windows ACL, no-follow and installer-lock behavior."""

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "review/scripts/runtime_windows.py"
SPEC = importlib.util.spec_from_file_location("runtime_windows", MODULE)
assert SPEC is not None and SPEC.loader is not None
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


@unittest.skipUnless(os.name == "nt", "native Windows filesystem protections")
class WindowsStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-windows-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.storage = self.base / "private"
        native.make_owned_directory(self.storage)

    def grant_everyone(self, path: Path, rights: str) -> None:
        """Add a controlled unsafe grant without depending on localized account names.

        Args:
            path (Path): Disposable test object to change.
            rights (str): icacls permission token to grant to Everyone.

        Raises:
            AssertionError: Windows refuses the fixture ACL change.
        """
        result = subprocess.run(["icacls", str(path), "/grant", f"*S-1-1-0:({rights})"],
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def junction(self, target: Path, link: Path) -> None:
        """Create a real junction without Administrator or Developer Mode privileges.

        Args:
            target (Path): Existing disposable target directory.
            link (Path): New junction path.
        """
        import _winapi
        _winapi.CreateJunction(str(target), str(link))

    def test_private_creation_and_unsafe_existing_leaf(self):
        leaf = self.storage / "nested" / "leaf"
        native.make_owned_directory(leaf)
        native.owned_directory(leaf)
        self.grant_everyone(leaf, "M")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.owned_directory(leaf)
        with self.assertRaises(ValueError):
            native.make_owned_directory(leaf)

    def test_process_default_owner_is_accepted_for_created_storage(self):
        # Elevated Windows tokens can default new objects to Administrators;
        # their ordinary tempfile/mkdir output must remain valid managed storage.
        ordinary = self.storage / "ordinary"
        ordinary.mkdir()
        native.owned_directory(ordinary)
        native.make_owned_directory(ordinary / "private-child")
        native.remove_retired_directory(ordinary)
        self.assertFalse(ordinary.exists())

    def test_owner_rights_grants_only_the_validated_owner(self):
        directory = self.storage / "owner-rights"
        native.make_owned_directory(directory)
        result = subprocess.run(["icacls", str(directory), "/grant", "*S-1-3-4:(OI)(CI)(F)"],
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        native.owned_directory(directory)
        native.make_owned_directory(directory / "child")
        self.grant_everyone(directory, "M")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.owned_directory(directory)
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.validate_storage_ancestors(directory, include_self=True)

    def test_ancestor_create_rights_are_safe_but_replacement_rights_are_not(self):
        ancestor = self.storage / "ancestor"
        native.make_owned_directory(ancestor)
        self.grant_everyone(ancestor, "AD")
        native.validate_storage_ancestors(ancestor, include_self=True)
        leaf = ancestor / "leaf"
        native.make_owned_directory(leaf)
        native.owned_directory(leaf)
        self.grant_everyone(ancestor, "DC")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.validate_storage_ancestors(leaf)

    def test_child_delete_rights_also_block_replacement(self):
        child = self.storage / "child"
        native.make_owned_directory(child)
        self.grant_everyone(child, "D")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.validate_storage_ancestors(child, include_self=True)

    def test_no_follow_rejects_junction_storage_and_lock(self):
        target = self.storage / "target"
        native.make_owned_directory(target)
        link = self.storage / "link"
        self.junction(target, link)
        with self.assertRaises(ValueError):
            native.owned_directory(link)
        with self.assertRaises(ValueError):
            native.make_owned_directory(link / "new")
        with self.assertRaises((OSError, ValueError)), native.open_regular(link):
            pass
        with self.assertRaises((OSError, ValueError)), native.install_lock(link):
            pass

    def test_lock_contention_is_nonblocking_between_processes(self):
        lock = self.storage / ".install.lock"
        code = """
import importlib.util
import sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("runtime_windows", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    with module.install_lock(Path(sys.argv[2])):
        print("acquired")
except BlockingIOError:
    print("contended")
"""
        with native.install_lock(lock):
            result = subprocess.run([sys.executable, "-c", code, str(MODULE), str(lock)],
                                    text=True, capture_output=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "contended")
        with native.install_lock(lock):
            pass

    def test_lock_rejects_externally_writable_regular_file(self):
        lock = self.storage / ".install.lock"
        with native.install_lock(lock):
            pass
        self.grant_everyone(lock, "M")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"), native.install_lock(lock):
            pass

    def test_pointer_reader_keeps_old_bytes_during_atomic_replacement(self):
        pointer = self.storage / ".release"
        replacement = self.storage / ".next"
        pointer.write_bytes(b"old")
        replacement.write_bytes(b"new")
        with native.open_regular(pointer) as pinned:
            native.replace_file(replacement, pointer)
            self.assertEqual(pinned.read(), b"old")
        with native.open_regular(pointer) as current:
            self.assertEqual(current.read(), b"new")

    def test_initial_pointer_publication_and_unsafe_target_rejection(self):
        pointer = self.storage / ".release"
        pending = self.storage / "pending"
        native.make_owned_directory(pending)
        replacement = pending / ".next"
        replacement.write_bytes(b"initial")
        native.replace_file(replacement, pointer)
        self.assertEqual(pointer.read_bytes(), b"initial")
        self.assertFalse(replacement.exists())
        self.grant_everyone(pointer, "M")
        replacement.write_bytes(b"unsafe")
        with self.assertRaisesRegex(ValueError, "external Windows mutation"):
            native.replace_file(replacement, pointer)
        self.assertEqual(pointer.read_bytes(), b"initial")
        self.assertEqual(replacement.read_bytes(), b"unsafe")

    def test_retired_cleanup_removes_readonly_files_without_following_junction(self):
        retired = self.storage / "retired"
        outside = self.storage / "outside"
        native.make_owned_directory(retired)
        native.make_owned_directory(outside)
        (outside / "preserved").write_text("keep")
        self.junction(outside, retired / "junction")
        readonly = retired / "readonly.py"
        readonly.write_text("old")
        readonly.chmod(stat.S_IREAD)
        native.remove_retired_directory(retired)
        self.assertFalse(retired.exists())
        self.assertEqual((outside / "preserved").read_text(), "keep")
        self.assertFalse((outside / "readonly.py").exists())


if __name__ == "__main__":
    unittest.main()
