"""Format adapters. Only schedule metadata is retained; workflows never run."""
import json
import os
from pathlib import Path
import re
import stat
from .model import InputError, make_schedule, timezone
from .yaml_subset import load_schedule

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
MAX_RECORDS = 2000
IGNORE_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}
HUGINN = {
    'every_1m': '* * * * *', 'every_2m': '*/2 * * * *',
    'every_5m': '*/5 * * * *', 'every_10m': '*/10 * * * *',
    'every_30m': '*/30 * * * *', 'every_1h': '0 * * * *',
    'every_2h': '0 */2 * * *', 'every_5h': '0 */5 * * *',
    'every_12h': '0 */12 * * *', 'every_1d': '0 0 * * *',
    'every_2d': '0 0 */2 * *', 'every_7d': '0 0 * * 1',
    'midnight': '0 0 * * *', 'noon': '0 12 * * *', 'never': '0 0 * * *',
}
for _h in range(1, 12):
    HUGINN[f'{_h}am'] = f'0 {_h} * * *'
    HUGINN[f'{_h}pm'] = f'0 {_h + 12} * * *'


def label(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 512:
        raise ValueError('schedule label must be a string/integer of at most 512 characters')
    return str(value)


def mapping(value):
    if not isinstance(value, dict):
        raise ValueError('expected an object')
    return value


def sequence(value):
    if not isinstance(value, list):
        raise ValueError('expected an array')
    return value


def integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('expected an integer schedule parameter')
    if isinstance(value, str) and (len(value) > 8 or not value.isascii() or not value.isdecimal()):
        raise ValueError('dynamic or non-integer schedule parameter is unsupported')
    n = int(value)
    if not low <= n <= high:
        raise ValueError('schedule parameter outside supported range')
    return n


def boolean(value):
    if not isinstance(value, bool):
        raise ValueError('schedule flag must be a boolean')
    return value


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('duplicate JSON key')
        result[k] = v
    return result


class Inventory:
    def __init__(self, default_timezone='UTC'):
        timezone(default_timezone)
        self.default_timezone = default_timezone
        self.schedules = []
        self.warnings = []

    def emit(self, platform, file, key, workflow, node, expr, tz=None, enabled=True, **kw):
        if len(self.schedules) >= MAX_RECORDS:
            raise InputError(f'more than {MAX_RECORDS} schedules; split the input')
        self.schedules.append(make_schedule(platform, file, key, workflow, node, expr,
                              self.default_timezone if tz is None else tz, enabled,
                              timezone_assumed=tz is None, **kw))

    def n8n(self, wf, file, key):
        name = wf.get('name', wf.get('id', file.stem))
        settings = mapping(wf.get('settings', {}))
        tz = settings.get('timezone')
        for index, node in enumerate(sequence(wf['nodes'])):
            node = mapping(node)
            typ = node.get('type')
            if typ not in ('n8n-nodes-base.scheduleTrigger', 'n8n-nodes-base.cron'):
                continue
            node_label = label(node.get('name', node.get('id', index)))
            try:
                enabled = boolean(wf.get('active', True)) and not boolean(node.get('disabled', False))
                params = mapping(node.get('parameters', {}))
                if typ.endswith('.cron'):
                    rules = sequence(mapping(params.get('triggerTimes', {})).get('item', []))
                else:
                    rules = sequence(mapping(params.get('rule', {})).get('interval', []))
                if not rules:
                    raise ValueError('schedule node has no exported rules')
            except ValueError as e:
                self.emit('n8n', file, key, name, f'{node_label}[{index}]', '', tz, error=str(e))
                continue
            for i, rule in enumerate(rules):
                expr, error = '', None
                try:
                    rule = mapping(rule)
                    if typ.endswith('.cron'):
                        expr = legacy_n8n(rule)
                    else:
                        expr = schedule_n8n(rule, node.get('typeVersion', 1))
                except ValueError as e:
                    error = str(e)
                self.emit('n8n', file, key, name, f'{node_label}[{index}:{i}]', expr, tz,
                          enabled, dialect='n8n', error=error, day_mode='or-values', enabled_assumed='active' not in wf)

    def node_red(self, flows, file, key):
        tabs = {n['id']: n for n in flows if isinstance(n, dict) and n.get('type') == 'tab' and 'id' in n}
        for index, node in enumerate(flows):
            node = mapping(node)
            if node.get('type') != 'inject':
                continue
            if not node.get('crontab') and not node.get('repeat') and not node.get('once'):
                continue
            tab = tabs.get(node.get('z'), {})
            name = tab.get('label', node.get('z', file.stem))
            node_label = label(node.get('name') or node.get('id', index))
            error, kind, expr, enabled = None, 'cron', node.get('crontab', ''), True
            try:
                enabled = not boolean(node.get('d', False)) and not boolean(tab.get('disabled', False))
                if node.get('repeat'):
                    # Node-RED gives repeat precedence over crontab.
                    expr, kind = str(node['repeat']), 'interval'
                    raise ValueError('relative repeat has no exported start phase; cannot forecast')
                if not expr:
                    kind = 'once'
                    raise ValueError('startup-only inject is not a wall-clock schedule')
                if node.get('once'):
                    self.warnings.append(f'{file}: inject {index} also runs once at startup; startup fire is excluded')
            except ValueError as e:
                error = str(e)
            self.emit('node-red', file, key, name, f'{node_label}[{index}]', expr, enabled=enabled,
                      kind=kind, error=error, day_mode='or-literal')

    def huginn(self, scenario, file, key):
        name = scenario.get('name', file.stem)
        for i, agent in enumerate(sequence(scenario['agents'])):
            agent = mapping(agent)
            sched = agent.get('schedule')
            if sched is None:
                continue
            expr, error, enabled = '', None, True
            try:
                enabled = not boolean(agent.get('disabled', False)) and not boolean(agent.get('deactivated', False))
                if not isinstance(sched, str) or sched not in HUGINN:
                    raise ValueError('unknown Huginn schedule enum')
                expr = HUGINN[sched]
                enabled = enabled and sched != 'never'
            except ValueError as e:
                error = str(e)
            self.emit('huginn', file, key, name, f"{label(agent.get('name', agent.get('guid', i)))}[{i}]",
                      expr, enabled=enabled, error=error, day_mode='and')
            if str(agent.get('type', '')).split('::')[-1] == 'SchedulerAgent':
                self.warnings.append(f'{file}: SchedulerAgent options/control targets are outside v1; only its schedule enum is inventoried')

    def windmill(self, data, file, key):
        self.emit('windmill', file, key, data.get('script_path', file.stem),
                  data.get('path', file.stem), data.get('schedule', data.get('cron', '')),
                  data.get('timezone'), data.get('enabled', True), dialect='windmill',
                  day_mode='and', enabled_assumed='enabled' not in data)

    def activepieces(self, data, file, key):
        # Supports flow exports, flowVersion wrappers and template flows arrays.
        if 'flows' in data:
            for i, flow in enumerate(sequence(data['flows'])):
                self.activepieces(mapping(flow), file, key + f'/flows/{i}')
            return
        version = mapping(data.get('version', data.get('flowVersion', data)))
        trigger = mapping(version.get('trigger', {}))
        settings = mapping(trigger.get('settings', {}))
        if settings.get('pieceName') not in ('@activepieces/piece-schedule', '@activepieces/pieces-schedule'):
            return
        name = version.get('displayName', data.get('displayName', file.stem))
        node_label = label(trigger.get('name', 'trigger'))
        expr, error, kind, tz, enabled = '', None, 'cron', None, True
        try:
            status = data.get('status', 'ENABLED')
            if status not in ('ENABLED', 'DISABLED'):
                raise ValueError('unknown Activepieces flow status')
            enabled = status == 'ENABLED'
            props = mapping(settings.get('input', {}))
            typ = settings.get('triggerName')
            tz = props.get('timezone')
            if typ == 'cron_expression':
                expr = props.get('cronExpression', '')
            elif typ == 'every_x_minutes':
                kind, expr, tz = 'interval', str(integer(props.get('minutes', 1), 1, 59) * 60), 'UTC'
                raise ValueError('relative interval has no exported start phase; cannot forecast')
            elif typ in ('every_hour', 'every_day'):
                weekends = boolean(props.get('run_on_weekends', False))
                hour = '*' if typ == 'every_hour' else integer(props.get('hour_of_the_day', 0), 0, 23)
                if typ == 'every_hour':
                    tz = 'UTC'
                expr = f"0 {hour} * * {'*' if weekends else '1-5'}"
            elif typ == 'every_week':
                hour = integer(props.get('hour_of_the_day'), 0, 23)
                day = integer(props.get('day_of_the_week'), 0, 6)
                expr = f'0 {hour} * * {day}'
            elif typ == 'every_month':
                hour = integer(props.get('hour_of_the_day'), 0, 23)
                day = integer(props.get('day_of_the_month'), 1, 31)
                expr = f'0 {hour} {day} * *'
            else:
                raise ValueError('unsupported Activepieces schedule trigger')
        except ValueError as e:
            error = str(e)
        self.emit('activepieces', file, key, name, node_label, expr, tz, enabled,
                  kind=kind, error=error, enabled_assumed='status' not in data)

    def crontab(self, text, file, key):
        tz = None
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            env = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if env:
                if env[1] == 'CRON_TZ':
                    tz = env[2].strip().strip('\"\'')
                    timezone(tz)
                elif env[1] == 'TZ':
                    self.warnings.append(f'{file}:{i}: TZ is a job environment variable; use CRON_TZ or --default-timezone for scheduling')
                continue
            error = None
            if line.startswith('@'):
                parts = line.split(None, 1)
                expr = parts[0]
                if len(parts) != 2:
                    error = 'crontab line has no command'
            else:
                parts = line.split(None, 5)
                expr = ' '.join(parts[:5])
                if len(parts) != 6:
                    error = 'crontab line requires five fields and a command'
            # Command/username remains opaque and is never kept or evaluated.
            self.emit('crontab', file, key, file.stem, f'line {i}', expr, tz, error=error)


def schedule_n8n(rule, version):
    field = rule.get('field', 'days')
    if field == 'cronExpression':
        return rule.get('expression', '')
    # Omitted clock fields may be randomized by the installed n8n version.
    # typeVersion is a node schema version, not the running server version.
    if not isinstance(version, (int, float)) or isinstance(version, bool):
        raise ValueError('invalid n8n typeVersion')
    def clock(key, high):
        if key not in rule:
            raise ValueError('n8n randomized/implicit clock field is not exported; use explicit cron')
        return integer(rule.get(key, 0), 0, high)
    if field == 'seconds':
        raise ValueError('multiple firings per minute are unsupported')
    if field == 'minutes':
        n = integer(rule.get('minutesInterval', 5), 1, 59)
        if 60 % n:
            raise ValueError('stateful n8n minute recurrence is unsupported; use explicit cron')
        return f'*/{n} * * * *'
    minute = clock('triggerAtMinute', 59)
    if field == 'hours':
        if integer(rule.get('hoursInterval', 1), 1, 23) != 1:
            raise ValueError('stateful n8n hour recurrence is unsupported; use explicit cron')
        return f'{minute} * * * *'
    hour = clock('triggerAtHour', 23)
    if field == 'days':
        if integer(rule.get('daysInterval', 1), 1, 31) != 1:
            raise ValueError('stateful n8n day recurrence is unsupported; use explicit cron')
        return f'{minute} {hour} * * *'
    if field == 'weeks':
        if integer(rule.get('weeksInterval', 1), 1, 52) != 1:
            raise ValueError('stateful n8n week recurrence is unsupported; use explicit cron')
        days = sequence(rule.get('triggerAtDay', [0]))
        days = ','.join(str(integer(d, 0, 6)) for d in days) or '*'
        return f'{minute} {hour} * * {days}'
    if field == 'months':
        if integer(rule.get('monthsInterval', 1), 1, 12) != 1:
            raise ValueError('stateful n8n month recurrence is unsupported; use explicit cron')
        if 'triggerAtDayOfMonth' not in rule:
            raise ValueError('n8n randomized day of month is not exported')
        day = integer(rule.get('triggerAtDayOfMonth', 1), 1, 31)
        return f'{minute} {hour} {day} * *'
    raise ValueError('unknown n8n schedule interval')


def legacy_n8n(rule):
    mode = rule.get('mode', 'everyDay')
    if mode == 'custom':
        return rule.get('cronExpression', '')
    if mode == 'everyMinute':
        return '* * * * *'
    minute = integer(rule.get('minute', 0), 0, 59)
    if mode == 'everyHour':
        return f'{minute} * * * *'
    if mode == 'everyX':
        n = integer(rule.get('value', 2), 1, 59)
        unit = rule.get('unit', 'hours')
        if unit == 'minutes':
            return f'*/{n} * * * *'
        if unit == 'hours' and n <= 23:
            return f'0 */{n} * * *'
        raise ValueError('unsupported legacy n8n everyX unit/range')
    hour = integer(rule.get('hour', 14), 0, 23)
    if mode == 'everyDay':
        return f'{minute} {hour} * * *'
    if mode == 'everyWeek':
        return f"{minute} {hour} * * {integer(rule.get('weekday', 1), 0, 6)}"
    if mode == 'everyMonth':
        return f"{minute} {hour} {integer(rule.get('dayOfMonth', 1), 1, 31)} * *"
    raise ValueError('unsupported legacy n8n cron mode')


def input_files(paths, warnings):
    seen, result = set(), []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise InputError(f'input path does not exist: {p}')
        if p.is_symlink():
            warnings.append(f'{p}: symlink skipped')
            continue
        if p.is_dir():
            candidates = []
            def walk_error(e):
                raise InputError(f'cannot read input directory: {e.filename}')
            for parent, dirs, files in os.walk(p, followlinks=False, onerror=walk_error):
                for d in dirs:
                    if (Path(parent) / d).is_symlink():
                        warnings.append(f'{Path(parent) / d}: symlink skipped')
                dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.') and not (Path(parent) / d).is_symlink())
                for f in sorted(files):
                    candidates.append((Path(parent) / f, (Path(parent) / f).relative_to(p).as_posix()))
                    if len(candidates) > MAX_FILES:
                        raise InputError(f'more than {MAX_FILES} files; split the input')
        else:
            candidates = [(p, p.name)]
        for f, key in candidates:
            if f.is_symlink() or not f.is_file():
                warnings.append(f'{f}: symlink or non-regular file skipped')
                continue
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append((f, key))
            if len(result) > MAX_FILES:
                raise InputError(f'more than {MAX_FILES} files; split the input')
    return result


def extract(paths, default_timezone='UTC'):
    inv = Inventory(default_timezone)
    total = 0
    for file, key in input_files(paths, inv.warnings):
        is_yaml = file.name.endswith(('.schedule.yaml', '.schedule.yml'))
        is_cron = file.name in ('crontab', '.crontab') or file.suffix in ('.cron', '.crontab')
        if not is_yaml and not is_cron and file.suffix != '.json':
            inv.warnings.append(f'{file}: unrecognised file skipped')
            continue
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InputError(f'{file}: not a regular file')
                data = stream.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise InputError('input size limit exceeded (8 MiB/file, 64 MiB total)')
            text = data.decode('utf-8-sig')
            if is_yaml:
                inv.windmill(load_schedule(text), file, key)
            elif is_cron:
                inv.crontab(text, file, key)
            else:
                obj = json.loads(text, object_pairs_hook=unique_object,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON constant')))
                if isinstance(obj, dict) and isinstance(obj.get('nodes'), list):
                    inv.n8n(obj, file, key)
                elif isinstance(obj, list) and any(isinstance(n, dict) and n.get('type') == 'inject' for n in obj):
                    inv.node_red(obj, file, key)
                elif isinstance(obj, list) and obj and all(isinstance(w, dict) and 'nodes' in w for w in obj):
                    for i, wf in enumerate(obj):
                        inv.n8n(wf, file, key + f'/workflows/{i}')
                elif isinstance(obj, dict) and 'agents' in obj:
                    inv.huginn(obj, file, key)
                elif isinstance(obj, dict) and any(k in obj for k in ('trigger', 'version', 'flowVersion', 'flows')):
                    inv.activepieces(obj, file, key)
                elif isinstance(obj, dict) and 'schedule' in obj and 'script_path' in obj:
                    inv.windmill(obj, file, key)
                else:
                    inv.warnings.append(f'{file}: unrecognised JSON format skipped')
        except InputError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
            raise InputError(f'{file}: malformed or unsupported input structure (payload omitted)') from None
        except OSError:
            raise InputError(f'{file}: cannot read input file') from None
    if not inv.schedules:
        details = '; '.join(inv.warnings[:3])
        raise InputError('no schedules found' + (f'; {details}' if details else ''))
    # Duplicate identities from different roots are dangerous for keyed analysis.
    ids = [r.id for r in inv.schedules]
    if len(ids) != len(set(ids)):
        raise InputError('duplicate schedule identities across input roots; use a common parent directory')
    return inv
