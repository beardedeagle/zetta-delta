#!/usr/bin/env python3
"""Exercise portable runtime identities and atomic release installation offline."""

import errno
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
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

    def pending_helper_upgrade(self, *, legacy: bool = False) -> tuple:
        """Retain an offline setup record while replacing its installed release.

        Args:
            legacy (bool): Exercise the first scripts-directory conversion.

        Returns:
            tuple: Git fixture, new preparation module, staged source and old setup.
        """
        with patch.object(sys, "path", [str(ROOT / "review/scripts"), *sys.path]):
            specification = importlib.util.spec_from_file_location("helper_git_fixtures", ROOT / "tests/pr-review-preparation-test.py")
            assert specification is not None and specification.loader is not None
            fixtures = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(fixtures)
        fixture = fixtures.GitFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.enterContext(patch.object(Path, "home", return_value=self.base / "isolated-home"))
        self.enterContext(patch.object(shutil, "which", return_value=None))

        def load(release):
            specification = importlib.util.spec_from_file_location("installed_preparation", release / "preparation.py")
            assert specification is not None and specification.loader is not None
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
            return module

        if legacy:
            old = self.destination / "scripts"
            shutil.copytree(self.source, old)
        else:
            old = Path(self.install()["release"])
        before = load(old)
        staged = before.prepare(fixture.directory, "run-fixture", "local", fixture.local, fixture.parent)
        request = before.setup_request(fixture.directory, "run-fixture", "local", staged, ["tgrep"])
        if not legacy:
            source = self.source / "workflow.py"
            source.write_bytes(source.read_bytes() + b"\n# next repository release\n")
        installed = self.install(force=True)
        return fixture, load(Path(installed["release"])), staged, request

    def assert_helper_upgrade_lifecycle(self, *, legacy: bool = False) -> None:
        """Verify exact helper pins survive setup, sealing and owned cleanup.

        Args:
            legacy (bool): Exercise the first scripts-directory conversion.
        """
        fixture, preparation, staged, request = self.pending_helper_upgrade(legacy=legacy)
        original_request = json.dumps(request, sort_keys=True)
        resource = fixture.directory / staged["preparation"]["resource"]
        originals = {name: (resource / name).read_bytes() for name in staged["preparation"]["artifacts"]}
        preparation.run_setup(staged, request)
        frozen = preparation.finalize(fixture.directory, "run-fixture", "local", staged, request, ["tgrep"],
                                       "Offline fixture uses static evidence for the unavailable index.")
        self.assertEqual(preparation.check(fixture.directory, "run-fixture", "local", frozen, {})["status"], "unchanged")
        self.assertTrue(all((resource / name).read_bytes() == content for name, content in originals.items()))
        self.assertEqual(json.dumps(request, sort_keys=True), original_request)
        self.assertEqual(preparation.cleanup(fixture.directory, "run-fixture", "local", request, resource.name), [resource.name])
        self.assertFalse(resource.exists())

    def test_pending_setup_keeps_its_authenticated_helper_across_release_upgrade(self):
        self.assert_helper_upgrade_lifecycle()

    def test_pending_setup_keeps_its_trusted_helper_alias_after_legacy_conversion(self):
        self.assert_helper_upgrade_lifecycle(legacy=True)

    def test_pending_setup_rejects_untrusted_missing_and_changed_helper_pins(self):
        fixture, preparation, staged, request = self.pending_helper_upgrade()
        source = Path(staged["source_root"])
        helper = Path(request["helper"])
        arbitrary = self.base / "arbitrary-helper.py"
        shutil.copy2(helper, arbitrary)
        elsewhere = runtime.install(self.base / "other-installation", self.base / "other-backup",
                                    sources={path.name: path for path in self.source.iterdir()})
        for path in (arbitrary, Path(elsewhere["release"]) / helper.name):
            with (self.subTest(path=path), self.assertRaises(preparation.NeedsDecision),
                  preparation.idle_indexes(source, dict(request, helper=str(path)))):
                pass
        original = helper.read_bytes()
        for missing in (False, True):
            try:
                if missing:
                    helper.unlink()
                else:
                    helper.write_bytes(original + b"\n# changed retained helper\n")
                with (self.subTest(missing=missing), self.assertRaises(preparation.NeedsDecision),
                      preparation.idle_indexes(source, request)):
                    pass
            finally:
                helper.write_bytes(original)
        self.assertTrue((fixture.directory / staged["preparation"]["resource"]).is_dir())

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

    def test_installed_helpers_must_match_the_installation_receipt(self):
        installed = self.install()
        release = Path(installed["release"])
        with patch.object(runtime, "__file__", str(release / "runtime.py")):
            self.assertEqual(runtime.identity(), installed["runtime"])
            runtime.verify_recorded(installed["runtime"])
            for name in runtime.HELPERS:
                helper = release / name
                original = helper.read_bytes()
                try:
                    helper.write_bytes(original + b"\n# changed installed helper\n")
                    with self.subTest(helper=name), self.assertRaisesRegex(ValueError, "manifest"):
                        runtime.verify_recorded(installed["runtime"])
                finally:
                    helper.write_bytes(original)

    def test_installed_receipt_requires_complete_consistent_file_hashes(self):
        installed = self.install()
        release = Path(installed["release"])
        manifest = release / runtime.MANIFEST
        original = json.loads(manifest.read_text())
        missing = {name: value for name, value in original.items() if name != "installed_files"}
        cases = [missing, dict(original, version=True), dict(original, version=2)]
        for installed_files in (None, [], {},
                                {name: digest for name, digest in original["installed_files"].items() if name != "identity.py"},
                                dict(original["installed_files"], **{"identity.py": True}),
                                dict(original["installed_files"], **{"identity.py": "0" * 64}),
                                dict(original["installed_files"], **{"workflow.py": "0" * 64}),
                                dict(original["installed_files"], unexpected="0" * 64)):
            cases.append(dict(original, installed_files=installed_files))
        with patch.object(runtime, "__file__", str(release / "runtime.py")):
            for index, value in enumerate(cases):
                manifest.write_text(json.dumps(value))
                with self.subTest(case=index), self.assertRaisesRegex(ValueError, "manifest"):
                    runtime.identity()

    def test_legitimate_helper_only_release_preserves_aggregate_runtime_identity(self):
        first = self.install()
        for name in runtime.HELPERS:
            path = self.source / name
            path.write_bytes(path.read_bytes() + b"\n# independent helper release\n")
        second = self.install(force=True)
        self.assertTrue(second["changed"])
        self.assertNotEqual(second["release"], first["release"])
        self.assertEqual(second["runtime"], first["runtime"])
        for installed in (first, second):
            with patch.object(runtime, "__file__", str(Path(installed["release"]) / "runtime.py")):
                runtime.verify_recorded(first["runtime"])

    def test_force_install_repairs_invalid_receipts_instead_of_reporting_idempotence(self):
        installed = self.install()
        for index, kind in enumerate(("helper-hash", "missing-files", "version")):
            with self.subTest(receipt=kind):
                release = Path(installed["release"])
                manifest = release / runtime.MANIFEST
                data = json.loads(manifest.read_text())
                if kind == "helper-hash":
                    data["installed_files"]["identity.py"] = "0" * 64
                elif kind == "missing-files":
                    data.pop("installed_files")
                else:
                    data["version"] = True
                manifest.write_text(json.dumps(data))
                original = manifest.read_bytes()
                self.backup = self.base / f"receipt-backup-{index}"
                repaired = self.install(force=True)
                self.assertTrue(repaired["changed"])
                self.assertNotEqual(repaired["release"], installed["release"])
                self.assertEqual(manifest.read_bytes(), original)
                with patch.object(runtime, "__file__", str(Path(repaired["release"]) / "runtime.py")):
                    runtime.verify_recorded(installed["runtime"])
                self.assertFalse(self.install(force=True)["changed"])
                installed = repaired

    def test_versioned_releases_require_receipts_without_disabling_legacy_source_use(self):
        installed = self.install()
        release = Path(installed["release"])
        (release / runtime.MANIFEST).unlink()
        helper = release / "identity.py"
        helper.write_bytes(helper.read_bytes() + b"\n# unrecorded helper drift\n")
        with (patch.object(runtime, "__file__", str(release / "runtime.py")),
              self.assertRaises((OSError, ValueError))):
            runtime.verify_recorded(installed["runtime"])
        legacy = self.base / "legacy/scripts"
        shutil.copytree(self.source, legacy)
        with patch.object(runtime, "__file__", str(legacy / "runtime.py")):
            runtime.verify_recorded(installed["runtime"])

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

    def test_writable_nonsticky_runtime_and_backup_ancestors_are_rejected_without_changes(self):
        for owner in ("runtime", "backup"):
            for mode in (0o775, 0o777):
                with self.subTest(storage=owner, mode=mode):
                    name = owner + "-" + str(mode)
                    shared = self.base / ("shared-" + name)
                    shared.mkdir()
                    shared.chmod(mode)
                    private = shared / "private"
                    private.mkdir(mode=0o700)
                    (private / "peer.txt").write_text("unrelated peer data\n")
                    self.destination = self.base / ("review-" + name)
                    self.backup = self.base / ("backup-" + name)
                    selected = private / "owned"
                    selected.mkdir(mode=0o700)
                    if owner == "runtime":
                        self.destination = selected
                    else:
                        self.backup = selected
                    before = self.legacy_record(shared)
                    with self.assertRaisesRegex(ValueError, "ancestor"):
                        self.install()
                    self.assertEqual(self.legacy_record(shared), before)
                    self.assertFalse((self.backup if owner == "runtime" else self.destination).exists())

    def test_trusted_sticky_system_temp_ancestors_support_owned_runtime_storage(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-runtime-sticky-", dir=Path("/tmp").resolve())
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name).resolve()
        sticky = parent.parent
        before = sticky.stat()
        self.assertTrue(before.st_mode & stat.S_ISVTX)
        self.assertIn(before.st_uid, (0, os.getuid()))
        self.destination = parent / "review"
        self.backup = parent / "backup"
        installed = self.install()
        self.assertEqual(stat.S_IMODE(sticky.stat().st_mode), stat.S_IMODE(before.st_mode))
        with patch.object(runtime, "__file__", str(Path(installed["release"]) / "runtime.py")):
            self.assertEqual(runtime.identity(), installed["runtime"])

    def test_foreign_owned_ancestor_is_rejected_without_changing_storage(self):
        ancestor = self.base / "foreign"
        ancestor.mkdir(mode=0o755)
        self.destination = ancestor / "review"
        self.destination.mkdir(mode=0o700)
        (self.destination / "peer.txt").write_text("unrelated peer data\n")
        before = self.legacy_record(ancestor)
        lstat = Path.lstat

        def foreign_metadata(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == ancestor:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        with (patch.object(Path, "lstat", autospec=True, side_effect=foreign_metadata),
              self.assertRaisesRegex(ValueError, "ancestor")):
            self.install()
        self.assertEqual(self.legacy_record(ancestor), before)
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

    def test_competing_install_cannot_be_replaced_without_force_after_preflight(self):
        other = self.base / "competing-source"
        shutil.copytree(self.source, other)
        (other / "workflow.py").write_text("# competing complete release\n")
        flock = runtime.fcntl.flock
        competitor = {}
        injected = False

        def complete_before_lock(descriptor, operation):
            nonlocal injected
            if not injected:
                injected = True
                competitor.update(runtime.install(self.destination, self.base / "competing-backup",
                                  sources={path.name: path for path in other.iterdir()}))
            return flock(descriptor, operation)

        with (patch.object(runtime.fcntl, "flock", side_effect=complete_before_lock),
              self.assertRaisesRegex(ValueError, "force")):
            self.install()
        self.assertEqual((self.destination / "scripts").resolve(), Path(competitor["release"]))
        self.assertEqual((Path(competitor["release"]) / "workflow.py").read_bytes(),
                         (other / "workflow.py").read_bytes())
        self.assertEqual(list((self.destination / "releases").iterdir()), [Path(competitor["release"]).parent])
        self.assertFalse(self.backup.exists())

    def test_idempotence_cannot_accept_an_external_target_swapped_before_lock(self):
        first = self.install()
        scripts = self.destination / "scripts"
        external = self.base / "external"
        shutil.copytree(Path(first["release"]), external)
        external.chmod(0o775)
        before = self.legacy_record(external)
        flock = runtime.fcntl.flock

        def redirect_before_lock(descriptor, operation):
            scripts.unlink()
            scripts.symlink_to(external, target_is_directory=True)
            return flock(descriptor, operation)

        with (patch.object(runtime.fcntl, "flock", side_effect=redirect_before_lock),
              self.assertRaisesRegex(ValueError, "owned release")):
            self.install(force=True)
        self.assertEqual(scripts.resolve(), external)
        self.assertEqual(self.legacy_record(external), before)
        self.assertEqual(list((self.destination / "releases").iterdir()), [Path(first["release"]).parent])
        self.assertFalse(self.backup.exists())

    def test_existing_externally_writable_release_parent_is_rejected_without_changes(self):
        first = self.install()
        parent = Path(first["release"]).parent
        for mode in (0o775, 0o777):
            with self.subTest(mode=mode):
                parent.chmod(mode)
                before = self.legacy_record(self.destination)
                with self.assertRaisesRegex(ValueError, "external write access"):
                    self.install(force=True)
                self.assertEqual(self.legacy_record(self.destination), before)
                self.assertFalse(self.backup.exists())

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
