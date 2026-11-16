"""Versioned normalized inventory, independent of original source exports."""
import json
import os
from pathlib import Path
import re
import stat

from .cron import CronError, normalize_cron
from .extract import Inventory, MAX_RECORDS, unique_object
from .model import InputError, Schedule, timezone

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
# These are part of the v1 normalized format, rather than inferred at read time.
ADAPTERS = {
    'crontab': ('standard', 'or'),
    'n8n': ('n8n', 'or-values'),
    'node-red': ('standard', 'or-literal'),
    'huginn': ('standard', 'and'),
    'windmill': ('windmill', 'and'),
    'activepieces': ('standard', 'or'),
}
# Freeze v1 explicitly: future Schedule additions must not silently change it.
RECORD_FIELDS = {'id', 'platform', 'file', 'workflow', 'node', 'kind', 'expr',
                 'timezone', 'enabled', 'supported', 'error', 'source_expr',
                 'source_dialect', 'second', 'day_mode', 'timezone_assumed',
                 'enabled_assumed'}


def exact_object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise InputError(f'snapshot {label} has missing or unknown fields')


def string(value, label, maximum, nonempty=False):
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value):
        raise InputError(f'snapshot {label} must be a string of at most {maximum} characters')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise InputError(f'snapshot {label} contains invalid Unicode') from None


def assumptions(inv):
    return {'default_timezone': inv.default_timezone,
            'timezone_assumed_records': sum(r.timezone_assumed for r in inv.schedules),
            'enabled_assumed_records': sum(r.enabled_assumed for r in inv.schedules)}


def validate_record(data):
    exact_object(data, RECORD_FIELDS, 'schedule')
    for key, maximum in [('id', 20), ('platform', 32), ('file', 4096), ('workflow', 512),
                         ('node', 512), ('kind', 16), ('expr', 1024), ('timezone', 128),
                         ('source_dialect', 16), ('day_mode', 16)]:
        string(data[key], key, maximum, nonempty=key in ('id', 'file', 'timezone'))
    if not re.fullmatch('[0-9a-f]{20}', data['id']):
        raise InputError('snapshot ID must be 20 lowercase hexadecimal digits')
    for key in ('enabled', 'supported', 'timezone_assumed', 'enabled_assumed'):
        if type(data[key]) is not bool:
            raise InputError(f'snapshot {key} must be a boolean')
    if type(data['second']) is not int or not 0 <= data['second'] <= 59:
        raise InputError('snapshot second must be an integer in 0..59')
    pair = (data['source_dialect'], data['day_mode'])
    # Early n8n structural failures are emitted before its dialect is selected.
    early_n8n_error = data['platform'] == 'n8n' and not data['supported'] and pair == ('standard', 'or')
    if (data['platform'] not in ADAPTERS or
            (pair != ADAPTERS[data['platform']] and not early_n8n_error)):
        raise InputError('snapshot platform/dialect/day mode must match a v1 adapter')
    if data['kind'] not in ('cron', 'interval', 'once'):
        raise InputError('snapshot has unknown schedule kind')
    timezone(data['timezone'])  # Includes unsupported and disabled records.
    if data['source_expr'] is not None:
        string(data['source_expr'], 'source_expr', 1024)
    record = Schedule(**data)  # Preserve ID and provenance; never call make_schedule.
    if record.supported:
        if record.kind != 'cron' or record.error is not None or record.source_expr is None:
            raise InputError('snapshot supported cron must have source_expr and no error')
        try:
            normalized, second = normalize_cron(record.source_expr, record.source_dialect)
            cron = record.cron()
            if (normalized, second) != (record.expr, record.second):
                raise InputError('snapshot normalized expression/second disagrees with source_expr')
            if record.platform == 'activepieces' and len(cron.month_days) != 31 and len(cron.week_days) != 7:
                raise InputError('snapshot Activepieces combined day constraints are unsupported')
        except CronError:
            raise InputError('snapshot supported record has an invalid cron expression') from None
    else:
        string(record.error, 'unsupported error', 1024, nonempty=True)
    return record


def validate_snapshot(data):
    exact_object(data, {'schema', 'version', 'schedules', 'warnings', 'assumptions'}, 'envelope')
    if (data['schema'] != 'cronherd.inventory' or type(data['version']) is not int or data['version'] != 1):
        raise InputError('snapshot requires schema=cronherd.inventory and integer version=1')
    saved = data['assumptions']
    exact_object(saved, {'default_timezone', 'timezone_assumed_records', 'enabled_assumed_records'}, 'assumptions')
    string(saved['default_timezone'], 'default_timezone', 128, nonempty=True)
    inv = Inventory(saved['default_timezone'])
    if not isinstance(data['schedules'], list) or not 1 <= len(data['schedules']) <= MAX_RECORDS:
        raise InputError('snapshot must have 1..2000 schedules')
    inv.schedules = [validate_record(record) for record in data['schedules']]
    if len({r.id for r in inv.schedules}) != len(inv.schedules):
        raise InputError('snapshot contains duplicate schedule IDs')
    for key in ('timezone_assumed_records', 'enabled_assumed_records'):
        if type(saved[key]) is not int or saved[key] != assumptions(inv)[key]:
            raise InputError('snapshot assumption counts disagree with schedule flags')
    if any(r.timezone_assumed and r.timezone != inv.default_timezone for r in inv.schedules):
        raise InputError('snapshot assumed timezone disagrees with saved default_timezone')
    if not isinstance(data['warnings'], list) or len(data['warnings']) > 10000:
        raise InputError('snapshot warnings must be an array of at most 10000 strings')
    for warning in data['warnings']:
        string(warning, 'warning', 8192)
    inv.warnings = data['warnings']
    return inv


def snapshot_json(inv):
    """Validate and serialize fully before the CLI writes anything to stdout."""
    data = {'schema': 'cronherd.inventory', 'version': 1,
            'schedules': [r.as_dict() for r in sorted(inv.schedules, key=lambda r: r.id)],
            'warnings': inv.warnings, 'assumptions': assumptions(inv)}
    validate_snapshot(data)
    raw = json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False) + '\n'
    if len(raw.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        raise InputError('snapshot size limit exceeded (8 MiB)')
    return raw


def load_snapshot(path):
    path = Path(path)
    try:
        # O_NONBLOCK prevents hanging on a FIFO; O_NOFOLLOW rejects symlinks.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise InputError('snapshot input must be a regular file, not a symlink')
            raw = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise InputError('snapshot size limit exceeded (8 MiB)')
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate_snapshot(data)
    except InputError:
        raise
    except (OSError, ValueError, RecursionError):
        raise InputError('cannot read snapshot: expected regular UTF-8 JSON file with unique keys') from None
