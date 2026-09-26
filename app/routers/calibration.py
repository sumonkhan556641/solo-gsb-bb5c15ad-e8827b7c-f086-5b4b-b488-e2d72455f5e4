"""校准工位预约路由。

* ``GET  /api/calibration-slots``               只读：单工位最早可用时段（维护窗口与预约一并占用）；
* ``POST /api/calibration-bookings``            写事务内重查占用、维护封锁与证书截止后确认（409 冲突）；
* ``POST /api/calibration-bookings/reschedule`` 2～10 条已有预约批量改期（同一写事务核验，含维护封锁，409 整体回滚）；
* ``GET  /api/calibration-bookings``            按仪器（可选）查询已确认预约；
* ``GET  /api/calibration-bookings/inspection`` 已确认预约有效性巡检（UTC 半开窗筛选，逐条只读复核链与证书快照）；
* ``POST /api/calibration-bookings/{booking_id}/waitlist`` 对指定已确认预约登记候补（沿用该预约时段排队）；
* ``GET  /api/calibration-waitlist``            候补状态查询（按目标预约/仪器/状态筛选）；
* ``DELETE /api/calibration-bookings/{booking_id}`` 取消预约并在同一写事务内按候补队列补位（避开维护窗口；重复取消 404）。

维护封锁窗口的登记/列表/撤销见 ``routers/maintenance_windows.py``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import booking, crud
from ..database import get_conn_ctx, get_db
from ..schemas import (
    BookingInspectionOut,
    CalibrationBookingIn,
    CalibrationBookingOut,
    CalibrationCancelOut,
    CalibrationRescheduleIn,
    CalibrationSlotsOut,
    CalibrationWaitlistIn,
    CalibrationWaitlistOut,
)

router = APIRouter(prefix="/api", tags=["calibration-booking"])


@router.get(
    "/calibration-slots",
    response_model=CalibrationSlotsOut,
    summary="查询单个校准工位时间窗内最早可用时段",
)
def calibration_slots(
    instrument: str = Query(
        ..., description="受校准仪器编码（溯源链按查询窗起点判定）"
    ),
    start: datetime = Query(
        ...,
        alias="start",
        description="UTC 查询窗起点（含），如 2026-06-20T00:00:00Z",
    ),
    end: datetime = Query(
        ...,
        alias="end",
        description="UTC 查询窗终点，如 2026-06-21T00:00:00Z",
    ),
    duration_seconds: int = Query(
        ...,
        alias="duration_seconds",
        gt=0,
        description="所需校准时长（正整数秒）",
    ),
    db: Connection = Depends(get_db),
):
    """输入仪器、UTC 起止时间与所需时长，返回时间窗内**最早可确认时段**。

    计算规则：

    1. 全实验室仅**单个校准工位**，所有仪器共用一份占用；占用按
       **半开区间** ``[start_at, end_at)`` 计算，相接但不重叠的预约
       （如 ``[09,10)`` 与 ``[10,11)``）互不冲突；
    2. **生效中的维护封锁窗口与预约一并视作工位占用**（响应分别在
       ``busy`` 与 ``maintenance_windows`` 中列出），空档计算须同时避开；
    3. **查询起点**要求该仪器存在**唯一且校验通过**的溯源链（入口门禁；
       不可用时 ``slot=null``、``reason_code=TRACEABILITY_CHAIN_UNAVAILABLE``，
       完整证据见 ``trace_report``）；
    4. 在合并占用上贪心求物理最早空档，并**对每个候选起点重放与预约
       确认相同的门禁**——占用把建议起点推后后不沿用查询起点的旧结论：
       候选起点须唯一成链且候选结束不晚于该起点证书 ``valid_until``；
    5. 候选不可确认时，沿**证书与溯源边的有效期边界**继续寻找最早
       可确认时段（边界点为闭区间端点，即使落在占用段起点也单独重验；
       闭区间交接点不合格则从其后一个整秒继续）。响应中的证书、截止
       （``traceability_at`` / ``certificate_id`` /
       ``certificate_valid_until``）与 ``trace_report`` 均对应**实际
       建议起点**（无建议时为最后重验的候选起点）；
    6. 全窗无物理空档 -> ``NO_FREE_SLOT_IN_WINDOW``；全部候选均越证
       书截止且无换新链 -> ``CERTIFICATE_DEADLINE_TOO_EARLY``；边界
       逐点重验后仍存在结构性不可确认候选（无有效证书以外的链失效）
       -> ``NO_CONFIRMABLE_SLOT_IN_WINDOW``（``chain_reason_code``
       给出细分原因）。``busy`` / ``maintenance_windows`` 始终覆盖整个
       请求窗（搜索窗不再按单张证书截止裁剪）。
    """
    try:
        return booking.find_slots(db, instrument, start, end, duration_seconds)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/calibration-bookings",
    response_model=CalibrationBookingOut,
    status_code=201,
    summary="确认校准工位预约（同一写事务内重查，冲突 409）",
)
def create_calibration_booking(body: CalibrationBookingIn):
    """确认预约：在**同一写事务**（``BEGIN IMMEDIATE``）内重新检查：

    1. 仪器存在；
    2. 预约起点该仪器的唯一有效溯源链仍成立（重查，不沿用 GET 结果）；
    3. 预约结束（``start + duration_seconds``，半开）不晚于起点证书
       ``valid_until``；
    4. 单个工位在该半开区间内无其他预约（``b.start < end AND b.end > start``）；
    5. 该半开区间不与任何**生效中维护封锁窗口**重叠（首尾相接允许），
       冲突返回 ``STATION_UNDER_MAINTENANCE`` 并附全部冲突窗口。

    任一条件不成立返回 **409**，响应体带机器可读 ``code``、中文 ``message``
    与结构化 ``detail``（占用冲突时给出全部冲突预约），事务回滚、绝不写入。
    全部成立则在同一事务内原子写入 SQLite 并返回已确认预约。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.confirm_booking(
                conn,
                body.instrument_code,
                body.start,
                body.duration_seconds,
                body.note,
            )
    except booking.BookingConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/calibration-bookings/reschedule",
    response_model=list[CalibrationBookingOut],
    summary="批量改期已有预约（同一写事务内核验，冲突 409 整体回滚）",
)
def reschedule_calibration_bookings(body: CalibrationRescheduleIn):
    """批量改期 **2～10 条已有预约**：在**同一写事务**（``BEGIN IMMEDIATE``）内：

    1. 核对每条预约存在且预约 ID 不重复；
    2. 逐条以新起点（``duration_seconds`` 缺省沿用原时长）重新验证该仪器
       在新起点的唯一有效溯源链，且新结束（``start + duration_seconds``，
       半开）不晚于起点证书 ``valid_until``；
    3. 先把本批预约的**旧占用整体排除**，再按半开区间检查新时段**彼此**
       及**与其他预约**的冲突——因此允许本批预约交换时段，首尾相接
       （新起点恰等于另一段结束点）也不冲突。

    任一条件不成立返回 **409**，响应体 ``detail.booking_id`` 指明失败的
    预约 ID，并带机器可读 ``code``、中文 ``message`` 与结构化 ``detail``，
    事务整体回滚（整批预约保持改期前状态，绝不部分更新）。全部成立才在
    同一事务内更新各预约的时段、时长与证书快照；**原仪器、备注与创建
    时间保持不变**，响应按请求顺序返回更新后的预约。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.reschedule_bookings(
                conn,
                [
                    {
                        "booking_id": it.booking_id,
                        "start": it.start,
                        "duration_seconds": it.duration_seconds,
                    }
                    for it in body.items
                ],
            )
    except booking.BookingConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )


@router.get(
    "/calibration-bookings",
    response_model=list[CalibrationBookingOut],
    summary="查询校准工位预约（可按仪器筛选）",
)
def list_calibration_bookings(
    instrument: Optional[str] = Query(
        None, description="仅返回该仪器的预约；留空返回全部预约（按起点升序）"
    ),
    db: Connection = Depends(get_db),
):
    if instrument is not None and crud.get_instrument(db, instrument) is None:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    return booking.list_bookings(db, instrument)


@router.get(
    "/calibration-bookings/inspection",
    response_model=BookingInspectionOut,
    summary="已确认预约有效性巡检（UTC 半开窗筛选，逐条只读复核）",
)
def inspect_calibration_bookings(
    start: datetime = Query(
        ...,
        description="UTC 巡检查询窗起点（含），按预约开始时刻筛选",
    ),
    end: datetime = Query(
        ...,
        description="UTC 巡检查询窗终点（不含），须严格晚于起点",
    ),
    db: Connection = Depends(get_db),
):
    """对已确认校准预约做**只读**有效性巡检：

    1. 筛选**开始时刻**落在 UTC 半开查询窗 ``[start, end)`` 内的预约
       （开始时刻恰等于窗起点计入、恰等于窗终点不计入），按（开始时刻,
       预约 ID）稳定排序；
    2. 对每条预约在其**开始时刻**重建唯一有效溯源链（复用与确认/改期
       相同的链路门禁，不沿用预约保存的快照），要求链校验通过且预约
       **结束不晚于当前起点证书到期**（半开，``end == valid_until`` 允许）；
    3. 再与预约保存的证书 ID 与到期快照比较：

       * 链失效（仪器不存在 / 无有效证书 / 起点不唯一 / 多候选 / 断裂 /
         校验失败）或截止不足 -> ``invalid``（给出细分原因码）；
       * 链仍有效且截止充足，但证书 ID 或到期时刻变化 -> ``changed``
         （``CERTIFICATE_SNAPSHOT_CHANGED``）；
       * 其余 -> ``valid``。

    响应返回每条预约的状态、原因码、当前起点证书及确认时原快照，并在
    ``summary`` 汇总各状态数量。查询窗起点不早于终点（``start >= end``）
    返回 **422**；全程只读，不写库。
    """
    try:
        return booking.inspect_bookings(db, start, end)
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


def _conflict_http(exc: booking.BookingConflict) -> HTTPException:
    """预约写事务冲突 → HTTP 响应：目标预约不存在为 404，其余为 409。"""
    status = 404 if exc.code == booking.CONFLICT_BOOKING_NOT_FOUND else 409
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


@router.post(
    "/calibration-bookings/{booking_id}/waitlist",
    response_model=CalibrationWaitlistOut,
    status_code=201,
    summary="对指定已确认预约登记候补（沿用该预约时段，按登记先后排队）",
)
def register_calibration_waitlist(booking_id: str, body: CalibrationWaitlistIn):
    """登记候补：在**同一写事务**（``BEGIN IMMEDIATE``）内核验目标预约与
    候补仪器均存在后写入候补记录：

    * 候补**沿用该预约当前的起止时段**（快照保存），按登记先后
      （``seq`` 自增）排队，响应 ``queue_position`` 为当前队列名次；
    * 溯源链、证书截止与占用冲突**不在登记时校验**，而在目标预约取消时
      的补位事务内逐条重验；
    * 目标预约不存在返回 **404**，候补仪器不存在返回 **409**
      （``INSTRUMENT_NOT_FOUND``）。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.register_waitlist(
                conn, booking_id, body.instrument_code, body.note
            )
    except booking.BookingConflict as exc:
        raise _conflict_http(exc)


