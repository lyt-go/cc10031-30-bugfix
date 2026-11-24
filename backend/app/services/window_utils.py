"""窗口与交付申请的时间/测点解析工具。

归并窗口记录实际观测的起止时刻，交付申请只声明「测点 + 时间区间」。
这里集中处理两边的口径：时间戳容错解析、半开区间、测点范围匹配。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)

_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$")
_RANGE_SEP = re.compile(r"\s*(?:~|～|至|→|\.\.)\s*")


def parse_timestamp(value: Any) -> datetime | None:
    """尽量把业务里的各种时间写法解析成 naive datetime；解析不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    # 去掉带时区偏移的尾巴，窗口口径统一按 naïve 本地时间比较。
    text = re.sub(r"(?<=\d)(?:[+-]\d{2}:?\d{2})$", "", text)
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def to_iso(value: Any) -> str | None:
    dt = parse_timestamp(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def is_date_only(value: Any) -> bool:
    return bool(value) and bool(_DATE_ONLY.match(str(value).strip()))


def parse_time_range(text: Any, reference_date: datetime | None = None
                     ) -> tuple[datetime | None, datetime | None, str]:
    """解析交付申请的时间范围。

    返回 (start, end, basis)，区间语义为左闭右开 [start, end)。
    - 单日期 ``2026-09-01``：整天。
    - 单时刻 ``10:00``：一小时窗，避免吞掉当天剩余时间。
    - ``起~止`` / ``10:00-11:00``：止为纯日期时取次日零点为右边界。
    - 解析失败：basis=unparsed，不做时间裁剪（兼容历史无时间字段的记录）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None, None, "empty"

    parts = _RANGE_SEP.split(raw)
    if len(parts) == 1 and "," in raw:
        parts = [piece.strip() for piece in raw.split(",", 1)]
    if len(parts) == 1 and "-" in raw:
        # 「10:00-11:00」这种同日只写时段的申请。
        head, _, tail = raw.partition("-")
        if _TIME_ONLY.match(head.strip()) and _TIME_ONLY.match(tail.strip()):
            parts = [head.strip(), tail.strip()]
    if len(parts) == 1:
        start_text = parts[0]
        end_text = None
    else:
        start_text, end_text = parts[0].strip(), parts[1].strip()

    # 只写时刻的申请：锚定到 today（调用方可传入基准日），按时段解释。
    anchor = (reference_date or datetime.now()).date()
    if _TIME_ONLY.match(start_text):
        start_text = f"{anchor.isoformat()}T{_TIME_ONLY.match(start_text).group(0)}"
    if end_text is not None and _TIME_ONLY.match(end_text):
        end_text = f"{anchor.isoformat()}T{_TIME_ONLY.match(end_text).group(0)}"

    start = parse_timestamp(start_text)
    if start is None:
        return None, None, "unparsed"

    if end_text is None:
        if is_date_only(parts[0]):
            return start, start + timedelta(days=1), "date"
        if _TIME_ONLY.match(parts[0]):
            # 只有时刻：按一小时的时段理解，避免把当天剩余时间全算进来。
            return start, start + timedelta(hours=1), "time"
        return start, None, "open"

    end = parse_timestamp(end_text)
    if end is None:
        return start, None, "partially_unparsed"
    if is_date_only(parts[1]):
        end = end + timedelta(days=1)
        return start, end, "date"
    if _TIME_ONLY.match(parts[0]) and _TIME_ONLY.match(parts[1]):
        # 只写时段的申请：按「每天的 10:00~11:00」理解，对账时只看时刻。
        if end <= start:
            end = end + timedelta(days=1)
        return start, end, "time"
    return start, end, "datetime"


def parse_points(text: Any) -> list[str]:
    """把「测点范围」拆成测点模式列表，支持逗号/顿号/分号/空白分隔。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    return [piece for piece in re.split(r"[,，、;；\s]+", raw) if piece]


def point_matches(point: Any, patterns: list[str]) -> bool:
    """测点匹配：空申请表示不限制；支持 * / ? 通配。"""
    if not patterns:
        return True
    name = str(point or "").strip()
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if name.casefold() == pattern.casefold():
            return True
        if ("*" in pattern or "?" in pattern) and fnmatchcase(
                name.casefold(), pattern.casefold()):
            return True
    return False


def _anchor_clock(value: datetime | None) -> datetime | None:
    """只写时段的申请按「每天同一时刻」比较，统一锚到一个基准日。"""
    return value.replace(year=2000, month=1, day=1) if value is not None else None


def _clock_window(window_start: datetime, window_end: datetime,
                  request: dict[str, Any]) -> tuple[datetime, datetime]:
    if request.get("basis") != "time":
        return window_start, window_end
    start = _anchor_clock(window_start)
    end = _anchor_clock(window_end)
    if end < start:
        end = end + timedelta(days=1)
    return start, end


def _clock_request(request: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = request.get("start")
    end = request.get("end")
    if request.get("basis") != "time":
        return start, end
    start = _anchor_clock(start)
    end = _anchor_clock(end)
    if start is not None and end is not None and end <= start:
        end = end + timedelta(days=1)
    return start, end


def request_contains(stamp: datetime, request: dict[str, Any]) -> bool:
    """某个观测时刻是否落在申请区间内；时段申请按每天的时刻比较。"""
    start, end = _clock_request(request)
    if request.get("basis") == "time":
        stamp = _anchor_clock(stamp)
        # 跨零点时段（如 23:00-01:00）：零点后的观测归属前一天窗口。
        if start is not None and end is not None and start > end and stamp < start:
            stamp = stamp + timedelta(days=1)
    if start is not None and stamp < start:
        return False
    if end is not None and stamp >= end:
        return False
    return True


def request_overlaps(window_start: datetime, window_end: datetime,
                     request: dict[str, Any]) -> bool:
    """窗口实际起止是否与申请区间有交集。"""
    w_start, w_end = _clock_window(window_start, window_end, request)
    start, end = _clock_request(request)
    if start is not None and w_end < start:
        return False
    if end is not None and w_start >= end:
        return False
    return True


def request_covers(window_start: datetime, window_end: datetime,
                   request: dict[str, Any]) -> bool:
    """窗口是否完整落在申请区间内（整窗口径可以安全交付）。"""
    w_start, w_end = _clock_window(window_start, window_end, request)
    start, end = _clock_request(request)
    if start is not None and w_start < start:
        return False
    if end is not None and w_end >= end:
        return False
    return True
