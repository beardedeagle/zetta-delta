#!/usr/bin/env python3
"""Prove native Windows installation safety without running the POSIX engine."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ImportTests(unittest.TestCase):
    def test_native_import_does_not_require_fcntl(self):
        result = subprocess.run([sys.executable, "-B", "-c", """
import builtins, runpy, sys
original = builtins.__import__
def native_import(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('No module named fcntl')
    return original(name, *args, **kwargs)
if sys.platform == 'win32':
    builtins.__import__ = native_import
runpy.run_path(sys.argv[1], run_name='import_probe')
""", str(ROOT / "review/scripts/runtime.py")], capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(os.name == "nt", "requires native Windows filesystem and ACLs")
class WindowsInstallationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("review_runtime", ROOT / "review/scripts/runtime.py")
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        temporary = tempfile.TemporaryDirectory(prefix="review-windows-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.destination = self.base / "review"
        self.backup = self.base / "backup"
        self.sources = self.runtime.install_sources()

    def install(self, **kwargs):
        return self.runtime.install(self.destination, self.backup, **kwargs)

    def identity(self):
        result = subprocess.run([sys.executable, "-B", str(self.destination / "scripts/runtime.py"), "identity"],
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_dry_run_is_read_only_and_upgrade_pins_complete_releases(self):
        planned = self.install(dry_run=True)
        self.assertTrue(planned["dry_run"])
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.backup.exists())
        first = self.install()
        self.assertEqual(self.identity(), first["runtime"])
        self.assertFalse((self.destination / "scripts").is_symlink())
        second = self.install(force=True)
        self.assertFalse(second["changed"])
        changed = self.base / "source"
        changed.mkdir()
        for name, source in self.sources.items():
            shutil.copy2(source, changed / name)
        with (changed / "runtime.py").open("a") as stream:
            stream.write("\n# distinguish the next release\n")
        second = self.install(force=True, sources={p.name: p for p in changed.iterdir()})
        self.assertNotEqual(second["runtime"], first["runtime"])
        self.assertEqual(self.identity(), second["runtime"])
        self.assertEqual(self.runtime.code_identity(Path(second["backup"])), first["runtime"])
        self.assertTrue(Path(first["release"]).is_dir())
        self.assertTrue(Path(second["backup"]).is_relative_to(self.backup))

    def test_lock_contention_does_not_modify_activation(self):
        first = self.install()
        with self.runtime.windows.install_lock(self.destination / ".install.lock"), self.assertRaises(BlockingIOError):
            self.install(force=True)
        self.assertEqual(self.identity(), first["runtime"])

    def test_forced_upgrade_replaces_old_bootstraps_and_preserves_their_bytes(self):
        first = self.install()
        old = (self.destination / "scripts/runtime.py").read_bytes()
        replacement = self.runtime.WINDOWS_BOOTSTRAP + "\n# next bootstrap revision\n"
        with patch.object(self.runtime, "WINDOWS_BOOTSTRAP", replacement):
            result = self.install(force=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.identity(), first["runtime"])
        self.assertEqual((Path(result["backup"]) / ".entrypoints/runtime.py").read_bytes(), old)
        # A damaged regular entrypoint can also be repaired by the source installer.
        self.backup = self.base / "repair-backup"
        (self.destination / "scripts/runtime.py").write_bytes(b"broken\xff")
        repaired = self.install(force=True)
        self.assertTrue(repaired["changed"])
        self.assertEqual(self.identity(), first["runtime"])

    def test_legacy_backup_and_failed_pointer_publication_preserve_old_code(self):
        self.runtime.make_owned_directory(self.destination / "scripts")
        legacy = self.destination / "scripts/legacy.txt"
        legacy.write_text("preserve me")
        original = os.replace

        def fail_pointer(source, target):
            if Path(target) == self.destination / ".release":
                raise OSError("injected activation failure")
            return original(source, target)

        with (patch.object(os, "replace", side_effect=fail_pointer),
              patch.object(self.runtime, "remove_retired_directory", side_effect=PermissionError("cleanup unavailable")),
              self.assertRaisesRegex(OSError, "injected activation failure")):
            self.install(force=True)
        self.assertEqual(legacy.read_text(), "preserve me")
        self.assertFalse((self.destination / ".release").exists())
        # Keep the failed attempt's backup intact; a fresh root is required.
        self.backup = self.base / "backup-next"
        result = self.install(force=True)
        self.assertEqual((Path(result["backup"]) / "legacy.txt").read_text(), "preserve me")
        self.assertEqual(self.identity(), result["runtime"])

    def test_native_shell_runtime_only_install_uses_same_backend(self):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "Windows CI must provide Git Bash")
        home = self.base / "home"
        home.mkdir()
        result = subprocess.run([bash, str(ROOT / "install.sh"), "--review-runtime-only", "--dry-run"],
                                env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((home / ".local").exists())


if __name__ == "__main__":
    unittest.main()
