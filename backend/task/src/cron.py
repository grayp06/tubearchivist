"""
functionality:
- parse and evaluate cron expressions for the task scheduler
- fields supported: minute, hour and day of week
"""

from datetime import datetime, timedelta

DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DAY_LABELS = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
LOOKBACK_DAYS = 8


def _parse_number(value: str, names: list[str] | None) -> int:
    """parse single value, by number or name"""
    value = value.strip().lower()
    if names and value[:3] in names and value.isalpha():
        return names.index(value[:3])

    if not value.isdigit():
        raise ValueError(f"invalid value '{value}'")

    return int(value)


def parse_field(
    expression: str,
    min_value: int,
    max_value: int,
    names: list[str] | None = None,
) -> set[int]:
    """parse cron field to all matching values"""
    expression = str(expression).strip()
    if not expression:
        raise ValueError("empty cron field")

    values: set[int] = set()
    for part in expression.split(","):
        base, _, step_str = part.partition("/")
        step = 1
        if step_str:
            if not step_str.isdigit() or int(step_str) < 1:
                raise ValueError(f"invalid step '{step_str}'")

            step = int(step_str)

        if base == "*":
            start, end = min_value, max_value
        elif "-" in base:
            start_str, end_str = base.split("-", maxsplit=1)
            start = _parse_number(start_str, names)
            end = _parse_number(end_str, names)
            if names and end < start:
                # named range wrapping around the week, like sat-sun
                end += 7
        elif step_str:
            raise ValueError(f"step needs a range or '*' in '{part}'")
        else:
            start = end = _parse_number(base, names)

        if start > end or start < min_value or end > max_value:
            raise ValueError(f"'{part}' out of range {min_value}-{max_value}")

        values.update(range(start, end + 1, step))

    return values


class CronSchedule:
    """represent a parsed cron schedule"""

    def __init__(self, minute: str, hour: str, day_of_week: str):
        self.minute = str(minute)
        self.hour = str(hour)
        self.day_of_week = str(day_of_week)
        self.minutes = parse_field(self.minute, 0, 59)
        self.hours = parse_field(self.hour, 0, 23)
        # sunday can be 0 or 7
        days = parse_field(self.day_of_week, 0, 7, names=DAY_NAMES)
        self.days = {i % 7 for i in days}

    @classmethod
    def from_string(cls, expression: str) -> "CronSchedule":
        """build from 'minute hour day_of_week' string"""
        fields = expression.split()
        if len(fields) != 3:
            raise ValueError("expected three cron schedule fields")

        return cls(*fields)

    def __str__(self) -> str:
        return f"{self.minute} {self.hour} {self.day_of_week}"

    @staticmethod
    def _weekday(date: datetime) -> int:
        """day of week where sunday is 0"""
        return (date.weekday() + 1) % 7

    def matches(self, date: datetime) -> bool:
        """check if date is a scheduled time"""
        return (
            date.minute in self.minutes
            and date.hour in self.hours
            and self._weekday(date) in self.days
        )

    def previous_run(self, now: datetime) -> datetime | None:
        """get latest scheduled time at or before now"""
        now = now.replace(second=0, microsecond=0)
        for days_back in range(LOOKBACK_DAYS):
            day = now - timedelta(days=days_back)
            if self._weekday(day) not in self.days:
                continue

            for hour in sorted(self.hours, reverse=True):
                for minute in sorted(self.minutes, reverse=True):
                    candidate = day.replace(hour=hour, minute=minute)
                    if candidate <= now:
                        return candidate

        return None

    def is_due(self, last_run_at: datetime | None, now: datetime) -> bool:
        """check if a scheduled time passed since last run"""
        previous = self.previous_run(now)
        if previous is None:
            return False

        if last_run_at is None:
            return self.matches(now)

        return last_run_at < previous

    @property
    def human_readable(self) -> str:
        """describe schedule in words"""
        if len(self.minutes) == 1 and len(self.hours) == 1:
            hour, minute = next(iter(self.hours)), next(iter(self.minutes))
            suffix = "AM" if hour < 12 else "PM"
            time_str = f"At {hour % 12 or 12:02d}:{minute:02d} {suffix}"
        elif len(self.hours) == 24:
            minutes = ", ".join(f"{i:02d}" for i in sorted(self.minutes))
            time_str = f"At minute {minutes} past every hour"
        else:
            minutes = ", ".join(f"{i:02d}" for i in sorted(self.minutes))
            hours = ", ".join(f"{i:02d}" for i in sorted(self.hours))
            time_str = f"At minute {minutes} past hour {hours}"

        if len(self.days) == 7:
            return f"{time_str}, every day"

        days = ", ".join(DAY_LABELS[i] for i in sorted(self.days))

        return f"{time_str}, only on {days}"
