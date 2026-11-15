"""Native Windows storage safeguards for the review runtime installer.

Installation uses Win32 handles and ACLs rather than treating POSIX permission
bits as Windows access controls. No optional packages or link privileges are
required. Review-engine execution remains separate from this installer backend.
"""

import ctypes
import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL)]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _Acl(ctypes.Structure):
    _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                ("size", wintypes.WORD), ("count", wintypes.WORD),
                ("reserved2", wintypes.WORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_ubyte), ("flags", ctypes.c_ubyte),
                ("size", wintypes.WORD)]


# A directory may grant creation beneath an ancestor (for example C:\) while
# still protecting its existing children. Replacement and ACL changes may not
# be granted to an untrusted principal. Managed leaves also forbid content writes.
_REPLACEMENT_RIGHTS = 0x00010000 | 0x00040000 | 0x00080000 | 0x40 | 0x10 | 0x100
_WRITE_RIGHTS = _REPLACEMENT_RIGHTS | 0x2 | 0x4
_GENERIC_MUTATION = 0x10000000 | 0x40000000
_TRUSTED_SYSTEM_SIDS = {
    "S-1-5-18",  # LocalSystem.
    "S-1-5-32-544",  # Builtin Administrators.
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",  # TrustedInstaller.
}

if os.name == "nt":
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.POINTER(_SecurityAttributes), wintypes.DWORD,
                                    wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetAce.restype = wintypes.BOOL


def _sid_text(sid: int) -> str:
    """Convert an existing SID to its canonical string without retaining native memory.

    Args:
        sid (int): Pointer to an existing Windows SID.

    Returns:
        str: Canonical Windows SID string.

    Raises:
        OSError: Windows rejects the SID conversion.
    """
    value = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(value)
    finally:
        _kernel32.LocalFree(value)


@lru_cache(maxsize=1)
def _current_sid() -> str:
    """Return the process user's SID, independently of an elevated token owner.

    Returns:
        str: Current process user SID.

    Raises:
        OSError: The process token cannot be queried.
    """
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != 122 or not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(needed.value)
        if not _advapi32.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_text(ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents.sid)
    finally:
        _kernel32.CloseHandle(token)


@contextmanager
def _private_attributes() -> Iterator[_SecurityAttributes]:
    """Supply an explicit owner and protected inheritable private ACL at creation.

    Yields:
        _SecurityAttributes: Attributes valid until the context exits.

    Raises:
        OSError: Windows rejects the security descriptor.
    """
    sid = _current_sid()
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        _kernel32.LocalFree(descriptor)


def _open_handle(path: Path, *, directory: bool = False, writable: bool = False,
                 create: bool = False) -> int:
    """Open a disk object itself and reject every reparse-point redirection.

    Args:
        path (Path): Existing object or a regular file to create.
        directory (bool): Require a directory instead of a regular file.
        writable (bool): Request write access and allow competing lock handles.
        create (bool): Create a missing regular file with a private ACL.

    Returns:
        int: Owned native handle; the caller must close or transfer it.

    Raises:
        OSError: The object cannot be opened or inspected.
        ValueError: The handle refers to a reparse point or the wrong object type.
    """
    access = 0x00020080 | (0 if directory else 0x80000000)  # READ_CONTROL, attributes, data.
    if writable:
        access |= 0x40000000  # GENERIC_WRITE.
    sharing = 0x1 | (0x2 if writable else 0x4)  # READ with WRITE or DELETE.
    if directory:
        sharing |= 0x2  # Metadata validation must coexist with existing directory writers.
    with _private_attributes() if create else nullcontext() as attributes:
        handle = _kernel32.CreateFileW(str(path), access, sharing,
                                       ctypes.byref(attributes) if attributes is not None else None,
                                       4 if create else 3, 0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _AttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (_kernel32.GetFileType(handle) != 1 or info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory):
            raise ValueError("runtime storage must be a nonredirected regular file or directory")
        return handle
    except BaseException:
        _kernel32.CloseHandle(handle)
        raise


def _validate_handle(handle: int, *, managed: bool) -> None:
    """Validate ownership and effective granting ACEs on an already opened object.

    Args:
        handle (int): No-follow handle with READ_CONTROL access.
        managed (bool): Require the current owner and forbid all external writes.

    Raises:
        OSError: Ownership or access-control metadata cannot be read.
        ValueError: Ownership or ACLs permit untrusted mutation or replacement.
    """
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = _advapi32.GetSecurityInfo(handle, 1, 0x1 | 0x4, ctypes.byref(owner), None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        current = _current_sid()
        trusted = _TRUSTED_SYSTEM_SIDS | {current}
        if not owner.value or _sid_text(owner) not in ({current} if managed else trusted):
            raise ValueError("runtime storage must have a trusted Windows owner")
        if not dacl.value:
            raise ValueError("runtime storage must have a restricting Windows DACL")
        rights = (_WRITE_RIGHTS if managed else _REPLACEMENT_RIGHTS) | _GENERIC_MUTATION
        for index in range(ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents.count):
            address = ctypes.c_void_p()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(address)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(address, ctypes.POINTER(_AceHeader)).contents
            if header.flags & 0x8 or header.kind in (1, 6, 10, 12):
                continue  # INHERIT_ONLY or deny ACEs cannot grant current-object access.
            if header.kind != 0 or header.size < 12:
                raise ValueError("unsupported granting Windows ACL entry in runtime storage")
            mask = ctypes.cast(address.value + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
            trustee = _sid_text(address.value + 8)
            if mask & rights and trustee not in trusted:
                raise ValueError("runtime storage permits external Windows mutation or replacement "
                                 f"(SID {trustee}, ACE {index}, mask 0x{mask:08x}, "
                                 f"unsafe rights 0x{mask & rights:08x}, flags 0x{header.flags:02x})")
    finally:
        _kernel32.LocalFree(descriptor)


def open_regular(path: Path, *, writable: bool = False, create: bool = False) -> BinaryIO:
    """Open a regular file without following a final reparse point.

    Args:
        path (Path): Expected regular source, receipt, pointer or lock file.
        writable (bool): Open for binary reading and writing.
        create (bool): Create a missing file atomically with a private ACL.

    Returns:
        BinaryIO: Owned stream that closes its native handle on exit.

    Raises:
        OSError: Opening, inspecting or wrapping the file fails.
        ValueError: The path is redirected or does not name a regular disk file.
    """
    handle = _open_handle(path, writable=writable, create=create)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY))
    except BaseException:
        _kernel32.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "r+b" if writable else "rb")
    except BaseException:
        os.close(descriptor)
        raise


