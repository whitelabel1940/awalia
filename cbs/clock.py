"""Local clock and deterministic time descriptions, shared by every caller."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta

_fixed = ContextVar("cbs_now", default=None)
WEEKDAYS = "月火水木金土日"


def now():
    return _fixed.get() or datetime.now().astimezone()


def parse(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone() if value.tzinfo else value.replace(tzinfo=now().tzinfo)


@contextmanager
def at(value):
    token = _fixed.set(parse(value))
    try:
        yield now()
    finally:
        _fixed.reset(token)


def iso():
    return now().isoformat()


def day(value, boundary_hour=4):
    return (parse(value) - timedelta(hours=boundary_hour)).date().isoformat()


def absolute(value):
    dt = parse(value)
    return f"{dt.year}/{dt.month}/{dt.day}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def relative(value, current=None):
    dt, current = parse(value), parse(current) if current else now()
    days = (current.date() - dt.date()).days
    md = f"{dt.month}/{dt.day}"
    if days < 0:
        return "明日" if days == -1 else f"{-days}日後（{md}）"
    if days == 0:
        if 0 <= (current - dt).total_seconds() < 3600:
            return "さっき"
        return "今朝" if dt.hour < 12 <= current.hour else "今日"
    if days == 1:
        return "昨日"
    if days < 7:
        return f"{days}日前"
    if days < 14:
        return "先週"
    if days < 60:
        return f"{round(days / 7)}週間前（{md}）"
    months = (current.year - dt.year) * 12 + current.month - dt.month
    if days < 365 and dt.year == current.year:
        return f"{max(2, months)}か月前（{dt.month}月）"
    season = "冬" if dt.month in (12, 1, 2) else "春" if dt.month < 6 else "夏" if dt.month < 9 else "秋"
    year = "去年" if current.year - dt.year == 1 else f"{dt.year}年"
    return f"{year}の{season}ごろ"


def season(month):
    return "冬" if month in (12, 1, 2) else "春" if month < 6 else "夏" if month < 9 else "秋"


def casual(value, current=None):
    """The loose way people say it in conversation; relative() stays the precise reference."""
    dt, current = parse(value), parse(current) if current else now()
    days = (current.date() - dt.date()).days
    if days < 0:
        ahead = -days
        if ahead == 1:
            return "明日"
        if ahead == 2:
            return "あさって"
        if ahead < 7:
            return f"今度の{WEEKDAYS[dt.weekday()]}曜"
        if ahead < 14:
            return "来週"
        return "来月ごろ" if (dt.year, dt.month) == (current.year, current.month + 1) or (current.month == 12 and dt.month == 1) else f"{dt.month}月ごろ"
    if days == 0:
        return relative(value, current)
    if days == 1:
        return "昨日"
    if days == 2:
        return "おととい"
    if days < 7:
        return "この間"
    if days < 14:
        return "先週"
    if days < 30:
        return "ちょっと前"
    if days < 60:
        return "1か月くらい前"
    if days < 365:
        same = season(dt.month) == season(current.month)
        if dt.year == current.year:
            return "数か月前" if same else f"{season(dt.month)}ごろ"
        return f"去年の{season(dt.month)}ごろ"
    return "去年ごろ" if current.year - dt.year == 1 else "何年か前"


def casual_gap(previous, current=None):
    """Loose wording for how long it has been; None when it is too short to mention."""
    if not previous:
        return None
    days = max(0, ((parse(current) if current else now()) - parse(previous)).total_seconds()) / 86400
    if days < 2:
        return None
    if days < 7:
        return "数日ぶり"
    if days < 14:
        return "1週間ぶりくらい"
    if days < 30:
        return "2、3週間ぶり"
    if days < 60:
        return "1か月ぶりくらい"
    return f"{int(days // 30)}か月ぶりくらい" if days < 365 else "すごく久しぶり"


def gap(previous, current=None):
    if not previous:
        return None
    seconds = max(0, ((parse(current) if current else now()) - parse(previous)).total_seconds())
    if seconds < 60:
        return "1分未満ぶり"
    if seconds < 3600:
        return f"{int(seconds // 60)}分ぶり"
    if seconds < 86400:
        return f"{int(seconds // 3600)}時間ぶり"
    return f"{int(seconds // 86400)}日ぶり"


def stamp(value, current=None):
    return {"iso": parse(value).isoformat(), "absolute": absolute(value), "relative": relative(value, current),
            "casual": casual(value, current)}


def calendar(value):
    """LLMs select supplied dates instead of performing calendar arithmetic."""
    dt = parse(value)
    monday = dt.date() - timedelta(days=dt.weekday())
    return {"おととい": (dt.date() - timedelta(days=2)).isoformat(),
            "一昨日": (dt.date() - timedelta(days=2)).isoformat(),
            "昨日": (dt.date() - timedelta(days=1)).isoformat(), "今日": dt.date().isoformat(),
            "明日": (dt.date() + timedelta(days=1)).isoformat(),
            **{f"{label}{weekday}曜": (monday + timedelta(days=offset + i)).isoformat()
               for label, offset in (("先週", -7), ("今週", 0), ("来週", 7))
               for i, weekday in enumerate(WEEKDAYS)},
            **{f"次の{weekday}曜": (dt.date() + timedelta(days=(i - dt.weekday()) % 7 or 7)).isoformat()
               for i, weekday in enumerate(WEEKDAYS)}}
