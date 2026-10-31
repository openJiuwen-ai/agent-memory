# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scope-bound local media admission and handle-based staging for video imports."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.type_def.scope import Scope

_DIMS = ("org", "space", "user", "agent", "session")
_DENIED = "local media is outside the configured import scope"


class LocalMediaImports:
    """Immutable server configuration; omitted Scope dimensions mean empty, not wildcard."""

    def __init__(self, bindings: object = None) -> None:
        self._roots: dict[tuple[str, ...], tuple[Path, ...]] = {}
        if bindings is None:
            return
        if not isinstance(bindings, list):
            raise ValidationError("local_media_roots must be a list of scope/root bindings")
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != {"scope", "root"}:
                raise ValidationError("local_media_roots entries require only scope and root")
            scope = binding["scope"]
            if not isinstance(scope, Mapping):
                raise ValidationError(
                    "local media scope requires a nonempty org and string dimensions"
                )
            if (
                set(scope) - set(_DIMS)
                or any(not isinstance(value, str) for value in scope.values())
                or not scope.get("org")
            ):
                raise ValidationError(
                    "local media scope requires a nonempty org and string dimensions"
                )
            root_value = binding["root"]
            if not isinstance(root_value, str) or not root_value.strip():
                raise ValidationError("local media root must be a nonempty absolute directory")
            root = Path(root_value).expanduser()
            if not root.is_absolute() or str(root).startswith(("\\\\", "//")):
                raise ValidationError("local media root must be an absolute local directory")
            try:
                root = root.resolve(strict=True)
                if not root.is_dir() or root.parent == root or str(root).startswith(("\\\\", "//")):
                    raise ValueError("invalid import root")
            except (OSError, ValueError, RuntimeError):
                raise ValidationError(
                    "local media root must be an existing non-volume directory"
                ) from None
            key = tuple(scope.get(dim, "") for dim in _DIMS)
            self._roots[key] = (*self._roots.get(key, ()), root)

    def stage(self, uri: str, scope: Scope, run_root: Path) -> Path:
        """Copy an admitted, safely opened file; downstream tools never reopen the input path."""
        roots = self._roots.get(tuple(getattr(scope, dim) for dim in _DIMS), ())
        if not roots:
            raise PermissionDeniedError(_DENIED)
        path = _local_path(uri)
        try:
            resolved = path.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in roots):
                raise PermissionDeniedError(_DENIED)
            # Preserve the basename for pipeline identities; its parent is always task-private.
            input_root = run_root / "input"
            staged = input_root / resolved.name
            with _open_local_file(resolved) as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise PermissionDeniedError(_DENIED)
                input_root.mkdir()
                with staged.open("xb") as target:
                    shutil.copyfileobj(source, target)
            return staged
        except (OSError, ValueError, RuntimeError):
            # Do not reveal whether a non-admitted server path exists or how it is laid out.
            raise PermissionDeniedError(_DENIED) from None


def _local_path(uri: str) -> Path:
    if not isinstance(uri, str) or not uri.strip() or "\x00" in uri:
        raise ValidationError("video requires a local media path or file URI")
    if os.name == "nt" and Path(uri).is_absolute() and not uri.startswith(("\\\\", "//")):
        path = Path(uri)
    else:
        parsed = urlparse(uri)
        if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
            raise ValidationError("video imports require a local path or local file URI")
        if parsed.query or parsed.fragment:
            raise ValidationError("video file URI must not contain a query or fragment")
        value = unquote(parsed.path) if parsed.scheme else uri
        if os.name == "nt" and len(value) >= 3:
            if value[0] == "/" and value[2] == ":":
                value = value[1:]
        path = Path(value).expanduser()
    if ".." in path.parts or str(path).startswith(("\\\\", "//")):
        raise PermissionDeniedError(_DENIED)
    return path


@contextmanager
def _open_descriptor(path: str, flags: int, *, dir_fd: int | None = None) -> Iterator[int]:
    # These are read-only opens; mode is explicit and does not alter existing permissions.
    descriptor = os.open(path, flags, mode=0o600, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_local_file(path: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        with _open_windows_file(path) as source:
            yield source
        return
    # Walk from the filesystem anchor, pinning every directory by descriptor. A replaced
    # ancestor or leaf cannot redirect a later open through a symlink.
    with ExitStack() as descriptors:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = descriptors.enter_context(_open_descriptor(path.anchor, directory_flags))
        for component in path.parts[1:-1]:
            directory = descriptors.enter_context(
                _open_descriptor(component, directory_flags, dir_fd=directory)
            )
        descriptor = descriptors.enter_context(
            _open_descriptor(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        )
        # ExitStack owns the descriptor even if creating its Python wrapper fails.
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            yield source


@contextmanager
def _open_windows_file(path: Path) -> Iterator[BinaryIO]:
    """Pin ancestors without delete sharing, and reject all reparse points by handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    info.restype = wintypes.BOOL
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    final_path.restype = wintypes.DWORD

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    def open_handle(candidate: Path, *, directory: bool):
        # OPEN_EXISTING, OPEN_REPARSE_POINT and BACKUP_SEMANTICS; no FILE_SHARE_DELETE.
        access = 0x80 if directory else 0x80000000
        share = 0x3 if directory else 0x1
        handle = create(str(candidate), access, share, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot open local media")
        attributes = AttributeTag()
        if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            close(handle)
            raise OSError(ctypes.get_last_error(), "cannot inspect local media")
        if attributes.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            close(handle)
            raise PermissionDeniedError(_DENIED)
        return handle

    with ExitStack() as stack:
        for parent in reversed(path.parents):
            handle = open_handle(parent, directory=True)
            stack.callback(close, handle)
        handle = open_handle(path, directory=False)
        name = ctypes.create_unicode_buffer(32768)
        size = final_path(handle, name, len(name), 0)
        actual = name.value.removeprefix("\\\\?\\")
        if not size or size >= len(name) or Path(actual) != path:
            close(handle)
            raise PermissionDeniedError(_DENIED)
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except OSError:
            close(handle)
            raise
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                yield source
        finally:
            os.close(descriptor)
