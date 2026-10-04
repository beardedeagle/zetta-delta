#!/usr/bin/env python3
"""Detect replacement of the reviewed local zvec-grep package after an update.

The index maintainer runs this before every zg build and refuses zg when it
fails. stack/install.sh pins it with --pin after installing zvec-grep.

Usage:
    verify-install.py              check the zg on PATH against the pin
    verify-install.py PACKAGE      check the package folder PACKAGE
    verify-install.py --pin PACKAGE  pin PACKAGE as the reviewed build
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile


def artifact_hashes(root: Path) -> dict[str, str]:
    """Hash the exact dist artifact set without following paths outside it."""
    dist = root / "dist"
    files = {}
    for path in sorted(dist.rglob("*")):
        resolved = path.resolve()
        if not resolved.is_relative_to(dist):
            raise ValueError("reviewed artifact is outside the package")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("the package has no dist/ files")
    return files


def package_entrypoint(package: dict, root: Path) -> str:
    """Return the zg entrypoint whose bytes are included in the pin."""
    binary = package.get("bin")
    relative = binary.get("zg") if isinstance(binary, dict) else binary
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("the package must define a relative zg entrypoint")
    path = root / relative
    if not path.resolve().is_relative_to(root / "dist") or not path.is_file():
        raise ValueError("the zg entrypoint is not a dist artifact")
    return path.relative_to(root).as_posix()


def verify() -> None:
    manifest = json.loads(Path(__file__).with_name("local-install.json").read_text())
    executable = shutil.which("zg")
    if len(sys.argv) > 2 or (len(sys.argv) == 1 and not executable):
        raise ValueError("expected an installed zg or one candidate package path")
    if len(sys.argv) == 2:
        root = Path(sys.argv[1]).resolve()
    else:
        root = next(parent for parent in Path(executable).resolve().parents
                    if (parent / "package.json").is_file())
    if manifest.get("schema") != 2:
        raise ValueError("the installation needs a current reviewed pin")
    package = json.loads((root / "package.json").read_text())
    if package.get("name") != "@zvec/zvec-grep" or package.get("version") != manifest["version"]:
        raise ValueError("installed package version differs from the reviewed local build")
    if hashlib.sha256((root / "package.json").read_bytes()).hexdigest() != manifest["package_sha256"]:
        raise ValueError("reviewed package metadata differs")
    entrypoint = package_entrypoint(package, root)
    if entrypoint != manifest["entrypoint"] or artifact_hashes(root) != manifest["files"]:
        raise ValueError("reviewed artifact set or checksum differs")
    if len(sys.argv) == 1 and Path(executable).resolve() != (root / entrypoint).resolve():
        raise ValueError("zg is not the reviewed package entrypoint")
    print("zg local package verification passed")


def pin(root: Path) -> None:
    """Record ROOT's version and every file under its dist/ as the reviewed build.

    Writes local-install.json next to this script, replacing it in one step.

    Args:
        root (Path): the installed zvec-grep package folder.

    Raises:
        ValueError: ROOT is not a zvec-grep package, or has no dist/ files.
        OSError: a file could not be read, or the manifest written.
    """
    root = root.resolve()
    package = json.loads((root / "package.json").read_text())
    if package.get("name") != "@zvec/zvec-grep" or not package.get("version"):
        raise ValueError("not a zvec-grep package")
    files = artifact_hashes(root)
    entrypoint = package_entrypoint(package, root)
    if entrypoint not in files:
        raise ValueError("the entrypoint must be a reviewed artifact")
    manifest = Path(__file__).with_name("local-install.json")
    contents = {"schema": 2, "version": package["version"], "entrypoint": entrypoint,
                "package_sha256": hashlib.sha256((root / "package.json").read_bytes()).hexdigest(), "files": files}
    with tempfile.NamedTemporaryFile(mode="w", dir=manifest.parent, prefix="local-install-", delete=False) as f:
        partial = Path(f.name)
        f.write(json.dumps(contents, indent=2) + "\n")
    try:
        partial.replace(manifest)
    finally:
        partial.unlink(missing_ok=True)
    print(f"zg local package pinned: {package['version']}, {len(files)} files")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--pin":
        try:
            pin(Path(sys.argv[2]))
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"zg local package pin failed: {error}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    try:
        verify()
    except (OSError, ValueError, KeyError, IndexError, TypeError, StopIteration):
        print("zg local package verification failed; reinstall the reviewed local package before indexing", file=sys.stderr)
        sys.exit(1)