@router.get(
    "/calibration-waitlist",
    response_model=list[CalibrationWaitlistOut],
    summary="候补状态查询（按目标预约/仪器/状态筛选，按登记先后升序）",
)
def list_calibration_waitlist(
    booking_id: Optional[str] = Query(
        None,
        description="仅返回该目标预约的候补队列（目标预约已取消也可查）",
    ),
    instrument: Optional[str] = Query(
        None, description="仅返回该仪器的候补；仪器不存在返回 404"
    ),
    status: Optional[str] = Query(
        None,
        description="按状态筛选：waiting | promoted | failed | expired",
    ),
    db: Connection = Depends(get_db),
):
    """候补状态查询：按登记先后（``seq``）升序返回候补记录。

    ``queue_position`` 仅对候补中（``waiting``）条目给出其在目标预约
    队列中的 1 基名次；已补位（``promoted``）、重验不合格（``failed``）
    与已失效（``expired``）条目为 ``null``，其 ``status_reason`` 给出
    进入终态的中文原因。
    """
    if status is not None and status not in booking.WAITLIST_STATUSES:
        raise HTTPException(
            422,
            f"未知候补状态: {status}（支持: {sorted(booking.WAITLIST_STATUSES)}）",
        )
    if instrument is not None and crud.get_instrument(db, instrument) is None:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    return booking.list_waitlist(
        db, booking_id=booking_id, instrument_code=instrument, status=status
    )


