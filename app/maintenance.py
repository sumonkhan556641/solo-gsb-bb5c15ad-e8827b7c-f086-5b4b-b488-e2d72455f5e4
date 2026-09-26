"""校准工位维护封锁：维护窗口登记、列表、撤销与半开占用重查。

业务规则
--------
* 全实验室只有**单个校准工位**：维护窗口与预约一样按半开区间
  ``[start_at, end_at)`` 占用工位——相接但不重叠的两段
  （如维护 ``[08:00,09:00)`` 与预约 ``[09:00,10:00)``）不冲突，
  可首尾相接。
* **登记**（``register_window``）：调用方必须提供已执行
  ``BEGIN IMMEDIATE`` 的连接；在**同一写事务**内同时检查既有预约与
  活跃维护窗口，任一半开重叠即抛 :class:`MaintenanceConflict`（路由
  层映射为 409）并带全部冲突 ID，事务回滚、绝不写入；首尾相接允许。
* **空档查询**（``active_windows`` / ``overlapping_windows``）：维护
  窗口与预约一并视作工位占用。
* **撤销**（``revoke_window``）为软删除：窗口行保留用于状态追溯，
  仅把 ``status`` 置为 ``revoked``；所有占用重查只看 ``active`` 窗口，
  因此撤销后历史窗口不再封锁工位。重复撤销（含不存在的 ID）抛
  :class:`MaintenanceConflict`（``WINDOW_NOT_FOUND``，路由层 404）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any, Optional

from .crud import ValidationError
from .utils import fmt, to_utc_naive, utcnow_str

# 登记写事务冲突原因码
CONFLICT_STATION_UNDER_MAINTENANCE = "STATION_UNDER_MAINTENANCE"
CONFLICT_WINDOW_NOT_FOUND = "MAINTENANCE_WINDOW_NOT_FOUND"

# 维护窗口状态：active 生效中 | revoked 已撤销（软删除）
WINDOW_ACTIVE = "active"
WINDOW_REVOKED = "revoked"
WINDOW_STATUSES = (WINDOW_ACTIVE, WINDOW_REVOKED)

_WINDOW_FIELDS = (
    "id",
    "start_at",
    "end_at",
    "reason",
    "status",
    "created_at",
    "revoked_at",
)


class MaintenanceConflict(Exception):
    """维护窗口写事务内重查失败（路由层映射为 HTTP 409/404）。"""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------- 工具

def _window_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _WINDOW_FIELDS}


def _get_window_row(
    conn: sqlite3.Connection, window_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM station_maintenance_windows WHERE id=?", (window_id,)
    ).fetchone()


def insert_window(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    reason: str,
    window_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """底层写入生效中维护窗口（供测试播种与演示数据复用），不做任何查重。

    正式登记必须走 :func:`register_window`（同一写事务内与预约/其他维护
    窗口半开查重）；本函数用于构造 API 不可能产生的历史重叠数据。
    """
    start_s, end_s = fmt(to_utc_naive(start)), fmt(to_utc_naive(end))
    if window_id is None:
        window_id = (
            "MW-" + datetime.utcnow().strftime("%Y%m%d")
            + "-" + uuid.uuid4().hex[:12]
        )
    conn.execute(
        """
        INSERT INTO station_maintenance_windows(
            id, start_at, end_at, reason, status, created_at, revoked_at)
        VALUES (:id, :start_at, :end_at, :reason, :status, :created_at, NULL)
        """,
        {
            "id": window_id,
            "start_at": start_s,
            "end_at": end_s,
            "reason": reason,
            "status": WINDOW_ACTIVE,
            "created_at": created_at or utcnow_str(),
        },
    )
    return _window_dict(_get_window_row(conn, window_id))


def active_windows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全部生效中（active）维护窗口，按起点升序。"""
    return conn.execute(
        "SELECT * FROM station_maintenance_windows "
        "WHERE status='active' ORDER BY start_at, id"
    ).fetchall()


def overlapping_windows(
    conn: sqlite3.Connection,
    lo_s: str,
    hi_s: str,
    *,
    exclude_id: Optional[str] = None,
) -> list[sqlite3.Row]:
    """与半开查询窗 ``[lo, hi)`` 相交的**生效中**维护窗口。

    半开重叠判据 ``w.start < hi AND w.end > lo``：恰在 ``lo`` 结束或
    恰在 ``hi`` 开始的窗口不算重叠（首尾相接允许）。``exclude_id``
    用于排除窗口自身（当前登记流程不需要，保留以备用）。
    """
    sql = (
        "SELECT * FROM station_maintenance_windows "
        "WHERE status='active' AND start_at < ? AND end_at > ?"
    )
    params: list[Any] = [hi_s, lo_s]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY start_at, id"
    return conn.execute(sql, params).fetchall()


