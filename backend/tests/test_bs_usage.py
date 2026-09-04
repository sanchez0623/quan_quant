# -*- coding: utf-8 -*-
"""bs_usage 黑名单记账测试：官方账本对账（wd-blacklist-stats）为唯一事实源。

背景：本地按"次数×6h"估算冻结期 + mark_released 登录成功即改写释放时间，
曾把限制期内后续被拒误判为新事件而虚增计数（本地 3 次 vs 官方 2 次）。
现为：record_blacklist 优先官方对账（同事件不 +1、新事件取官方 total、
不可达回退本地估算）；mark_released 仅刷新检查时间不改写释放期。
"""
import sqlite3
from datetime import datetime

import pytest

from app import db
from app.data import bs_usage


@pytest.fixture(autouse=True)
def _bl_env():
    """建表 + 每用例前后清 bs_blacklist（防前置测试污染/污染后续）"""
    db.init_db()
    with db.conn() as c:
        c.execute("DELETE FROM bs_blacklist")
    yield
    with db.conn() as c:
        c.execute("DELETE FROM bs_blacklist")


def _seed_local(detected_day: str, freeze_count: int, release_at: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO bs_blacklist(ip,freeze_count,detected_at,release_at,"
            "last_check) VALUES(?,?,?,?,?)",
            ("1.2.3.4", freeze_count, detected_day, release_at, detected_day))


def _rows() -> list[dict]:
    with db.conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in
                c.execute("SELECT * FROM bs_blacklist ORDER BY id")]


OFFICIAL_SAME = {"total": 2, "latest_date": "2026-09-03",
                 "latest_release": "2026-09-03 23:10:00.0"}
OFFICIAL_NEW = {"total": 3, "latest_date": "2026-09-05",
                "latest_release": "2026-09-05 18:00:00.0"}


def test_official_same_event_no_double_count(monkeypatch):
    """官方最新记录日期 == 本地最近检测日 => 同一事件：不插新行，
    freeze_count 对齐官方 total，release_at 用官方精确释放时间"""
    _seed_local("2026-09-03 11:01:14", 2, "2026-09-03 20:18:06")
    monkeypatch.setattr(bs_usage, "_fetch_official_blacklist",
                        lambda ip: OFFICIAL_SAME)
    out = bs_usage.tracker.record_blacklist("1.2.3.4")
    assert out["freeze_count"] == 2 and out["release_at"] == "2026-09-03 23:10:00"
    rows = _rows()
    assert len(rows) == 1, "同事件不得插入新行（虚增计数）"
    assert rows[0]["freeze_count"] == 2
    assert rows[0]["release_at"] == "2026-09-03 23:10:00"


def test_official_new_event_uses_official_total(monkeypatch):
    """官方最新记录日期 != 本地最近检测日 => 新事件：插新行，
    freeze_count 直接采用官方 yearlyRestrictCount"""
    _seed_local("2026-09-03 11:01:14", 2, "2026-09-03 23:10:00")
    monkeypatch.setattr(bs_usage, "_fetch_official_blacklist",
                        lambda ip: OFFICIAL_NEW)
    out = bs_usage.tracker.record_blacklist("1.2.3.4")
    assert out["freeze_count"] == 3
    rows = _rows()
    assert len(rows) == 2
    assert rows[-1]["freeze_count"] == 3
    assert rows[-1]["detected_at"] == "2026-09-05"
    assert rows[-1]["release_at"] == "2026-09-05 18:00:00"


def test_official_unavailable_falls_back_local(monkeypatch):
    """官方接口不可达 => 回退本地估算：n=本地 freeze_count+1，
    release_at=now+次数×6h"""
    _seed_local("2026-09-03 11:01:14", 2, "2026-09-03 23:10:00")
    monkeypatch.setattr(bs_usage, "_fetch_official_blacklist",
                        lambda ip: None)
    out = bs_usage.tracker.record_blacklist("1.2.3.4")
    assert out["freeze_count"] == 3
    rows = _rows()
    assert len(rows) == 2
    est = datetime.fromisoformat(rows[-1]["release_at"])
    assert (est - datetime.fromisoformat(rows[-1]["detected_at"])).total_seconds() \
        == pytest.approx(3 * 6 * 3600)


def test_mark_released_keeps_release_at(monkeypatch):
    """登录成功只刷新检查时间，不改写 release_at——
    提前抹掉释放期曾把限制期内后续被拒误判为新事件（实测虚增）"""
    _seed_local("2026-09-03 11:01:14", 2, "2026-09-03 23:10:00")
    bs_usage.tracker.mark_released()
    rows = _rows()
    assert rows[0]["release_at"] == "2026-09-03 23:10:00", \
        "release_at 不得被 mark_released 改写"
    assert rows[0]["last_check"] > rows[0]["detected_at"]