@router.delete(
    "/calibration-bookings/{booking_id}",
    response_model=CalibrationCancelOut,
    summary="取消预约并在同一写事务内按候补队列补位（重复取消 404）",
)
def cancel_calibration_booking(booking_id: str):
    """取消预约：在**同一写事务**（``BEGIN IMMEDIATE``）内：

    1. 删除预约行、释放单工位占用；
    2. 依登记先后（``seq`` 升序）逐条**重验**候补中申请：仪器在时段
       起点的唯一有效溯源链、时段终点不晚于起点证书 ``valid_until``、
       与剩余预约无半开冲突；
    3. **跳过不合格申请**（标记 ``failed`` 并记录原因），只将**首个
       合格申请**转为已确认预约（沿用原时段与时长，保存起点证书快照
       与候补备注），该条目标记 ``promoted``；
    4. 排在中签者之后、未及评估的候补中条目标记 ``expired``。

    响应给出补位结果（``promotion``）、被跳过清单（``skipped``）与
    失效候补 id（``expired_waitlist_ids``）。预约不存在或已取消返回
    **404**——**重复取消不会重复补位**。
    """
    try:
        with get_conn_ctx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return booking.cancel_booking(conn, booking_id)
    except booking.BookingConflict as exc:
        raise _conflict_http(exc)