def _overlapping_bookings(
    conn: sqlite3.Connection, lo_s: str, hi_s: str
) -> list[sqlite3.Row]:
    """与半开查询窗相交的既有预约（登记封锁时查重用，单工位不分仪器）。"""
    return conn.execute(
        "SELECT * FROM calibration_bookings WHERE start_at < ? AND end_at > ? "
        "ORDER BY start_at, id",
        (hi_s, lo_s),
    ).fetchall()


# ---------------------------------------------------------------- 登记 / 列表 / 撤销

def register_window(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    reason: str,
) -> dict[str, Any]:
    """在**同一写事务**内登记工位维护封锁窗口。

    调用方必须提供已执行 ``BEGIN IMMEDIATE`` 的连接；本函数只做检查与
    插入，COMMIT/ROLLBACK 由调用方（路由层上下文）负责。流程：

    1. 校验 ``start < end``（半开区间，UTC）；
    2. 同一事务内查重：与既有**预约**或**生效中维护窗口**任一半开
       重叠（``w.start < end AND w.end > start``）即抛
       :class:`MaintenanceConflict`（409），detail 同时给出全部冲突
       预约与冲突窗口 ID；首尾相接允许；
    3. 无冲突才原子写入，事务回滚时绝不留下窗口。
    """
    start_naive = to_utc_naive(start)
    end_naive = to_utc_naive(end)
    if start_naive >= end_naive:
        raise ValidationError("维护窗口起点 start 必须严格早于终点 end")
    start_s, end_s = fmt(start_naive), fmt(end_naive)

    # 同一写事务内检查现有预约与活跃维护窗口（半开，首尾相接允许）
    bookings = _overlapping_bookings(conn, start_s, end_s)
    windows = overlapping_windows(conn, start_s, end_s)
    if bookings or windows:
        raise MaintenanceConflict(
            CONFLICT_STATION_UNDER_MAINTENANCE,
            f"维护窗口 {start_s} ~ {end_s}（半开区间）与单工位上 "
            f"{len(bookings)} 条既有预约、{len(windows)} 个生效维护窗口"
            "存在重叠，封锁登记失败（首尾相接允许）",
            {
                "window_start": start_s,
                "window_end": end_s,
                "conflicting_bookings": [
                    {
                        "booking_id": b["id"],
                        "instrument_code": b["instrument_code"],
                        "start_at": b["start_at"],
                        "end_at": b["end_at"],
                    }
                    for b in bookings
                ],
                "conflicting_maintenance_windows": [_window_dict(w) for w in windows],
            },
        )

    window_id = (
        "MW-" + datetime.utcnow().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    )
    now = utcnow_str()
    conn.execute(
        """
        INSERT INTO station_maintenance_windows(
            id, start_at, end_at, reason, status, created_at, revoked_at)
        VALUES (:id, :start_at, :end_at, :reason, :status, :created_at, NULL)
        """,
        {
            "id": window_id,
            "start_at": start_s,
            "end_at": end_s,
            "reason": reason,
            "status": WINDOW_ACTIVE,
            "created_at": now,
        },
    )
    return _window_dict(_get_window_row(conn, window_id))


def list_windows(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """维护窗口列表：可按状态与时间窗（半开相交）筛选，按起点升序。

    时间筛选对**任意状态**窗口按半开区间 ``start < end AND w.end > start``
    判定相交；不传状态则返回全部状态（active/revoked）。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if start is not None and end is not None:
        clauses.append("start_at < ? AND end_at > ?")
        params.extend((fmt(to_utc_naive(end)), fmt(to_utc_naive(start))))
    elif start is not None:
        clauses.append("end_at > ?")
        params.append(fmt(to_utc_naive(start)))
    elif end is not None:
        clauses.append("start_at < ?")
        params.append(fmt(to_utc_naive(end)))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM station_maintenance_windows{where} "
        "ORDER BY start_at, id",
        params,
    ).fetchall()
    return [_window_dict(r) for r in rows]


def revoke_window(conn: sqlite3.Connection, window_id: str) -> dict[str, Any]:
    """撤销维护封锁窗口（软删除，同一写事务内完成）。

    调用方必须提供已执行 ``BEGIN IMMEDIATE`` 的连接。窗口不存在或已
    撤销即抛 :class:`MaintenanceConflict`（``WINDOW_NOT_FOUND``，路由层
    404）——重复撤销不会重复处理。撤销后 ``status=revoked``、记录
    ``revoked_at``，占用重查不再计入该窗口，但行保留可追溯。
    """
    row = _get_window_row(conn, window_id)
    if row is None or row["status"] != WINDOW_ACTIVE:
        raise MaintenanceConflict(
            CONFLICT_WINDOW_NOT_FOUND,
            f"维护窗口不存在或已撤销: {window_id}（撤销不重复执行）",
            {"maintenance_window_id": window_id},
        )
    now = utcnow_str()
    conn.execute(
        "UPDATE station_maintenance_windows "
        "SET status='revoked', revoked_at=? WHERE id=?",
        (now, window_id),
    )
    return _window_dict(_get_window_row(conn, window_id))
