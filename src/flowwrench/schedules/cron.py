"""Bounded, independent minute-resolution cron parser (no execution)."""
from dataclasses import dataclass
from datetime import datetime


class CronError(ValueError):
    pass


MONTHS = dict(zip('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(), range(1, 13)))
DAYS = dict(zip('SUN MON TUE WED THU FRI SAT'.split(), range(7)))
ALIASES = {
    '@yearly': '0 0 1 1 *', '@annually': '0 0 1 1 *',
    '@monthly': '0 0 1 * *', '@weekly': '0 0 * * 0',
    '@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@hourly': '0 * * * *',
}


def field_values(text, low, high, names=None):
    """Lists, inclusive ranges, */step and start/step. No wraparound ranges."""
    names = names or {}
    if not text or len(text) > 256:
        raise CronError('empty or oversized cron field')

    def number(s):
        if s.upper() in names:
            return names[s.upper()]
        if not s.isascii() or not s.isdecimal():
            raise CronError('unsupported cron token (only numbers, names, *, ranges, lists and steps)')
        n = int(s)
        if not low <= n <= high:
            raise CronError(f'cron value outside {low}..{high}')
        return n

    result = set()
    for part in text.split(','):
        pair = part.split('/')
        if len(pair) > 2:
            raise CronError('invalid step')
        step = 1
        if len(pair) == 2:
            if not pair[1].isascii() or not pair[1].isdecimal():
                raise CronError('step must be a positive integer')
            step = int(pair[1])
            if not 1 <= step <= high - low + 1:
                raise CronError('step outside field width')
        base = pair[0]
        if base == '*':
            a, b = low, high
        elif '-' in base:
            ends = base.split('-')
            if len(ends) != 2:
                raise CronError('invalid range')
            a, b = map(number, ends)
        else:
            a = number(base)
            b = high if len(pair) == 2 else a
        if a > b:
            raise CronError('descending/wraparound ranges are unsupported')
        result.update(range(a, b + 1, step))
    return frozenset(result)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset
    hours: frozenset
    month_days: frozenset
    months: frozenset
    week_days: frozenset
    dom_star: bool
    dow_star: bool
    day_mode: str = 'or'

    @classmethod
    def parse(cls, expression, day_mode='or'):
        if not isinstance(expression, str) or len(expression) > 1024:
            raise CronError('cron expression must be a string of at most 1024 characters')
        expression = ALIASES.get(expression.strip().lower(), expression)
        parts = expression.split()
        if len(parts) != 5:
            raise CronError('expected five cron fields')
        if day_mode not in ('or', 'or-values', 'or-literal', 'and'):
            raise CronError('unknown day matching mode')
        m, h, d, mo, w = parts
        return cls(' '.join(parts), field_values(m, 0, 59), field_values(h, 0, 23),
                   field_values(d, 1, 31), field_values(mo, 1, 12, MONTHS),
                   frozenset(x % 7 for x in field_values(w, 0, 7, DAYS)),
                   d.startswith('*'), w.startswith('*'), day_mode)

    def matches_date(self, day):
        if day.month not in self.months:
            return False
        dom = day.day in self.month_days
        dow = (day.weekday() + 1) % 7 in self.week_days
        dom_wild, dow_wild = self.dom_star, self.dow_star
        if self.day_mode == 'or-values':
            dom_wild, dow_wild = len(self.month_days) == 31, len(self.week_days) == 7
        elif self.day_mode == 'or-literal':
            parts = self.expr.split()
            dom_wild, dow_wild = parts[2] == '*', parts[4] == '*'
        if self.day_mode == 'and' or dom_wild or dow_wild:
            return dom and dow
        return dom or dow

    def matches(self, time: datetime):
        return self.matches_date(time) and time.hour in self.hours and time.minute in self.minutes

    def shifted(self, minutes):
        """A forward shift that cannot wrap into another hour/day."""
        if not 0 <= minutes <= 59 or max(self.minutes) + minutes > 59:
            raise CronError('shift would cross an hour boundary')
        parts = self.expr.split()
        parts[0] = ','.join(str(m + minutes) for m in sorted(self.minutes))
        return Cron.parse(' '.join(parts), self.day_mode)


def normalize_cron(expr, dialect='standard'):
    """Return (five-field expression, fixed second). Never guess a dialect."""
    if not isinstance(expr, str) or len(expr) > 1024:
        raise CronError('cron expression must be a string of at most 1024 characters')
    parts = expr.split()
    second = 0
    if dialect in ('n8n', 'windmill') and len(parts) in (6, 7):
        if len(parts) == 7 and (dialect != 'windmill' or parts[-1] != '*'):
            raise CronError('year constraints are unsupported')
        seconds = field_values(parts[0], 0, 59)
        if len(seconds) != 1:
            raise CronError('multiple firings per minute are unsupported')
        second = next(iter(seconds))
        parts = parts[1:6]
    cron = Cron.parse(' '.join(parts))
    return cron.expr, second
