"""Versioned reclamation artifacts and repeatable copy checks."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import struct
import tempfile

from .storage import DietError, ordinary_tables, quote, row_counts

FORMAT = 'n8n-exec-diet/reclaim-plan'
MAX_PLAN_BYTES = 1048576
CONTENT_ALGORITHM = 'sha256-typed-rows-v1'


class _TextBytes(bytes):
    """Keep SQLite TEXT bytes (even invalid UTF-8) distinct from BLOB bytes."""


DEFAULT_ROW_HASH_MEMORY_BYTES = 8 * 1024 * 1024


def validate_row_hash_memory_bytes(value):
    # This budgets raw hashes, not Python objects, SQLite, or row/BLOB values.
    if type(value) is not int or value < 32:
        raise DietError('row-hash memory bytes must be an integer of at least 32')
    return value // 32


def validate_max_content_value_bytes(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise DietError('max-content-value-bytes must be an integer from 0 to 9223372036854775807')


def _check_content_values(conn, table, limit):
    # A separate predicate per column avoids SQLite expression/parameter limits
    # on wide tables. table_xinfo includes generated columns, as does SELECT *.
    # Return only a constant, never a payload, identity, or offending value size.
    with closing(conn.execute('PRAGMA table_xinfo(' + quote(table) + ')')) as columns:
        for column in columns:
            if column[6] == 1:  # hidden virtual-table columns are not in SELECT *
                continue
            name = quote(column[1])
            sql = ('SELECT 1 FROM ' + quote(table) + ' WHERE typeof(' + name
                   + ") IN ('text','blob') AND length(CAST(" + name + ' AS BLOB)) > ? LIMIT 1')
            with closing(conn.execute(sql, (limit,))) as check:
                if check.fetchone() is not None:
                    raise DietError('content value exceeds --max-content-value-bytes; no output published')


def validate_max_content_row_bytes(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
        raise DietError('max-content-row-bytes must be an integer from 0 to 9223372036854775807')


def _content_row_bytes_sql(conn, table):
    # All declared columns, including virtual/stored generated columns, match
    # SELECT *. No payload or identity leaves SQLite during this preflight.
    with closing(conn.execute('PRAGMA table_xinfo(' + quote(table) + ')')) as columns:
        terms = []
        for column in columns:
            if column[6] == 1:
                continue
            name = quote(column[1])
            terms.append('(CASE WHEN typeof(' + name + ") IN ('text','blob') THEN length(CAST("
                         + name + ' AS BLOB)) ELSE 0 END)')
    # SQLite hard maxima are 32767 columns and 2^31-1 bytes per value. Even
    # generated columns sum to <2^46, so integer addition cannot overflow into
    # REAL. Keep the policy bound as an integer, including at 2^63-1.
    # Balance addition to avoid expression-depth limits on wide tables.
    while len(terms) > 1:
        terms = ['(' + terms[i] + ' + ' + terms[i+1] + ')' if i+1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0] if terms else '0'


def _check_content_rows(conn, table, limit):
    total = _content_row_bytes_sql(conn, table)
    with closing(conn.execute('SELECT 1 FROM ' + quote(table) + ' WHERE ' + total + ' > ? LIMIT 1',
                              (limit,))) as check:
        if check.fetchone() is not None:
            raise DietError('content row exceeds --max-content-row-bytes; no output published')


def validate_max_content_rows(value):
    if value is not None and (type(value) is not int or value < 0):
        raise DietError('max-content-rows must be a non-negative integer')


def _check_content_row_count(conn, tables, limit):
    # Count the whole pass before any payload SELECT or byte preflight. Python
    # integers keep the cross-table total exact, even beyond SQLite's int64.
    total = 0
    for table in tables:
        with closing(conn.execute('SELECT count(*) FROM ' + quote(table))) as cursor:
            total += cursor.fetchone()[0]
        if total > limit:
            raise DietError('content row count exceeds --max-content-rows; no output published')


def validate_max_content_bytes(value):
    if value is not None and (type(value) is not int or value < 0):
        raise DietError('max-content-bytes must be a non-negative integer')


def _check_content_bytes(conn, tables, limit):
    # Only integer row sizes enter Python. Never SUM across rows in SQLite:
    # that could overflow int64 or round via REAL. Python keeps the total exact.
    total = 0
    try:
        for table in tables:
            expression = _content_row_bytes_sql(conn, table)
            with closing(conn.execute('SELECT ' + expression + ' FROM ' + quote(table))) as cursor:
                for row in cursor:
                    total += row[0]
                    if total > limit:
                        raise DietError('content bytes exceed --max-content-bytes; no output published')
    except (sqlite3.Error, OSError) as exc:
        # SQLite/OS exception text can contain schema identities or paths.
        raise DietError('content byte preflight failed; no output published') from exc


def _row_hash(row):
    digest = hashlib.sha256(b'n8n-exec-diet/row/v1\0' + struct.pack('>Q', len(row)))
    for value in row:
        if value is None:
            tag, encoded = b'n', b''
        elif isinstance(value, _TextBytes):
            tag, encoded = b't', value
        elif isinstance(value, bytes):
            tag, encoded = b'b', value
        elif isinstance(value, int):
            tag, encoded = b'i', struct.pack('>q', value)
        elif isinstance(value, float):
            # SQLite treats signed zeros identically; VACUUM may normalize them.
            tag, encoded = b'r', struct.pack('>d', value if value else 0.0)
        else:
            raise DietError('unsupported SQLite value type for content digest')
        digest.update(tag)
        digest.update(struct.pack('>Q', len(encoded)))
        digest.update(encoded)
    return digest.digest()


def _write_hash(stream, value):
    # Unbuffered regular-file writes can be short (e.g. disk exhaustion).
    pending = memoryview(value)
    while pending:
        written = stream.write(pending)
        if not written:
            raise OSError('could not write content hash spill')
        pending = pending[written:]


def _write_run(stream, rows):
    rows.sort()
    for row_hash in rows:
        _write_hash(stream, row_hash)


def _read_hash(stream, index):
    # Independent offsets let two runs share one fd without Python buffering.
    value = b''
    while len(value) < 32:
        part = os.pread(stream.fileno(), 32 - len(value), index * 32 + len(value))
        if not part:
            raise DietError('truncated content hash spill')
        value += part
    return value


def _merge_runs(source, target, width, count):
    """Merge adjacent runs with two hashes of state, no per-run fd/path list."""
    target.seek(0)
    target.truncate()
    for start in range(0, count, 2 * width):
        left, middle = start, min(start + width, count)
        right, end = middle, min(start + 2 * width, count)
        a = _read_hash(source, left) if left < middle else None
        b = _read_hash(source, right) if right < end else None
        while a is not None or b is not None:
            if b is None or (a is not None and a <= b):
                _write_hash(target, a)
                left += 1
                a = _read_hash(source, left) if left < middle else None
            else:
                _write_hash(target, b)
                right += 1
                b = _read_hash(source, right) if right < end else None


def _table_content(cursor, capacity):
    rows, count = [], 0
    source = target = None
    with ExitStack() as spills:
        for row in cursor:
            if len(rows) == capacity:
                if source is None:
                    # Anonymous mode-0600 files, removed even if the process dies.
                    # ExitStack also closes the first if creating the second fails.
                    source = spills.enter_context(tempfile.TemporaryFile(mode='w+b', buffering=0))
                    target = spills.enter_context(tempfile.TemporaryFile(mode='w+b', buffering=0))
                _write_run(source, rows)
                rows.clear()
            rows.append(_row_hash(row))
            count += 1
        digest = hashlib.sha256(b'n8n-exec-diet/table/v1\0' + struct.pack('>Q', count))
        if source is None:
            rows.sort()
            for row_hash in rows:
                digest.update(row_hash)
        else:
            _write_run(source, rows)
            rows.clear()
            width = capacity
            while width < count:
                _merge_runs(source, target, width, count)
                source, target = target, source
                width *= 2
            for index in range(count):
                digest.update(_read_hash(source, index))
        return digest.hexdigest()


def content_digests(conn, *, row_hash_memory_bytes=DEFAULT_ROW_HASH_MEMORY_BYTES,
                    max_content_value_bytes=None, max_content_row_bytes=None, max_content_rows=None, max_content_bytes=None):
    """Hash typed-row multisets with bounded runs, excluding implicit rowids.

    SELECT * includes declared keys and generated columns. Raw TEXT remains
    distinct from BLOBs. The budget counts 32-byte row hashes, excluding Python
    overhead and a full row's values. Two private files bound merge state/fds.
    """
    capacity = validate_row_hash_memory_bytes(row_hash_memory_bytes)
    validate_max_content_value_bytes(max_content_value_bytes)
    validate_max_content_row_bytes(max_content_row_bytes)
    validate_max_content_rows(max_content_rows)
    validate_max_content_bytes(max_content_bytes)
    names = ordinary_tables(conn)
    if max_content_rows is not None:
        _check_content_row_count(conn, names, max_content_rows)
    if max_content_bytes is not None:
        _check_content_bytes(conn, names, max_content_bytes)
    tables = {}
    for table in names:
        # Caller retains the locked snapshot through preflight and content scan.
        # Check before SELECT *, including sqlite3's eager first sqlite3_step.
        if max_content_value_bytes is not None:
            _check_content_values(conn, table, max_content_value_bytes)
        if max_content_row_bytes is not None:
            _check_content_rows(conn, table, max_content_row_bytes)
        previous_factory = conn.text_factory
        conn.text_factory = _TextBytes
        cursor = None
        try:
            cursor = conn.execute('SELECT * FROM ' + quote(table))
            tables[table] = _table_content(cursor, capacity)
        finally:
            if cursor is not None:
                cursor.close()
            conn.text_factory = previous_factory
    return {'algorithm': CONTENT_ALGORITHM, 'tables': tables}


def schema_sha256(conn):
    # rootpage and schema_version change during VACUUM. Object definitions and
    # application metadata must survive; hash those in a canonical order.
    objects = [list(row) for row in conn.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name COLLATE BINARY')]
    metadata = {key: conn.execute('PRAGMA ' + key).fetchone()[0]
                for key in ('user_version', 'application_id', 'encoding')}
    encoded = json.dumps({'objects': objects, 'metadata': metadata},
                         sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate plan key')
        result[key] = value
    return result


def load_plan(path):
    """Read before acquiring DB locks; reject special files and unbounded JSON."""
    supplied = Path(path).absolute()
    if supplied.is_symlink():
        raise DietError('reclamation plan must be a regular file, not a symlink')
    resolved = supplied.resolve(strict=True)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DietError('reclamation plan must be a regular file')
        chunks = []
        remaining = MAX_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
    finally:
        os.close(fd)
    if len(raw) > MAX_PLAN_BYTES:
        raise DietError('invalid reclamation plan: exceeds 1 MiB limit')
    try:
        plan = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DietError('invalid reclamation plan: expected versioned JSON artifact') from exc

    def integer(value):
        return type(value) is int and value >= 0

    def digest(value):
        return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

    def fingerprint(value):
        return (isinstance(value, dict) and set(value) == {'bytes', 'sha256'}
                and integer(value['bytes']) and digest(value['sha256']))

    fields = {'format', 'version', 'operation', 'source', 'schema_sha256', 'row_counts', 'storage'}
    valid = (isinstance(plan, dict) and type(plan.get('version')) is int
             and ((plan['version'] == 1 and set(plan) == fields)
                  or (plan['version'] == 2 and set(plan) == fields | {'content'})))
    if valid:
        source = plan['source']
        valid = (plan['format'] == FORMAT
                 and plan['operation'] == 'vacuum-into' and isinstance(source, dict)
                 and set(source) == {'path', 'database', 'wal'}
                 and isinstance(source['path'], str) and Path(source['path']).is_absolute()
                 and fingerprint(source['database'])
                 and (source['wal'] is None or fingerprint(source['wal']))
                 and digest(plan['schema_sha256']) and isinstance(plan['row_counts'], dict)
                 and bool(plan['row_counts'])
                 and all(isinstance(k, str) and integer(v) for k, v in plan['row_counts'].items())
                 and isinstance(plan['storage'], dict)
                 and all(integer(v) for v in plan['storage'].values()))
    if valid and plan['version'] == 2:
        content = plan['content']
        valid = (isinstance(content, dict) and set(content) == {'algorithm', 'tables'}
                 and content['algorithm'] == CONTENT_ALGORITHM
                 and isinstance(content['tables'], dict)
                 and set(content['tables']) == set(plan['row_counts'])
                 and all(digest(v) for v in content['tables'].values()))
    if not valid:
        raise DietError('invalid reclamation plan: unsupported format, version or fields')
    return plan, resolved


def verify_structure(conn, *, expected_schema, expected_counts, expected_content=None,
                     row_hash_memory_bytes=DEFAULT_ROW_HASH_MEMORY_BYTES, max_content_value_bytes=None, max_content_row_bytes=None, max_content_rows=None, max_content_bytes=None,
                     report_content_mismatches=False):
    if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:
        raise DietError('copy integrity_check failed')
    if schema_sha256(conn) != expected_schema:
        raise DietError('copy schema verification failed')
    if row_counts(conn) != expected_counts:
        raise DietError('copy row count verification failed')
    if expected_content is not None:
        # Complete the same policy-checked pass before emitting any metadata.
        # Reuse its hashes: explanation never fetches payloads a second time.
        actual = content_digests(conn, row_hash_memory_bytes=row_hash_memory_bytes,
                                 max_content_value_bytes=max_content_value_bytes,
                                 max_content_row_bytes=max_content_row_bytes,
                                 max_content_rows=max_content_rows,
                                 max_content_bytes=max_content_bytes)
        if actual != expected_content:
            if report_content_mismatches:
                return [{'table': table, 'expected_sha256': expected_content['tables'][table],
                         'actual_sha256': actual['tables'][table]}
                        for table in sorted(expected_content['tables'])
                        if actual['tables'][table] != expected_content['tables'][table]]
            raise DietError('copy content verification failed')
