import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..engine import build_report

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/export/{instrument}")
def export_report(
    instrument: str,
    at: Optional[datetime] = None,
    db: Connection = Depends(get_db),
):
    """JSON 导出：以附件下载形式返回完整校验报告。"""
    try:
        report = build_report(db, instrument, at)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    body = json.dumps(report, ensure_ascii=False, indent=2)
    filename = f"trace-report-{instrument}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Trace-Resolution": report["resolution"],
            "X-Trace-Valid": str(report["valid"]).lower(),
        },
    )


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_router.post("/reset", status_code=204)
def reset_all(db: Connection = Depends(get_db)):
    """清空全部数据（演示/测试用）。"""
    crud.reset(db)
    return Response(status_code=204)
