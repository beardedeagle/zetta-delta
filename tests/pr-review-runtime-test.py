#!/usr/bin/env python3
"""Exercise portable runtime identities and atomic release installation offline."""

import errno
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_runtime", ROOT / "review/scripts/runtime.py")
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-runtime-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        for name, path in runtime.install_sources().items():
            shutil.copy2(path, self.source / name)
        self.destination = self.base / "review"
        self.backup = self.base / "backup"

    def install(self, **kwargs):
        return runtime.install(self.destination, self.backup, sources={p.name: p for p in self.source.iterdir()}, **kwargs)

    def legacy_scripts(self) -> Path:
        """Create legacy content with metadata and links that a backup must retain.

        Returns:
            Path: Existing scripts directory with representative user-owned files.
        """
        scripts = self.destination / "scripts"
        nested = scripts / "nested"
        nested.mkdir(parents=True)
        payload = nested / "old.bin"
        payload.write_bytes(b"legacy runtime\x00\xff\n")
        payload.chmod(0o640)
        (scripts / "relative-link").symlink_to("nested/old.bin")
        os.utime(payload, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
        nested.chmod(0o750)
        os.utime(nested, ns=(1_700_000_002_000_000_000, 1_700_000_003_000_000_000))
        return scripts

    def legacy_record(self, root: Path) -> dict:
        """Capture retained content, permissions, timestamps and link targets.

        Args:
            root (Path): Legacy scripts or its completed backup copy.

        Returns:
            dict: Comparable records for the directory and all its entries.
        """
        result = {}
        for path in (root, *sorted(root.rglob("*"))):
            info = path.lstat()
            value = {"mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
            if path.is_symlink():
                value["link"] = os.readlink(path)
            elif path.is_file():
                value["bytes"] = path.read_bytes()
            result[str(path.relative_to(root))] = value
        return result

    def cross_filesystem_rename(self, source: Path, target: Path) -> Path:
        """Model an EXDEV boundary between runtime storage and its backup root.

        Args:
            source (Path): Rename source selected by the installer.
            target (Path): Requested rename destination.

        Returns:
            Path: Renamed path for a same-filesystem operation.

        Raises:
            OSError: The operation crosses the simulated filesystem boundary.
        """
        if ((source.is_relative_to(self.destination) and Path(target).is_relative_to(self.backup))
                or (source.is_relative_to(self.backup) and Path(target).is_relative_to(self.destination))):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(source), str(target))
        return self.rename(source, target)

    def test_identity_is_portable_and_detects_owned_code_and_schema_drift(self):
        original = runtime.identity()
        with patch.object(runtime, "__file__", str(self.source / "runtime.py")):
            self.assertEqual(runtime.identity(), original)
            runtime.verify_recorded(original)
            (self.source / "workflow.py").write_text("# changed implementation\n")
            with self.assertRaisesRegex(ValueError, "runtime.*differs"):
                runtime.verify_recorded(original)
        for malformed in (None, {}, dict(original, schema_version=True), dict(original, schema_version=999),
                          dict(original, fingerprint="0" * 64), dict(original, files={})):
            with self.subTest(expected=malformed), self.assertRaises(ValueError):
                runtime.verify_recorded(malformed)

    def test_external_helpers_do_not_change_runtime_identity(self):
        with patch.object(runtime, "__file__", str(self.source / "runtime.py")):
            expected = runtime.identity()
            (self.source / "identity.py").write_text("# independent identity helper\n")
            (self.source / "ensure-context-indexes.py").write_text("# independent index helper\n")
            self.assertEqual(runtime.identity(), expected)

    def test_group_writable_umask_keeps_new_runtime_and_backup_directories_private(self):
        existing = self.base / "existing"
        existing.mkdir(mode=0o750)
        peer = existing / "peer.txt"
        peer.write_text("unrelated peer data\n")
        before = self.legacy_record(existing)
        self.destination = existing / "new/share/review"
        self.backup = existing / "state/backups/current"
        previous = os.umask(0o002)
        try:
            first = self.install()
            original = Path(first["release"])
            (self.source / "workflow.py").write_text("# upgraded runtime\n")
            result = self.install(force=True)
            self.assertFalse(self.install(force=True)["changed"])
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), before["."]["mode"])
        self.assertEqual(peer.read_bytes(), before["peer.txt"]["bytes"])
        self.assertEqual(stat.S_IMODE(peer.stat().st_mode), before["peer.txt"]["mode"])
        for path in existing.rglob("*"):
            if stat.S_ISDIR(path.lstat().st_mode):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
        self.assertEqual(Path(result["backup"]).resolve(), original)
        with patch.object(runtime, "__file__", str(self.destination / "scripts/runtime.py")):
            self.assertEqual(runtime.identity(), result["runtime"])

    def test_existing_group_writable_directory_is_rejected_without_changing_it(self):
        self.destination.mkdir()
        peer = self.destination / "peer.txt"
        peer.write_text("unrelated peer data\n")
        self.destination.chmod(0o775)
        before = self.legacy_record(self.destination)
        previous = os.umask(0o002)
        try:
            with self.assertRaisesRegex(ValueError, "external write access"):
                self.install()
        finally:
            os.umask(previous)
        self.assertEqual(self.legacy_record(self.destination), before)
        self.assertFalse(self.backup.exists())

    def test_manifest_is_verified_and_legacy_files_are_preserved_before_switch(self):
        scripts = self.destination / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "local.py").write_text("preserve this legacy runtime\n")
        with self.assertRaisesRegex(ValueError, "force"):
            self.install()
        self.assertFalse((self.destination / "releases").exists())
        result = self.install(force=True)
        self.assertTrue(scripts.is_symlink())
        pinned = scripts.resolve()
        self.assertTrue(pinned.is_relative_to(self.destination / "releases"))
        self.assertEqual((Path(result["backup"]) / "local.py").read_text(), "preserve this legacy runtime\n")
        with patch.object(runtime, "__file__", str(pinned / "runtime.py")):
            expected = runtime.identity()
            self.assertEqual(expected, result["runtime"])
            manifest = pinned / "runtime-manifest.json"
            data = json.loads(manifest.read_text())
            data["runtime"]["fingerprint"] = "0" * 64
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "manifest"):
                runtime.identity()

    def test_release_switch_keeps_old_release_pinned_and_same_install_is_idempotent(self):
        first = self.install()
        scripts = self.destination / "scripts"
        old = scripts.resolve()
        original = (old / "workflow.py").read_bytes()
        self.assertFalse(self.install(force=True)["changed"])
        (self.source / "workflow.py").write_text("# second release\n")
        second = self.install(force=True)
        self.assertNotEqual(scripts.resolve(), old)
        self.assertEqual((old / "workflow.py").read_bytes(), original)
        self.assertNotEqual(first["runtime"], second["runtime"])
        self.assertTrue(Path(second["backup"]).is_symlink())
        self.assertEqual(Path(second["backup"]).resolve(), old)

    def test_dry_run_and_source_race_leave_the_active_release_unchanged(self):
        self.install()
        scripts = self.destination / "scripts"
        original = scripts.resolve()
        (self.source / "workflow.py").write_text("# candidate release\n")
        self.install(force=True, dry_run=True)
        self.assertEqual(scripts.resolve(), original)
        original_copy = runtime.shutil.copy2

        def changed_during_copy(source, target, *args, **kwargs):
            result = original_copy(source, target, *args, **kwargs)
            if Path(source).name == "workflow.py":
                Path(target).write_text("# raced bytes\n")
            return result

        with (patch.object(runtime.shutil, "copy2", side_effect=changed_during_copy),
              self.assertRaisesRegex(ValueError, "source changed")):
            self.install(force=True)
        self.assertEqual(scripts.resolve(), original)
        self.assertEqual(list((self.destination / "releases").glob(".pending-*")), [])

    def test_redirected_release_paths_are_refused_without_external_mutation(self):
        external = self.base / "external"
        external.mkdir()
        self.destination.mkdir()
        (self.destination / "scripts").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "owned release"):
            self.install(force=True)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((self.destination / "releases").exists())

    def test_forced_install_can_replace_a_broken_manifest_without_changing_old_bytes(self):
        self.install()
        old = (self.destination / "scripts").resolve()
        (old / "runtime-manifest.json").write_text("broken receipt\n")
        result = self.install(force=True)
        self.assertTrue(result["changed"])
        self.assertEqual((old / "runtime-manifest.json").read_text(), "broken receipt\n")
        with patch.object(runtime, "__file__", str(self.destination / "scripts/runtime.py")):
            self.assertEqual(runtime.identity(), result["runtime"])

    def test_legacy_backup_on_another_filesystem_preserves_content_and_metadata(self):
        scripts = self.legacy_scripts()
        expected = self.legacy_record(scripts)
        self.rename = Path.rename
        with patch.object(Path, "rename", autospec=True, side_effect=self.cross_filesystem_rename):
            result = self.install(force=True)
        self.assertTrue(scripts.is_symlink())
        backup = Path(result["backup"])
        self.assertTrue(backup.is_dir())
        self.assertFalse(backup.is_symlink())
        self.assertEqual(self.legacy_record(backup), expected)
        self.assertEqual(list(self.destination.glob(".scripts-previous-*")), [])
        with patch.object(runtime, "__file__", str(scripts / "runtime.py")):
            self.assertEqual(runtime.identity(), result["runtime"])

    def test_legacy_readonly_directory_cleanup_preserves_backup_metadata(self):
        scripts = self.legacy_scripts()
        (scripts / "nested").chmod(0o555)
        external = self.base / "external"
        external.mkdir(mode=0o555)
        (scripts / "external-link").symlink_to(external, target_is_directory=True)
        expected = self.legacy_record(scripts)
        result = self.install(force=True)
        self.assertTrue(scripts.is_symlink())
        self.assertEqual(self.legacy_record(Path(result["backup"])), expected)
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o555)
        self.assertEqual(list(self.destination.glob(".scripts-previous-*")), [])
        with patch.object(runtime, "__file__", str(scripts / "runtime.py")):
            self.assertEqual(runtime.identity(), result["runtime"])

    def test_command_reports_success_with_retained_copy_when_cleanup_fails(self):
        scripts = self.legacy_scripts()
        expected = self.legacy_record(scripts)
        output = io.StringIO()
        with (patch.object(runtime, "remove_retired_directory",
                           side_effect=OSError(errno.EBUSY, "simulated retired runtime cleanup failure")),
              patch.object(runtime, "install_sources", return_value={p.name: p for p in self.source.iterdir()}),
              patch.object(runtime.sys, "argv", ["runtime.py", "install", "--destination", str(self.destination),
                                                 "--backup-root", str(self.backup), "--force"]),
              redirect_stdout(output)):
            self.assertEqual(runtime.main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["changed"])
        self.assertEqual(scripts.resolve(), Path(result["release"]))
        self.assertEqual(self.legacy_record(Path(result["backup"])), expected)
        self.assertTrue(Path(result["cleanup_pending"]["path"]).is_dir())
        self.assertIn("simulated retired runtime cleanup failure", result["cleanup_pending"]["error"])

    def test_legacy_backup_copy_failure_leaves_original_active_and_no_partial_backup(self):
        scripts = self.legacy_scripts()
        expected = self.legacy_record(scripts)
        copytree = shutil.copytree

        def incomplete_backup(source, target, *args, **kwargs):
            result = copytree(source, target, *args, **kwargs)
            if Path(source) == scripts:
                raise OSError(errno.ENOSPC, "simulated incomplete backup")
            return result

        with (patch.object(runtime.shutil, "copytree", side_effect=incomplete_backup),
              self.assertRaisesRegex(OSError, "incomplete backup")):
            self.install(force=True)
        self.assertFalse(scripts.is_symlink())
        self.assertEqual(self.legacy_record(scripts), expected)
        self.assertFalse((self.backup / str(scripts).lstrip(os.sep)).exists())
        self.assertEqual(list(self.backup.rglob(".scripts-backup-*")), [])
        self.assertEqual(list(self.destination.glob(".scripts-previous-*")), [])

    def test_legacy_switch_failure_restores_local_original_with_cross_filesystem_backup(self):
        scripts = self.legacy_scripts()
        expected = self.legacy_record(scripts)
        self.rename = Path.rename
        replace = Path.replace

        def failed_switch(source, target):
            if source.name.startswith(".scripts-") and Path(target) == scripts:
                raise OSError(errno.EACCES, "simulated runtime switch failure")
            return replace(source, target)

        with (patch.object(Path, "rename", autospec=True, side_effect=self.cross_filesystem_rename),
              patch.object(Path, "replace", autospec=True, side_effect=failed_switch),
              self.assertRaisesRegex(OSError, "switch failure")):
            self.install(force=True)
        self.assertFalse(scripts.is_symlink())
        self.assertEqual(self.legacy_record(scripts), expected)
        self.assertEqual(self.legacy_record(self.backup / str(scripts).lstrip(os.sep)), expected)
        self.assertEqual(list(self.destination.glob(".scripts-previous-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
