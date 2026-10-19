"""
File-manager backend: the path jail and every filesystem operation the NAS
feature performs on behalf of a browser.

Everything here takes untrusted paths from HTTP handlers, so the module has
one security primitive and every operation goes through it: resolve_jailed()
collapses the path with realpath (following EVERY symlink, including the final
component) and then requires the result to sit under one of the allowed roots
— the mountpoints of live, NAS-managed mounts, provided by nas_manager. A
symlink pointing out of a mount therefore resolves out of the jail and is
rejected before any syscall touches it; ".." games die the same way. For the
read/write paths that reopen a name after the check, _open_nofollow() opens
the final component with O_NOFOLLOW relative to a directory fd, so a symlink
swapped in between check and open yields ELOOP instead of a jail escape
(TOCTOU).

Uploads never touch /tmp — on this platform /tmp is RAM. write_stream()
stages to a ".pcat-upload-*.part" file in the destination directory (same
filesystem), so the final os.replace() is atomic and an interrupted upload
leaves at most one .part file, which list_dir() hides and the next upload of
the same name overwrites the slot cleanly.

Folder download is a zip streamed with ZIP_STORED: stdlib zipfile writes to
unseekable streams (data-descriptor mode) since 3.6, STORED costs no CPU on
the router, and every client OS opens zip natively. If on-device testing ever
falsifies the streaming assumption the fallback is busybox `tar -czf -`.

This module deliberately has zero UCI/subprocess/socketio coupling (the one
subprocess user, archive streaming, takes a plain generator consumer) so the
whole surface is unit-testable against a tempdir.
"""

import errno
import os
import secrets
import stat
import zipfile

# Callers (routes) catch this one exception type for every "path refused"
# case and map it to a 200 {"status":"failed","code":"bad_path"} response.
class PathJailError(Exception):
    pass


