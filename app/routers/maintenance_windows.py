"""校准工位维护封锁路由。

* ``POST   /api/calibration-maintenance-windows``        登记维护窗口（同一写事务内查重，冲突 409）；
* ``GET    /api/calibration-maintenance-windows``        维护窗口列表（可按状态/时间窗筛选）；
* ``DELETE /api/calibration-maintenance-windows/{id}``   撤销维护窗口（软删除，重复撤销 404）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import crud, maintenance
from ..database import get_conn_ctx, get_db
from ..schemas import MaintenanceWindowIn, MaintenanceWindowOut

router = APIRouter(prefix="/api", tags=["calibration-maintenance"])


def _conflict_http(exc: maintenance.MaintenanceConflict) -> HTTPException:
    """维护写事务冲突 → HTTP 响应：窗口不存在/已撤销为 404，其余为 409。"""
    status = (
        404 if exc.code == maintenance.CONFLICT_WINDOW_NOT_FOUND else 409
    )
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


@router.post(
    "/calibration-maintenance-windows",
    response_model=MaintenanceWindowOut,
    status_code=201,
    summary="登记校准工位维护封锁窗口（同一写事务内查重，冲突 409）",
)
def register_maintenance_window(body: MaintenanceWindowIn):
    """登记维护窗口：在**同一写事务**（``BEGIN IMMEDIATE``）内按半开区间
    ``[start, end)`` 同时检查**既有预约**与**生效中维护窗口**：

    * 半开重叠判据 ``b.start < end AND b.end > start``；因此新窗口起点恰等于
      既有占用结束点（首尾相接，如维护 ``[10:00,11:00)`` 接预约
      ``[09:00,10:00)``）**允许登记**；
    * 任一时段重叠即返回 **409**（``STATION_UNDER_MAINTENANCE``），响应
      ``detail`` 同时给出全部 ``conflicting_bookings``（冲突预约 ID 与时段）
      与 ``conflicting_maintenance_windows``（冲突窗口 ID、时段与原因），
      事务回滚、**绝不写入**；
    * ``start >= end`` 返回 **422**，``reason`` 为空白返回 **422**。

    全部通过才在同一事务内原子写入 SQLite，返回生效中（``active``）窗口。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return maintenance.register_window(
                conn, body.start, body.end, body.reason
            )
    except maintenance.MaintenanceConflict as exc:
        raise _conflict_http(exc)
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.get(
    "/calibration-maintenance-windows",
    response_model=list[MaintenanceWindowOut],
    summary="维护封锁窗口列表（可按状态/时间窗筛选，按起点升序）",
)
def list_maintenance_windows(
    status: Optional[str] = Query(
        None, description="按状态筛选：active 生效中 | revoked 已撤销；留空返回全部"
    ),
    start: Optional[datetime] = Query(
        None, description="仅返回与该时刻之后仍占用（半开 end > start）的窗口"
    ),
    end: Optional[datetime] = Query(
        None, description="仅返回在该时刻之前已开始（半开 start < end）的窗口"
    ),
    db: Connection = Depends(get_db),
):
    """维护窗口列表，按起点（``start_at``）升序返回。

    可组合 ``status`` / ``start`` / ``end`` 筛选：同时给出 ``start`` 与
    ``end`` 时按与半开查询窗 ``[start, end)`` 相交过滤；``status`` 取值
    非法返回 **422**。撤销（``revoked``）窗口仍可查，用于追溯。
    """
    if status is not None and status not in maintenance.WINDOW_STATUSES:
        raise HTTPException(
            422,
            f"未知维护窗口状态: {status}（支持: "
            f"{sorted(maintenance.WINDOW_STATUSES)}）",
        )
    return maintenance.list_windows(db, status=status, start=start, end=end)


@router.delete(
    "/calibration-maintenance-windows/{window_id}",
    response_model=MaintenanceWindowOut,
    summary="撤销维护封锁窗口（软删除；重复撤销 404）",
)
def revoke_maintenance_window(window_id: str):
    """撤销维护窗口：在**同一写事务**（``BEGIN IMMEDIATE``）内把窗口标记为
    ``revoked`` 并记录 ``revoked_at``（软删除，行保留可追溯）。

    撤销后该窗口**不再参与**空档占用计算与预约确认/批量改期/候补补位的
    封锁重查。窗口不存在或已撤销返回 **404**
    （``MAINTENANCE_WINDOW_NOT_FOUND``）——重复撤销不会重复处理。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return maintenance.revoke_window(conn, window_id)
    except maintenance.MaintenanceConflict as exc:
        raise _conflict_http(exc)
