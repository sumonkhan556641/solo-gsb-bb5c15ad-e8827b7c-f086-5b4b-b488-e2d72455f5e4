from fastapi import APIRouter, Depends
from sqlite3 import Connection

from .. import crud
from ..database import get_db, get_preflight_conn
from ..schemas import (
    SubmissionIn,
    SubmissionPreflightIn,
    SubmissionPreflightResult,
    SubmissionResult,
)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])


@router.post("", response_model=SubmissionResult, status_code=201)
def submit_batch(body: SubmissionIn, db: Connection = Depends(get_db)):
    """实验室一次提交仪器、证书与父子溯源边；全部按唯一编码 upsert。"""
    try:
        return crud.submit(db, body.instruments, body.certificates, body.edges)
    except crud.ValidationError as exc:
        # 在路由层转换，以便 422 响应体带中文原因
        from fastapi import HTTPException

        raise HTTPException(422, str(exc))


@router.post(
    "/preflight",
    response_model=SubmissionPreflightResult,
    status_code=200,
    summary="批量提交预检（dry-run，不写库）",
)
def submit_preflight(body: SubmissionPreflightIn):
    """接收与 ``POST /api/submissions`` 相同的载荷，在**独立事务**中按提交顺序
    （仪器 → 证书 → 边）模拟整批 upsert，执行引用、区间、自环与签名摘要校验，
    事务最终整体回滚，**绝不写入数据库**。

    返回稳定排序的错误路径（``path``，JSON Pointer 风格，如
    ``certificates/0/instrument_code``）、错误码、中文细节，以及若正式提交将
    执行 upsert 的条目数量（``would_upsert``）。

    ``strict_digest=true`` 时，显式 ``signature_digest`` 与重算值不符按错误
    计；默认（``false``）仅列为警告，不阻断提交。
    """
    with get_preflight_conn() as conn:
        return crud.preflight(
            conn,
            body.instruments,
            body.certificates,
            body.edges,
            strict_digest=body.strict_digest,
        )
