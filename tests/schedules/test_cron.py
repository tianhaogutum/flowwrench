from datetime import datetime
import unittest
from flowwrench.schedules.cron import Cron, CronError, normalize_cron


class CronTests(unittest.TestCase):
    def test_parser_table(self):
        cases = [
            ('* * * * *', '2026-11-01T12:34', True),
            ('*/15 9-17 * JAN,NOV MON-FRI', '2026-11-02T09:15', True),
            ('*/15 9-17 * JAN,NOV MON-FRI', '2026-11-01T09:15', False),
            ('5,20,45 0,12 1-10/2 * *', '2026-11-03T12:20', True),
            ('5,20,45 0,12 1-10/2 * *', '2026-11-04T12:20', False),
            ('0/20 * * * *', '2026-11-01T00:40', True),
            ('5/20 * * * *', '2026-11-01T00:45', True),
            ('5/20 * * * *', '2026-11-01T00:40', False),
            ('0 0 * * 7', '2026-11-01T00:00', True),
            ('@hourly', '2026-11-01T05:00', True),
            ('@weekly', '2026-11-02T00:00', False),
            ('0 0 29 FEB *', '2028-02-29T00:00', True),
            ('0 0 1 * MON', '2026-11-02T00:00', True),
            ('0 0 1 * MON', '2026-11-01T00:00', True),
            ('0 0 */2 * MON', '2026-11-03T00:00', False),
            ('0 0 */2 * MON', '2026-11-09T00:00', True),
        ]
        for expr, value, expected in cases:
            with self.subTest(expr=expr, value=value):
                self.assertEqual(Cron.parse(expr).matches(datetime.fromisoformat(value)), expected)

    def test_invalid_table(self):
        for expr in ['60 * * * *', '0 24 * * *', '0 0 0 * *', '0 0 * 13 *',
                     '0 0 * * 8', '*/0 * * * *', '*/61 * * * *', '1-0 * * * *',
                     '0 0 L * *', '0 0 ? * MON#2', '0 0 1W * *', 'H * * * *',
                     '0 0 * * FRI-MON', '0,,1 * * * *', '* * * *', '@reboot',
                     '0 0 * * * 2027', '0/1/2 * * * *', '１ * * * *',
                     '__import__("os") * * * *', '', None, '*' * 1025]:
            with self.subTest(expr=expr):
                with self.assertRaises(CronError):
                    Cron.parse(expr)

    def test_day_and_semantics(self):
        value = datetime(2026, 11, 2)
        self.assertTrue(Cron.parse('0 0 1 * MON').matches(value))
        self.assertFalse(Cron.parse('0 0 1 * MON', 'and').matches(value))

    def test_dialects_and_seconds(self):
        self.assertEqual(normalize_cron('17 0 * * * *', 'n8n'), ('0 * * * *', 17))
        self.assertEqual(normalize_cron('0 30 2 * * * *', 'windmill'), ('30 2 * * *', 0))
        for expr, dialect in [('*/30 * * * * *', 'n8n'), ('0 0 0 * * * 2027', 'windmill'),
                              ('0 0 0 * * * *', 'n8n'), ('0 0 * * * *', 'standard')]:
            with self.subTest(expr=expr, dialect=dialect):
                with self.assertRaises(CronError):
                    normalize_cron(expr, dialect)

    def test_shift_preserves_other_fields(self):
        cron = Cron.parse('0,15,30 3 1 * MON', 'and')
        shifted = cron.shifted(10)
        self.assertEqual(shifted.expr, '10,25,40 3 1 * MON')
        self.assertEqual(shifted.day_mode, 'and')
        with self.assertRaises(CronError):
            Cron.parse('55 * * * *').shifted(10)

    def test_platform_day_matching_modes(self):
        nov3 = datetime(2026, 11, 3)  # Tuesday, odd day of month
        expr = '0 0 */2 * MON'
        self.assertFalse(Cron.parse(expr).matches(nov3))
        self.assertTrue(Cron.parse(expr, 'or-values').matches(nov3))
        self.assertTrue(Cron.parse(expr, 'or-literal').matches(nov3))
        nov2 = datetime(2026, 11, 2)  # Monday
        self.assertFalse(Cron.parse('0 0 1-31 * TUE', 'or-values').matches(nov2))
        self.assertTrue(Cron.parse('0 0 1-31 * TUE', 'or-literal').matches(nov2))
