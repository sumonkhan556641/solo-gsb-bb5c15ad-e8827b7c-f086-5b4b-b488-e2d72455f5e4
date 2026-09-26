"""计量校准溯源 API 入口。"""
from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import get_conn_ctx, init_db
from .routers import (
    calibration,
    certificates,
    edges,
    export,
    instruments,
    maintenance_windows,
    submissions,
    trace,
)
from .seed import DEMO_AT, seed_demo


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("SEED_DEMO", "1") == "1":
        with get_conn_ctx() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM instruments").fetchone()["n"]
            if n == 0:
                seed_demo(conn)
    yield


app = FastAPI(
    title="计量校准溯源 API",
    description=(
        "接收实验室仪器、校准证书、父子溯源关系及有效区间；按指定时点重建"
        "从仪器到国家基准的唯一证书链，校验区间连续性、签名摘要与测量不确定度，"
        "并在多候选链或区间断裂时返回可解释的冲突节点。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_non_finite(obj: Any) -> Any:
    """把错误详情中的非有限浮点（NaN/±Inf）替换为字符串。

    请求体含 NaN/Infinity 时，校验错误的 ``input`` 字段会携带这些值，
    而 JSON 响应不允许序列化非有限浮点（``allow_nan=False``），
    不处理会把本应返回 422 的请求变成 500。
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """与 FastAPI 默认处理器一致返回 422，但先清洗非有限浮点。"""
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(_sanitize_non_finite(exc.errors()))
        },
    )

app.include_router(instruments.router)
app.include_router(certificates.router)
app.include_router(edges.router)
app.include_router(submissions.router)
app.include_router(trace.router)
app.include_router(calibration.router)
app.include_router(maintenance_windows.router)
app.include_router(export.router)
app.include_router(export.admin_router)


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "metrological-traceability-api",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "chain_query": "/api/trace/{instrument}?at=YYYY-MM-DDTHH:MM:SSZ",
            "validation_report": "/api/reports/{instrument}",
            "time_window_coverage": (
                "/api/coverage/{instrument}"
                "?start=YYYY-MM-DDTHH:MM:SSZ&end=YYYY-MM-DDTHH:MM:SSZ"
            ),
            "json_export": "/api/export/{instrument}",
            "batch_submission": "POST /api/submissions",
            "batch_submission_preflight": (
                "POST /api/submissions/preflight（dry-run，不写库）"
            ),
            "certificate_revision_history": (
                "GET /api/certificates/{id}/revisions"
                "（首次保存为 v1，业务字段变化才追加连续版本）"
            ),
            "certificate_revision_diff": (
                "GET /api/certificates/{id}/revisions/diff"
                "?from_version=&to_version=（缺失版本 404）"
            ),
            "certificate_deactivation_impact": (
                "POST /api/assessments/certificate-deactivation"
            ),
            "batch_common_traceability": (
                "POST /api/assessments/common-traceability"
                "（多仪器共同溯源核查，只读，不写库）"
            ),
            "measurement_compatibility": (
                "POST /api/assessments/measurement-compatibility"
                "（多仪器读数 En 数相容性评估，只读，不写库）"
            ),
            "calibration_slot_query": (
                "GET /api/calibration-slots?instrument=&start=&end=&duration_seconds="
            ),
            "calibration_booking": (
                "POST /api/calibration-bookings"
                "（同一写事务内重查占用与证书截止，冲突 409）"
            ),
            "calibration_booking_reschedule": (
                "POST /api/calibration-bookings/reschedule"
                "（2～10 条已有预约批量改期，同一写事务核验，冲突 409 整体回滚）"
            ),
            "calibration_booking_list": "GET /api/calibration-bookings?instrument=",
            "calibration_booking_inspection": (
                "GET /api/calibration-bookings/inspection?start=&end="
                "（已确认预约有效性巡检：UTC 半开窗筛选，逐条在开始时刻"
                "只读复核唯一有效链与证书快照，valid/changed/invalid）"
            ),
            "calibration_waitlist_register": (
                "POST /api/calibration-bookings/{booking_id}/waitlist"
                "（登记候补，沿用该预约时段按登记先后排队）"
            ),
            "calibration_waitlist_query": (
                "GET /api/calibration-waitlist?booking_id=&instrument=&status="
            ),
            "calibration_booking_cancel": (
                "DELETE /api/calibration-bookings/{booking_id}"
                "（同一写事务释放占用并按候补队列补位，重复取消 404 不重复补位；"
                "补位重查避开生效维护窗口，跳过记录 STATION_UNDER_MAINTENANCE）"
            ),
            "calibration_maintenance_register": (
                "POST /api/calibration-maintenance-windows"
                "（UTC 半开区间登记工位维护封锁与原因；同一写事务内检查既有预约"
                "与维护窗口，半开重叠 409 并回冲突 ID，首尾相接允许）"
            ),
            "calibration_maintenance_list": (
                "GET /api/calibration-maintenance-windows?status=&start=&end="
                "（维护窗口列表；空档查询把维护窗口与预约一并视作占用）"
            ),
            "calibration_maintenance_revoke": (
                "DELETE /api/calibration-maintenance-windows/{window_id}"
                "（撤销维护封锁，软删除；重复撤销 404）"
            ),
        },
        "demo_query_time": DEMO_AT,
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
