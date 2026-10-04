"""Installer regressions with local fake sources and no dependency builds."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent


class CodegraphInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stack-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.stack = self.root / "stack"
        self.stack.mkdir()
        shutil.copy2(REPO / "stack/install.sh", self.stack / "install.sh")
        shutil.copytree(REPO / "stack/patches/codegraph", self.stack / "patches/codegraph")
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.install = self.home / ".codegraph"
        self.launcher = self.home / ".local/bin/codegraph"
        self.launcher.parent.mkdir(parents=True)
        self.env = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"), "TMPDIR": str(self.root),
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1", "TEST_BUNDLE": "first",
        }
        self.mock("python3", f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
        self.mock("node", "#!/bin/sh\nprintf '24\\n'\n")
        self.mock("npm", "#!/bin/sh\nexit 0\n")
        self.mock("cargo", "#!/bin/sh\nexit 0\n")
        self.mock("uname", "#!/bin/sh\ncase $1 in -s) echo Darwin;; -m) echo arm64;; esac\n")
        source_builder = self.root / "source.py"
        source_builder.write_text('''import io, os, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
(root / "scripts").mkdir(parents=True)
(root / "release").mkdir()
(root / "scripts/build-kernel.sh").write_text("#!/bin/sh\\nexit 0\\n")
(root / "scripts/build-bundle.sh").write_text("#!/bin/sh\\npython3 '" + __file__ + "' --bundle \\\"$1\\\"\\n")
'''.replace('root = pathlib.Path(sys.argv[1])', '''if sys.argv[1] == "--bundle":
    output = pathlib.Path("release/codegraph-" + sys.argv[2] + ".tar.gz")
    if os.environ.get("TEST_BAD_ARCHIVE") == "1":
        output.write_text("not a tar archive")
    else:
        body = ("#!/bin/sh\\necho " + os.environ["TEST_BUNDLE"] + "\\n").encode()
        with tarfile.open(output, "w:gz") as archive:
            entry = tarfile.TarInfo("codegraph/bin/codegraph")
            entry.size = len(body)
            entry.mode = 0o755
            archive.addfile(entry, io.BytesIO(body))
    sys.exit(0)
root = pathlib.Path(sys.argv[1])'''))
        git = f'''#!/bin/bash
if [[ $1 == -c ]]; then exec python3 '{source_builder}' "${{@: -1}}"; fi
if [[ $1 == hash-object ]]; then exec python3 -c 'import hashlib,sys; print(hashlib.sha1(sys.stdin.buffer.read()).hexdigest())'; fi
if [[ ${{3:-}} == rev-parse ]]; then echo 6560052a6f856855d3f71eee838fd66ccfa4285d; fi
exit 0
'''
        self.mock("git", git)

    def mock(self, name, contents):
        target = self.bin / name
        target.write_text(contents)
        target.chmod(0o755)

    def run_install(self, **extra):
        return subprocess.run(["/bin/bash", str(self.stack / "install.sh"), "codegraph"],
                              cwd=self.root, env=dict(self.env, **extra), text=True,
                              capture_output=True, timeout=20)

    def seed_legacy_install(self):
        old = self.install / "versions/v1.6.2-zetta-delta"
        (old / "bin").mkdir(parents=True)
        (old / "bin/codegraph").write_text("#!/bin/sh\necho previous\n")
        (old / "bin/codegraph").chmod(0o755)
        (self.install / "current").symlink_to(old)
        self.launcher.symlink_to(old / "bin/codegraph")
        return old

    def test_failed_reinstall_preserves_active_bundle_and_links(self):
        old = self.seed_legacy_install()
        before = (old / "bin/codegraph").read_bytes()
        result = self.run_install(TEST_BAD_ARCHIVE="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((old / "bin/codegraph").exists(), "failed reinstall removed active executable")
        self.assertEqual((old / "bin/codegraph").read_bytes(), before)
        self.assertEqual((self.install / "current").resolve(), old)
        self.assertEqual(self.launcher.resolve(), old / "bin/codegraph")

    def test_successful_patch_revision_retains_previous_build(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        previous = (self.install / "current").resolve()
        original = (previous / "bin/codegraph").read_bytes()
        patch = self.stack / "patches/codegraph/0002-feat-Add-Elixir-extraction-and-resolution.patch"
        patch.write_bytes(patch.read_bytes() + b"\n# fixture patch revision\n")
        second = self.run_install(TEST_BUNDLE="second")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        current = (self.install / "current").resolve()
        self.assertNotEqual(current, previous, "patch revision reused the active build directory")
        self.assertEqual((previous / "bin/codegraph").read_bytes(), original)
        self.assertIn(b"second", (current / "bin/codegraph").read_bytes())
        self.assertEqual(self.launcher.resolve(), current / "bin/codegraph")

    def test_same_revision_reinstall_retains_rollback_build(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        previous = (self.install / "current").resolve()
        second = self.run_install(TEST_BUNDLE="second")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertNotEqual((self.install / "current").resolve(), previous)
        self.assertIn(b"first", (previous / "bin/codegraph").read_bytes())


if __name__ == "__main__":
    unittest.main()