def validate_storage_ancestors(path: Path, *, include_self: bool = False) -> None:
    """Require trusted nonredirected ancestors that prevent external replacement.

    Args:
        path (Path): Absolute managed path or existing creation parent.
        include_self (bool): Also validate the existing creation parent itself.

    Raises:
        OSError: An ancestor cannot be opened or its security metadata read.
        ValueError: An ancestor is redirected, untrusted or externally replaceable.
    """
    for ancestor in (path, *path.parents) if include_self else path.parents:
        handle = _open_handle(ancestor, directory=True)
        try:
            _validate_handle(handle, managed=False)
        except ValueError as error:
            raise ValueError(f"runtime storage ancestor {ancestor}: {error}") from error
        finally:
            _kernel32.CloseHandle(handle)


def owned_directory(path: Path) -> None:
    """Require current-user owned private storage beneath trusted ancestors.

    Args:
        path (Path): Absolute existing directory managed by the installer.

    Raises:
        OSError: The directory or its ACL cannot be read.
        ValueError: Ownership, redirection, permissions or ancestor trust is unsafe.
    """
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("runtime storage must be absolute and nonredirected")
    handle = _open_handle(path, directory=True)
    try:
        _validate_handle(handle, managed=True)
    finally:
        _kernel32.CloseHandle(handle)
    validate_storage_ancestors(path)


def make_owned_directory(path: Path) -> None:
    """Create each missing directory with a protected inheritable private DACL.

    Args:
        path (Path): Absolute managed directory to create or validate.

    Raises:
        OSError: A directory cannot be created or inspected.
        ValueError: Existing storage is redirected, foreign-owned or unsafe.
    """
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("runtime storage must be absolute and nonredirected")
    missing = []
    existing = path
    while not existing.exists():
        missing.append(existing)
        existing = existing.parent
    if not missing:
        owned_directory(existing)
        return
    validate_storage_ancestors(existing, include_self=True)
    for directory in reversed(missing):
        with _private_attributes() as attributes:
            if not _kernel32.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                error = ctypes.get_last_error()
                if error != 183:  # A racing creator still has to pass ownership/ACL checks.
                    raise ctypes.WinError(error)
        owned_directory(directory)


@contextmanager
def install_lock(path: Path) -> Iterator[BinaryIO]:
    """Hold one nonblocking Windows byte-range lock on a protected regular file.

    Args:
        path (Path): Lock file beneath already validated managed storage.

    Yields:
        BinaryIO: Locked stream, valid until the context exits.

    Raises:
        BlockingIOError: Another installer owns the byte-range lock.
        OSError: The lock file cannot be opened, validated, locked or unlocked.
        ValueError: The lock file is redirected, foreign-owned or externally writable.
    """
    with open_regular(path, writable=True, create=True) as stream:
        _validate_handle(msvcrt.get_osfhandle(stream.fileno()), managed=True)
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(errno.EWOULDBLOCK, "another runtime installer holds the lock") from error
            raise
        try:
            yield stream
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def remove_retired_directory(path: Path) -> None:
    """Remove a private retired tree without traversing junctions or symlinks.

    Args:
        path (Path): Retired local scripts directory after verified backup/activation.

    Raises:
        OSError: Traversal, read-only attribute repair or removal fails.
        ValueError: The root or a file requiring attribute repair is unsafe.
    """
    owned_directory(path)

    def repair_readonly(function, failed, error):
        if not isinstance(error[1], PermissionError):
            raise error[1]
        candidate = Path(failed)
        # chmod on Windows follows reparse points; refuse them before repairing.
        handle = _open_handle(candidate, directory=candidate.is_dir())
        try:
            _validate_handle(handle, managed=True)
        finally:
            _kernel32.CloseHandle(handle)
        candidate.chmod(stat.S_IWRITE)
        function(failed)

    shutil.rmtree(path, onerror=repair_readonly)
