"""
时间戳格式化 helper 单元测试

覆盖:
  - 第一条消息(无 prev): 总带 MM-DD
  - 同一天内: 只显示 HH:MM
  - 跨天: 补 MM-DD HH:MM
  - 缺时间戳(ts_ms=0): 不加前缀
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmag.agent import _format_time_label  # noqa: E402


def _ts(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute).timestamp() * 1000


class TestFormatTimeLabel:
    def test_first_message_includes_date(self):
        ts = _ts(2026, 6, 14, 14, 30)
        out = _format_time_label(ts, None)
        assert out == "[06-14 14:30]"

    def test_same_day_shows_only_time(self):
        prev = _ts(2026, 6, 14, 14, 0)
        cur = _ts(2026, 6, 14, 14, 30)
        out = _format_time_label(cur, prev)
        assert out == "[14:30]"

    def test_cross_day_includes_new_date(self):
        prev = _ts(2026, 6, 14, 23, 59)
        cur = _ts(2026, 6, 15, 0, 1)
        out = _format_time_label(cur, prev)
        assert out == "[06-15 00:01]"

    def test_zero_timestamp_returns_empty(self):
        # 老数据没有 create_at,不应加空前缀
        out = _format_time_label(0, None)
        assert out == ""
        out = _format_time_label(0, _ts(2026, 6, 14, 14, 0))
        assert out == ""

    def test_zero_padded(self):
        ts = _ts(2026, 1, 5, 9, 5)
        out = _format_time_label(ts, None)
        assert out == "[01-05 09:05]", f"月/日/时/分都应两位补零,实际: {out}"

    def test_consecutive_messages_collapse_to_time_only(self):
        """3 条连续消息,只有第一条带日期"""
        t1 = _ts(2026, 6, 14, 10, 0)
        t2 = _ts(2026, 6, 14, 11, 30)
        t3 = _ts(2026, 6, 14, 12, 45)
        l1 = _format_time_label(t1, None)
        l2 = _format_time_label(t2, t1)
        l3 = _format_time_label(t3, t2)
        assert l1 == "[06-14 10:00]"
        assert l2 == "[11:30]"
        assert l3 == "[12:45]"

    def test_year_boundary_uses_new_date(self):
        """跨年 (虽然罕见) 也走 cross_day 分支"""
        prev = _ts(2025, 12, 31, 23, 59)
        cur = _ts(2026, 1, 1, 0, 0)
        out = _format_time_label(cur, prev)
        assert out == "[01-01 00:00]"
