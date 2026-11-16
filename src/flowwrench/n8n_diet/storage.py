"""SQLite safety boundary: shared source locks and atomic private-copy output."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import re
import sqlite3
import stat
import tempfile


class DietError(Exception):
    """An actionable input, schema, or safety error (CLI exit 2)."""


def fingerprint_fd(fd):
    """Hash an already-open fd without disturbing its offset or POSIX locks."""
    digest = hashlib.sha256()
    size = 0
    while chunk := os.pread(fd, 1048576, size):
        digest.update(chunk)
        size += len(chunk)
    return {'bytes': size, 'sha256': digest.hexdigest()}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def connect(path, *, immutable=False):
    if sqlite3.sqlite_version_info < (3, 31, 0):
        raise DietError('SQLite >=3.31.0 is required (VACUUM INTO and trusted_schema)')
    uri = Path(path).as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
    conn = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA temp_store=MEMORY')
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def source_database(path, *, force=False, fingerprint=None):
    """Never open the original through SQLite's writable/WAL machinery.

    Linux POSIX read locks cover SQLite's pending, reserved and shared lock bytes.
    This excludes cooperative SQLite writers and checkpoints, while allowing readers.
    --force admits a committed WAL, never a lock conflict. WAL is replayed privately.
    """
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise DietError('source must be a regular file, not a symlink')
    source = supplied.resolve(strict=True)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    shm_fd = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DietError('source must be a regular SQLite file')
        if os.read(fd, 16) != b'SQLite format 3\x00':
            raise DietError('not a SQLite file (expected SQLite 3 header)')
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 512, 0x40000000)
        except OSError as exc:
            raise DietError('database is locked; stop n8n/writers and retry; --force never bypasses locks') from exc
        if os.stat(source).st_ino != info.st_ino or os.stat(source).st_dev != info.st_dev:
            raise DietError('source path changed while opening')
        # WAL writers coordinate on the shm lock region, not the DB reserved byte.
        # Lock all eight WAL-index bytes without creating/modifying the shm file.
        shm = Path(str(source) + '-shm')
        if shm.exists() or shm.is_symlink():
            if shm.is_symlink():
                raise DietError('SHM symlinks are unsupported')
            shm_fd = os.open(shm, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(shm_fd).st_mode):
                raise DietError('SHM must be a regular file')
            try:
                fcntl.lockf(shm_fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 8, 120)
            except OSError as exc:
                raise DietError('WAL database is locked; stop writers; --force never bypasses locks') from exc
        journal = Path(str(source) + '-journal')
        if journal.is_symlink():
            raise DietError('rollback journal symlinks are unsupported')
        if journal.exists() and (not journal.is_file() or journal.stat().st_size):
            raise DietError('non-empty rollback journal; recover using SQLite/n8n on a separate backup first')
        wal = Path(str(source) + '-wal')
        if wal.is_symlink():
            raise DietError('WAL symlinks are unsupported')
        if wal.exists() and not wal.is_file():
            raise DietError('WAL must be a regular file')
        has_wal = wal.exists() and wal.stat().st_size > 0
        if has_wal and not force:
            raise DietError('non-empty WAL; stop n8n and checkpoint, or use --force for a private WAL snapshot')
        if has_wal and shm_fd is None:
            raise DietError('WAL snapshot requires an existing SHM lock file; checkpoint a separate backup first')
        # A WAL-mode database with no SHM could start a new writer outside our
        # main-file lock. Refuse rather than claim a stable immutable snapshot.
        os.lseek(fd, 18, os.SEEK_SET)
        if os.read(fd, 2) == b'\x02\x02' and shm_fd is None:
            raise DietError('WAL-mode source has no SHM lock file; use an offline DELETE-journal backup')
        with tempfile.TemporaryDirectory(prefix='n8n-exec-diet-') if has_wal else _no_temp() as temp:
            snapshot = source
            if has_wal:
                snapshot = Path(temp) / 'snapshot.sqlite'
                shutil.copyfile(source, snapshot)
                shutil.copyfile(wal, str(snapshot) + '-wal')
            conn = connect(snapshot, immutable=not has_wal)
            try:
                conn.execute('BEGIN')
                conn.execute('SELECT count(*) FROM sqlite_schema').fetchone()
                if fingerprint is not None:
                    # Closing another descriptor for the main DB would release
                    # ALL this process's POSIX locks on it. Reuse the locked fd.
                    fingerprint.update(path=str(source), database=fingerprint_fd(fd), wal=None)
                    if has_wal:
                        wal_fd = os.open(wal, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            fingerprint['wal'] = fingerprint_fd(wal_fd)
                        finally:
                            os.close(wal_fd)
                yield conn, source, has_wal
            finally:
                conn.close()
    finally:
        if shm_fd is not None:
            os.close(shm_fd)
        os.close(fd)


@contextmanager
def _no_temp():
    yield None


def output_target(path, source=None, overwrite=False):
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise DietError('output must not be a symlink')
    target = supplied.parent.resolve(strict=True) / supplied.name
    if source:
        source = Path(source).resolve()
        if target in [source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))]:
            raise DietError('output must not be source or a source sidecar')
        if target.exists() and os.path.samefile(target, source):
            raise DietError('output aliases source (hard link)')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise DietError('output has SQLite sidecars; choose a fresh destination')
    if target.exists():
        if not target.is_file():
            raise DietError('output exists and is not a regular file')
        if not overwrite:
            raise DietError('output exists; use --overwrite to replace it')
    return target


@contextmanager
def staged_output(target, *, overwrite=False, recovery_hint=None):
    fd, name = tempfile.mkstemp(prefix='.n8n-exec-diet-', suffix='.sqlite', dir=target.parent)
    os.close(fd)
    staging = Path(name)
    try:
        yield staging
        with staging.open('rb') as file:
            os.fsync(file.fileno())
        with _publish_guard(target, overwrite=overwrite):
            # link() provides race-safe no-clobber semantics; replace() requires opt-in.
            if overwrite:
                os.replace(staging, target)
            else:
                os.link(staging, target)
            try:
                dirfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            except OSError as exc:
                # The rename/link has already happened. Never claim that no
                # output exists, nor delete the only verified copy on failure.
                raise DietError('output published at ' + str(target) +
                                '; directory durability unconfirmed; retain the file; ' +
                                (recovery_hint or 'verify the published file before recovery') +
                                '; do not blindly overwrite it') from exc
    finally:
        staging.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(staging) + suffix).unlink(missing_ok=True)


@contextmanager
def _publish_guard(target, *, overwrite):
    # Recheck at publication, and never replace a database locked by a reader
    # or writer. O_RDWR is solely for a write lock on the authorized OUTPUT;
    # no bytes in that old output are written.
    output_target(target, overwrite=overwrite)
    fd = None
    try:
        if overwrite and target.exists():
            fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 512, 0x40000000)
            except OSError as exc:
                raise DietError('output database is locked; choose a fresh destination') from exc
            if os.fstat(fd).st_ino != target.stat().st_ino:
                raise DietError('output path changed before publication')
        yield
    finally:
        if fd is not None:
            os.close(fd)


def ordinary_tables(conn):
    rows = conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
    for row in rows:
        if row['sql'] and re.match(r'^\s*CREATE\s+VIRTUAL\b', row['sql'], re.IGNORECASE):
            raise DietError('virtual tables are unsupported: ' + row['name'])
    return [row['name'] for row in rows]


def row_counts(conn):
    return {name: conn.execute('SELECT count(*) FROM ' + quote(name)).fetchone()[0]
            for name in ordinary_tables(conn)}
