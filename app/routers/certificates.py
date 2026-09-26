from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..schemas import (
    CertificateIn,
    CertificateOut,
    CertificateRevisionDiffOut,
    CertificateRevisionListOut,
)

router = APIRouter(prefix="/api/certificates", tags=["certificates"])


def _row(r):
    return {k: r[k] for k in (
        "id", "instrument_code", "cert_number", "issued_at", "valid_from",
        "valid_until", "uncertainty", "unit", "digest_alg",
        "signature_digest", "signed_payload", "created_at",
    )}


def _revision_row(r):
    return {k: r[k] for k in (
        "certificate_id", "version", "instrument_code", "cert_number",
        "issued_at", "valid_from", "valid_until", "uncertainty", "unit",
        "digest_alg", "signature_digest", "signed_payload", "recorded_at",
    )}


@router.post("", response_model=CertificateOut, status_code=201)
def create_or_update_certificate(
    body: CertificateIn, db: Connection = Depends(get_db)
):
    try:
        crud.upsert_certificate(db, body)
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))
    return _row(crud.get_certificate(db, body.id))


@router.get("", response_model=list[CertificateOut])
def list_certificates(
    instrument_code: str | None = None, db: Connection = Depends(get_db)
):
    return [_row(r) for r in crud.list_certificates(db, instrument_code)]


@router.get("/{cert_id}", response_model=CertificateOut)
def get_certificate(cert_id: str, db: Connection = Depends(get_db)):
    row = crud.get_certificate(db, cert_id)
    if row is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    return _row(row)


@router.get(
    "/{cert_id}/revisions",
    response_model=CertificateRevisionListOut,
    summary="证书修订历史：按证书 ID 查询连续版本列表",
)
def list_certificate_revisions(cert_id: str, db: Connection = Depends(get_db)):
    """返回该证书的全部版本快照（按版本号升序）。

    首次按 ID 保存时记录 v1；存量证书首次更新前补录旧值为 v1；
    仅业务字段实际变化才追加连续版本，同内容重交不增版。
    """
    if crud.get_certificate(db, cert_id) is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    rows = crud.list_certificate_revisions(db, cert_id)
    return {
        "certificate_id": cert_id,
        "revision_count": len(rows),
        "revisions": [_revision_row(r) for r in rows],
    }


@router.get(
    "/{cert_id}/revisions/diff",
    response_model=CertificateRevisionDiffOut,
    summary="证书修订历史：指定两版的字段差异",
)
def diff_certificate_revisions(
    cert_id: str,
    from_version: int = Query(..., ge=1, description="基准版本号（从 1 起）"),
    to_version: int = Query(..., ge=1, description="目标版本号（从 1 起）"),
    db: Connection = Depends(get_db),
):
    """逐字段比较两个指定版本，返回发生变化的业务字段及新旧值。

    证书不存在或任一版本号不存在时返回 404（中文原因）。
    """
    if crud.get_certificate(db, cert_id) is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    old = crud.get_certificate_revision(db, cert_id, from_version)
    if old is None:
        raise HTTPException(404, f"证书 {cert_id} 不存在版本 {from_version}")
    new = crud.get_certificate_revision(db, cert_id, to_version)
    if new is None:
        raise HTTPException(404, f"证书 {cert_id} 不存在版本 {to_version}")
    changes = {
        f: {"from": old[f], "to": new[f]}
        for f in crud.CERT_BUSINESS_FIELDS
        if old[f] != new[f]
    }
    return {
        "certificate_id": cert_id,
        "from_version": from_version,
        "to_version": to_version,
        "changed_fields": list(changes),
        "changes": changes,
    }