class FileOpError(Exception):
    """Operation-level failure with a machine code the UI branches on."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_MAX_PATH = 4096
_PART_PREFIX = ".pcat-upload-"
_UPLOAD_CHUNK = 256 * 1024
# Free-space guard: require the declared size plus slack, or this floor when
# the size is unknown, so an upload cannot run a shared volume to zero.
_FREE_SPACE_FLOOR = 64 * 1024 * 1024
_FREE_SPACE_SLACK = 1.05

# A delete bigger than this is refused inline; the route starts a background
# job instead so the HTTP request doesn't hang on a huge tree.
INLINE_DELETE_LIMIT = 200


# ── the jail ─────────────────────────────────────────────────────────────────

def resolve_jailed(user_path, roots):
    """Resolve an untrusted path and require it to live under one of `roots`
    (absolute, already-canonical mountpoints). Returns the canonical path.
    Raises PathJailError otherwise. The empty roots list rejects everything —
    a device with no managed mounts has no filesystem surface at all."""
    if not isinstance(user_path, str):
        raise PathJailError("path must be a string")
    if "\x00" in user_path:
        raise PathJailError("NUL in path")
    if len(user_path) > _MAX_PATH:
        raise PathJailError("path too long")
    resolved = os.path.realpath(os.path.join("/", user_path))
    for root in roots or ():
        if resolved == root or resolved.startswith(root + "/"):
            return resolved
    raise PathJailError("path outside managed storage")


def _open_nofollow(dirpath, name, flags, mode=0o644):
    """Open `name` inside already-jailed `dirpath` without following a
    symlink final component (TOCTOU guard for the check-then-open paths)."""
    dfd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dfd)
    finally:
        os.close(dfd)


def valid_name(name):
    """One path component as accepted from the browser for create/rename."""
    return (isinstance(name, str) and 0 < len(name) <= 255
            and "/" not in name and "\x00" not in name
            and name not in (".", "..")
            and not name.startswith(_PART_PREFIX))


# ── listing / stat ───────────────────────────────────────────────────────────

def list_dir(path, offset=0, limit=1000):
    """Entries of a jailed directory. Symlinks are listed but flagged and
    never followed (their size/mtime come from lstat of the link itself)."""
    entries = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith(_PART_PREFIX):
                    continue  # in-flight upload staging
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # vanished mid-scan
                entries.append({
                    "name": entry.name,
                    "type": "dir" if stat.S_ISDIR(st.st_mode) else "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "symlink": stat.S_ISLNK(st.st_mode),
                })
    except NotADirectoryError:
        raise FileOpError("bad_path", "not a directory")
    except FileNotFoundError:
        raise FileOpError("bad_path", "no such directory")
    except PermissionError:
        raise FileOpError("io_error", "permission denied")
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    total = len(entries)
    window = entries[offset:offset + limit]
    return {"entries": window, "total": total,
            "truncated": offset + len(window) < total}


def volume_of(path):
    """{mountpoint free/total} for the filesystem holding a jailed path."""
    vfs = os.statvfs(path)
    return {"free_bytes": vfs.f_bavail * vfs.f_frsize,
            "total_bytes": vfs.f_blocks * vfs.f_frsize}


# ── simple mutations ─────────────────────────────────────────────────────────

def make_dir(parent, name):
    if not valid_name(name):
        raise FileOpError("bad_path", "invalid folder name")
    try:
        os.mkdir(os.path.join(parent, name))
    except FileExistsError:
        raise FileOpError("exists", "already exists")
    except OSError as e:
        raise FileOpError("io_error", str(e))


def rename(path, new_name):
    """Rename within the same directory only — the browser's inline-rename.
    Moves between directories go through the move/copy API instead."""
    if not valid_name(new_name):
        raise FileOpError("bad_path", "invalid name")
    dst = os.path.join(os.path.dirname(path), new_name)
    if os.path.lexists(dst):
        raise FileOpError("exists", "target name already exists")
    try:
        os.rename(path, dst)
    except OSError as e:
        raise FileOpError("io_error", str(e))
    return dst


def same_filesystem(a, b):
    return os.lstat(a).st_dev == os.lstat(b).st_dev


def move_entry(src, dest_dir):
    """Move src into dest_dir on the same filesystem (instant rename).
    Cross-filesystem moves are the caller's job to run as a copy+delete job.
    Returns the new path."""
    dst = os.path.join(dest_dir, os.path.basename(src.rstrip("/")))
    if os.path.lexists(dst):
        raise FileOpError("exists", "target already exists")
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise FileOpError("cross_device", "different filesystem")
        raise FileOpError("io_error", str(e))
    return dst


def _iter_tree_files(path):
    """(abs_path, rel_path) for every regular file under path (or the file
    itself); symlinks never followed."""
    st = os.lstat(path)
    if stat.S_ISREG(st.st_mode):
        yield path, os.path.basename(path)
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    base = os.path.basename(path.rstrip("/"))
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        rel_dir = os.path.relpath(dirpath, path)
        for f in filenames:
            full = os.path.join(dirpath, f)
            try:
                if stat.S_ISREG(os.lstat(full).st_mode):
                    rel = f if rel_dir == "." else os.path.join(rel_dir, f)
                    yield full, os.path.join(base, rel)
            except OSError:
                continue


def copy_tree_runner(handle, sources, dest_dir, delete_src=False,
                     conflict="rename"):
    """Job runner: copy each source (file or tree) into dest_dir with
    byte-accurate progress; optionally delete sources after (cross-device
    move). `handle` is a nas_jobs.JobHandle."""
    files = []
    total = 0
    for src in sources:
        for full, rel in _iter_tree_files(src):
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            files.append((full, rel, size))
            total += size
    handle.set_progress(pct=0, bytes_total=total, items_total=len(files),
                        message="copying")
    done_bytes = 0
    for idx, (full, rel, size) in enumerate(files):
        handle.check_cancel()
        target = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(full, "rb") as src_f:
            write_stream(
                os.path.dirname(target), os.path.basename(target), src_f,
                max_bytes=size + 1, declared_size=size, conflict=conflict)
        done_bytes += size
        handle.set_progress(
            pct=int(done_bytes * 100 / total) if total else 100,
            bytes_done=done_bytes, items_done=idx + 1,
            message=os.path.basename(full))
    if delete_src:
        handle.set_progress(message="removing originals")
        for src in sources:
            handle.check_cancel()
            delete_tree(src)


def tree_measure(path, limit=INLINE_DELETE_LIMIT):
    """(items, bytes) of a tree, stopping early once items exceeds `limit`
    (the caller only needs to know "small enough to do inline")."""
    items, size = 0, 0
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return 1, st.st_size
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        items += len(dirnames) + len(filenames)
        for f in filenames:
            try:
                size += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
        if items > limit:
            break
    return items, size


def delete_tree(path, progress_cb=None):
    """Remove a file or tree bottom-up without following symlinks. Used both
    inline (small trees) and by the delete job runner (progress_cb)."""
    removed = 0

    def _tick():
        nonlocal removed
        removed += 1
        if progress_cb and removed % 50 == 0:
            progress_cb(removed)

    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        _tick()
        return removed
    for dirpath, dirnames, filenames in os.walk(path, topdown=False,
                                                followlinks=False):
        for f in filenames:
            try:
                os.unlink(os.path.join(dirpath, f))
                _tick()
            except OSError:
                pass
        for d in dirnames:
            # A symlink to a directory shows up in dirnames; it must be
            # unlinked (removing the link), never rmdir'd or descended into.
            target = os.path.join(dirpath, d)
            try:
                if stat.S_ISLNK(os.lstat(target).st_mode):
                    os.unlink(target)
                else:
                    os.rmdir(target)
                _tick()
            except OSError:
                pass
    os.rmdir(path)
    _tick()
    return removed


# ── upload ───────────────────────────────────────────────────────────────────

def safe_relpath(relpath):
    """Validate the client-supplied relative path (may carry sub-folders from
    a folder upload). Returns the normalized list of components."""
    if not isinstance(relpath, str) or not relpath or "\x00" in relpath:
        raise PathJailError("bad relpath")
    if relpath.startswith("/") or len(relpath) > _MAX_PATH:
        raise PathJailError("bad relpath")
    parts = [p for p in relpath.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PathJailError("bad relpath")
    if not valid_name(parts[-1]):
        raise PathJailError("bad filename")
    return parts


def dedup_name(directory, name):
    """'a.jpg' -> 'a (1).jpg' (first free slot) for conflict:"rename"."""
    base, ext = os.path.splitext(name)
    for i in range(1, 1000):
        candidate = "{} ({}){}".format(base, i, ext)
        if not os.path.lexists(os.path.join(directory, candidate)):
            return candidate
    raise FileOpError("exists", "too many name collisions")


def write_stream(directory, name, stream, max_bytes, declared_size=None,
                 conflict="fail"):
    """Stream an upload into `directory` (already jailed) as `name`.

    Stages to a .part file on the same filesystem and os.replace()s into
    place, so the destination name either doesn't change or becomes the
    complete new file — never a truncated one. Returns
    {"path": final_path, "size": n, "renamed_to": new_name|None}.
    """
    if not valid_name(name):
        raise FileOpError("bad_path", "invalid filename")

    vfs = os.statvfs(directory)
    free = vfs.f_bavail * vfs.f_frsize
    need = (max(_FREE_SPACE_FLOOR, int(declared_size * _FREE_SPACE_SLACK))
            if declared_size else _FREE_SPACE_FLOOR)
    if free < need:
        raise FileOpError("no_space", "not enough free space on volume")

    renamed_to = None
    final_name = name
    target = os.path.join(directory, name)
    if os.path.lexists(target):
        if conflict == "fail":
            raise FileOpError("exists", "file already exists")
        if conflict == "rename":
            final_name = dedup_name(directory, name)
            renamed_to = final_name
        # conflict == "overwrite": os.replace below swaps it atomically

    part_name = _PART_PREFIX + secrets.token_hex(8) + ".part"
    part_path = os.path.join(directory, part_name)
    written = 0
    fd = _open_nofollow(directory, part_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise FileOpError("too_large", "upload exceeds size limit")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part_path, os.path.join(directory, final_name))
    except BaseException:
        try:
            os.unlink(part_path)
        except OSError:
            pass
        raise
    return {"path": os.path.join(directory, final_name), "size": written,
            "renamed_to": renamed_to}


# ── download ─────────────────────────────────────────────────────────────────

class _StreamBuffer:
    """Minimal unseekable write sink zipfile can drive; hands finished chunks
    to the HTTP response generator."""

    def __init__(self):
        self._chunks = []
        self._pos = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def seekable(self):
        return False

    def drain(self):
        chunks, self._chunks = self._chunks, []
        return chunks


def _zip_add_file(zf, buf, full, arcname):
    """Copy one file into the open zip in chunks, yielding zip bytes as they
    are produced — zf.write() would buffer the whole file in RAM first."""
    zinfo = zipfile.ZipInfo.from_file(full, arcname)
    zinfo.compress_type = zipfile.ZIP_STORED
    with open(full, "rb") as src, zf.open(zinfo, "w") as dest:
        while True:
            chunk = src.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            dest.write(chunk)
            yield from iter(buf.drain())


def zip_stream(paths):
    """Generator of zip bytes for one or more jailed paths (files or trees).
    ZIP_STORED: no CPU spent compressing; allowZip64 for >4 GiB content.
    Symlinks are skipped — following them could tour the whole filesystem."""
    buf = _StreamBuffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path in paths:
            base = os.path.basename(path.rstrip("/")) or "root"
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode):
                yield from _zip_add_file(zf, buf, path, base)
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            for dirpath, dirnames, filenames in os.walk(path,
                                                        followlinks=False):
                rel = os.path.relpath(dirpath, path)
                arc_dir = base if rel == "." else os.path.join(base, rel)
                if not filenames and not dirnames:
                    zf.writestr(arc_dir + "/", b"")
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    try:
                        if not stat.S_ISREG(os.lstat(full).st_mode):
                            continue
                    except OSError:
                        continue
                    try:
                        yield from _zip_add_file(
                            zf, buf, full, os.path.join(arc_dir, f))
                    except OSError:
                        continue  # vanished or unreadable: skip, keep going
                yield from iter(buf.drain())
    yield from iter(buf.drain())


def open_for_read(path):
    """(fd, size, mtime) with O_NOFOLLOW on the final component; the route
    wraps the fd in a file object for a range-capable streamed response."""
    directory, name = os.path.split(path)
    try:
        fd = _open_nofollow(directory, name, os.O_RDONLY)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FileOpError("bad_path", "refusing to follow symlink")
        raise FileOpError("io_error", str(e))
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise FileOpError("bad_path", "not a regular file")
    return fd, st.st_size, int(st.st_mtime)
