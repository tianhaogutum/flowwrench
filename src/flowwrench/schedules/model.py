"""Normalized records deliberately exclude commands, payloads and credentials."""
from dataclasses import dataclass, asdict
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .cron import Cron, CronError, normalize_cron


class InputError(ValueError):
    pass


def timezone(name):
    if not isinstance(name, str) or not name or len(name) > 128 or name.startswith(('/', '.')) or '..' in name.split('/'):
        raise InputError('invalid timezone identifier')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError(f'unknown timezone: {name}') from None


@dataclass
class Schedule:
    id: str
    platform: str
    file: str
    workflow: str
    node: str
    kind: str
    expr: str
    timezone: str
    enabled: bool
    supported: bool = True
    error: str | None = None
    source_expr: str | None = None
    source_dialect: str = 'standard'
    second: int = 0
    day_mode: str = 'or'
    timezone_assumed: bool = False
    enabled_assumed: bool = False

    def as_dict(self):
        return asdict(self)

    def cron(self):
        return Cron.parse(self.expr, self.day_mode)


def make_schedule(platform, file, key, workflow, node, expr, tz, enabled=True,
                  kind='cron', dialect='standard', error=None, day_mode='or',
                  timezone_assumed=False, enabled_assumed=False):
    for label in (workflow, node):
        if not isinstance(label, (str, int)) or isinstance(label, bool) or len(str(label)) > 512:
            raise InputError('workflow/node labels must be strings or integer IDs of at most 512 characters')
    timezone(tz)  # Unknown zones are fatal even on disabled/unsupported records.
    if not isinstance(enabled, bool):
        error = 'enabled/disabled flag must be a boolean'
        enabled = False
    identity = '\0'.join(map(str, [platform, key, workflow, node]))
    identity = sha256(identity.encode()).hexdigest()[:20]
    source_expr = expr if isinstance(expr, str) and len(expr) <= 1024 else None
    normalized, second = source_expr or '', 0
    if error is None:
        try:
            normalized, second = normalize_cron(expr, dialect)
            cron = Cron.parse(normalized, day_mode)
            if platform == 'activepieces' and len(cron.month_days) != 31 and len(cron.week_days) != 7:
                raise CronError('combined day-of-month/week constraints are unsupported for Activepieces versions; split into explicit schedules')
        except CronError as e:
            error = str(e)
    return Schedule(identity, platform, str(file), str(workflow), str(node), kind,
                    normalized, tz, enabled, error is None, error, source_expr,
                    dialect, second, day_mode, timezone_assumed, enabled_assumed)
