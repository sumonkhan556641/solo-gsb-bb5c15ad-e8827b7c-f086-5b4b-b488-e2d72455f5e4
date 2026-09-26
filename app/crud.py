"""数据访问与写入期校验。"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .schemas import CertificateIn, InstrumentIn, TraceEdgeIn
from .utils import (
    canonical_payload,
    compute_digest,
    fmt,
    to_utc_naive,
    utcnow_str,
)


class ValidationError(Exception):
    """输入数据业务校验失败（路由层映射为 HTTP 422）。"""


# ---------- 仪器 ----------

def upsert_instrument(conn: sqlite3.Connection, inst: InstrumentIn) -> str:
    conn.execute(
        """
        INSERT INTO instruments(code, name, manufacturer, model, serial_no,
                                is_national_standard, created_at)
        VALUES (:code, :name, :manufacturer, :model, :serial_no,
                :is_national_standard, :created_at)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            manufacturer=excluded.manufacturer,
            model=excluded.model,
            serial_no=excluded.serial_no,
            is_national_standard=excluded.is_national_standard
        """,
        {
            "code": inst.code,
            "name": inst.name,
            "manufacturer": inst.manufacturer,
            "model": inst.model,
            "serial_no": inst.serial_no,
            "is_national_standard": int(inst.is_national_standard),
            "created_at": utcnow_str(),
        },
    )
    return inst.code


def get_instrument(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM instruments WHERE code = ?", (code,)
    ).fetchone()


def list_instruments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM instruments ORDER BY code").fetchall()


# ---------- 证书 ----------

#: 参与“是否发生变化”判定并随版本快照保存的证书业务字段
#: （不含 created_at 等服务端维护字段）
CERT_BUSINESS_FIELDS = (
    "instrument_code", "cert_number", "issued_at", "valid_from",
    "valid_until", "uncertainty", "unit", "digest_alg",
    "signature_digest", "signed_payload",
)


def _insert_certificate_revision(
    conn: sqlite3.Connection,
    cert_id: str,
    version: int,
    values: Any,
    recorded_at: str,
) -> None:
    """写入一个版本快照（``values`` 可为 sqlite3.Row 或 dict）。"""
    conn.execute(
        """
        INSERT INTO certificate_revisions(
            certificate_id, version, instrument_code, cert_number,
            issued_at, valid_from, valid_until, uncertainty, unit,
            digest_alg, signature_digest, signed_payload, recorded_at)
        VALUES (:certificate_id, :version, :instrument_code, :cert_number,
                :issued_at, :valid_from, :valid_until, :uncertainty, :unit,
                :digest_alg, :signature_digest, :signed_payload, :recorded_at)
        """,
        {
            "certificate_id": cert_id,
            "version": version,
            **{k: values[k] for k in CERT_BUSINESS_FIELDS},
            "recorded_at": recorded_at,
        },
    )


def _record_certificate_revision(
    conn: sqlite3.Connection,
    cert_id: str,
    old: Optional[sqlite3.Row],
    new_values: dict[str, Any],
) -> None:
    """在证书 upsert 的同一事务内维护连续版本历史。

    * 首次写入：完整快照记为 v1；
    * 存量证书（库中已有行但尚无版本，功能上线前写入的）首次更新前，
      先把旧值补录为 v1；
    * 仅当业务字段实际变化时才追加连续版本 vN+1，同内容重交不增版。
    """
    now = utcnow_str()
    if old is None:
        _insert_certificate_revision(conn, cert_id, 1, new_values, now)
        return
    row = conn.execute(
        "SELECT MAX(version) AS v FROM certificate_revisions"
        " WHERE certificate_id = ?",
        (cert_id,),
    ).fetchone()
    max_version = row["v"]
    if max_version is None:
        # 存量证书首次更新：先补录旧值为 v1
        _insert_certificate_revision(conn, cert_id, 1, old, now)
        max_version = 1
    if any(old[k] != new_values[k] for k in CERT_BUSINESS_FIELDS):
        _insert_certificate_revision(conn, cert_id, max_version + 1, new_values, now)


def upsert_certificate(conn: sqlite3.Connection, cert: CertificateIn) -> str:
    inst = get_instrument(conn, cert.instrument_code)
    if inst is None:
        raise ValidationError(f"仪器不存在: {cert.instrument_code}（请先提交仪器）")

    d = cert.normalized()
    if not d["valid_from"] <= d["valid_until"]:
        raise ValidationError(
            f"证书 {cert.id} 的 valid_from 必须不晚于 valid_until"
        )

    payload = d["signed_payload"]
    if payload is None:
        payload = canonical_payload(d)
    digest = d["signature_digest"] or compute_digest(payload, d["digest_alg"])

    new_values = {
        **d,
        "signed_payload": payload,
        "signature_digest": digest,
        "created_at": utcnow_str(),
    }
    # 版本历史与证书更新同一事务：先取旧行，upsert 后按业务字段比对追加版本
    old = get_certificate(conn, cert.id)
    conn.execute(
        """
        INSERT INTO certificates(id, instrument_code, cert_number, issued_at,
                                 valid_from, valid_until, uncertainty, unit,
                                 digest_alg, signature_digest, signed_payload,
                                 created_at)
        VALUES (:id, :instrument_code, :cert_number, :issued_at,
                :valid_from, :valid_until, :uncertainty, :unit,
                :digest_alg, :signature_digest, :signed_payload, :created_at)
        ON CONFLICT(id) DO UPDATE SET
            instrument_code=excluded.instrument_code,
            cert_number=excluded.cert_number,
            issued_at=excluded.issued_at,
            valid_from=excluded.valid_from,
            valid_until=excluded.valid_until,
            uncertainty=excluded.uncertainty,
            unit=excluded.unit,
            digest_alg=excluded.digest_alg,
            signature_digest=excluded.signature_digest,
            signed_payload=excluded.signed_payload
        """,
        new_values,
    )
    _record_certificate_revision(conn, cert.id, old, new_values)
    return cert.id


def get_certificate(conn: sqlite3.Connection, cert_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE id = ?", (cert_id,)
    ).fetchone()


def list_certificates(
    conn: sqlite3.Connection, instrument_code: Optional[str] = None
) -> list[sqlite3.Row]:
    if instrument_code is not None:
        return conn.execute(
            "SELECT * FROM certificates WHERE instrument_code = ? ORDER BY valid_from",
            (instrument_code,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM certificates ORDER BY instrument_code, valid_from"
    ).fetchall()


# ---------- 证书修订历史 ----------

def list_certificate_revisions(
    conn: sqlite3.Connection, cert_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_revisions WHERE certificate_id = ?"
        " ORDER BY version",
        (cert_id,),
    ).fetchall()


def get_certificate_revision(
    conn: sqlite3.Connection, cert_id: str, version: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_revisions"
        " WHERE certificate_id = ? AND version = ?",
        (cert_id, version),
    ).fetchone()


# ---------- 溯源边 ----------

def upsert_edge(conn: sqlite3.Connection, edge: TraceEdgeIn) -> int:
    child = get_certificate(conn, edge.child_cert_id)
    if child is None:
        raise ValidationError(f"子证书不存在: {edge.child_cert_id}")
    parent = get_certificate(conn, edge.parent_cert_id)
    if parent is None:
        raise ValidationError(f"父证书不存在: {edge.parent_cert_id}")
    if edge.child_cert_id == edge.parent_cert_id:
        raise ValidationError("溯源边不能指向证书自身（自环）")

    vf = to_utc_naive(edge.valid_from) if edge.valid_from else None
    vu = to_utc_naive(edge.valid_until) if edge.valid_until else None
    # 未显式给出时，边的有效区间继承子证书区间
    vf_s = fmt(vf) if vf is not None else child["valid_from"]
    vu_s = fmt(vu) if vu is not None else child["valid_until"]
    if vf_s > vu_s:
        raise ValidationError(
            f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
            "的 valid_from 必须不晚于 valid_until"
        )
    if vu_s < child["valid_from"] or vf_s > child["valid_until"]:
        raise ValidationError(
            f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
            "有效区间与子证书区间完全不相交"
        )

    cur = conn.execute(
        """
        INSERT INTO trace_edges(child_cert_id, parent_cert_id, valid_from,
                                valid_until, note, created_at)
        VALUES (:child, :parent, :vf, :vu, :note, :created_at)
        ON CONFLICT(child_cert_id, parent_cert_id) DO UPDATE SET
            valid_from=excluded.valid_from,
            valid_until=excluded.valid_until,
            note=excluded.note
        """,
        {
            "child": edge.child_cert_id,
            "parent": edge.parent_cert_id,
            "vf": vf_s,
            "vu": vu_s,
            "note": edge.note,
            "created_at": utcnow_str(),
        },
    )
    row = conn.execute(
        "SELECT id FROM trace_edges WHERE child_cert_id=? AND parent_cert_id=?",
        (edge.child_cert_id, edge.parent_cert_id),
    ).fetchone()
    return int(row["id"])


def list_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trace_edges ORDER BY child_cert_id, parent_cert_id"
    ).fetchall()


# ---------- 批量提交 ----------

def submit(
    conn: sqlite3.Connection,
    instruments: list[InstrumentIn],
    certificates: list[CertificateIn],
    edges: list[TraceEdgeIn],
) -> dict[str, Any]:
    inst_ids = [upsert_instrument(conn, i) for i in instruments]
    cert_ids = [upsert_certificate(conn, c) for c in certificates]
    edge_ids = [upsert_edge(conn, e) for e in edges]
    return {
        "instruments_upserted": inst_ids,
        "certificates_upserted": cert_ids,
        "edges_upserted": edge_ids,
    }


# ---------- 批量提交预检（dry-run，独立事务回滚，不写库） ----------

# 路径分类的稳定排序权重：仪器 < 证书 < 边（与提交流序一致）
_PATH_RANK = {"instruments": 0, "certificates": 1, "edges": 2}


def _cert_digest_info(cert: CertificateIn) -> tuple[str, str, str]:
    """返回 (载荷, 提交摘要或重算摘要, 服务端重算摘要)。"""
    d = cert.normalized()
    payload = d["signed_payload"]
    if payload is None:
        payload = canonical_payload(d)
    recomputed = compute_digest(payload, d["digest_alg"])
    stored = d["signature_digest"] or recomputed
    return payload, stored, recomputed


def _issue_sort_key(issue: dict[str, Any]) -> tuple[int, int, str]:
    """按 路径分类 / 提交下标 / 路径其余部分 稳定排序。

    下标按整数比较，避免字符串序把 10 排到 2 前面。
    """
    parts = issue["path"].split("/")
    section = parts[0] if parts else ""
    index = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else -1
    tail = "/".join(parts[2:])
    return (_PATH_RANK.get(section, 99), index, tail)


def preflight(
    conn: sqlite3.Connection,
    instruments: list[InstrumentIn],
    certificates: list[CertificateIn],
    edges: list[TraceEdgeIn],
    strict_digest: bool = False,
) -> dict[str, Any]:
    """模拟整批提交并收集全部校验问题，不向外抛出。

    ``conn`` 必须由 ``database.get_preflight_conn()`` 提供：预检在其中照常
    upsert 以高保真模拟引用与区间继承，但事务在路由层退出时整体回滚，
    绝不写入数据库。校验按提交顺序（仪器 → 证书 → 边）逐项进行；
    某条目存在阻断性错误时跳过其写入，避免后续条目产生连带的级联误报。
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add(
        bucket: list[dict[str, Any]],
        path: str,
        code: str,
        message: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        bucket.append(
            {"path": path, "code": code, "message": message, "detail": detail or {}}
        )

    # 1) 仪器：upsert 无业务校验，必然按条目数写入
    for inst in instruments:
        upsert_instrument(conn, inst)
    inst_ok = len(instruments)

    # 2) 证书：引用（所属仪器）→ 区间 → 摘要
    cert_ok = 0
    for idx, cert in enumerate(certificates):
        base = f"certificates/{idx}"
        blocking = False

        if get_instrument(conn, cert.instrument_code) is None:
            blocking = True
            add(
                errors,
                f"{base}/instrument_code",
                "INSTRUMENT_NOT_FOUND",
                f"仪器不存在: {cert.instrument_code}（请先提交仪器）",
                {"instrument_code": cert.instrument_code},
            )

        d = cert.normalized()
        if d["valid_from"] > d["valid_until"]:
            blocking = True
            add(
                errors,
                f"{base}/valid_from",
                "CERTIFICATE_INTERVAL_INVALID",
                f"证书 {cert.id} 的 valid_from 必须不晚于 valid_until",
                {
                    "certificate_id": cert.id,
                    "valid_from": d["valid_from"],
                    "valid_until": d["valid_until"],
                },
            )

        # 摘要只在客户端显式提交 signature_digest 时才比对
        if d["signature_digest"]:
            _, stored, recomputed = _cert_digest_info(cert)
            if stored != recomputed:
                issue = {
                    "path": f"{base}/signature_digest",
                    "code": "SIGNATURE_DIGEST_MISMATCH",
                    "message": (
                        f"证书 {cert.id}（{cert.cert_number}）签名摘要校验失败："
                        f"提交 {stored}，重算 {recomputed}"
                    ),
                    "detail": {
                        "certificate_id": cert.id,
                        "digest_alg": d["digest_alg"],
                        "submitted": stored,
                        "recomputed": recomputed,
                    },
                }
                (errors if strict_digest else warnings).append(issue)

        if blocking:
            continue
        payload, digest, _ = _cert_digest_info(cert)
        conn.execute(
            """
            INSERT INTO certificates(id, instrument_code, cert_number, issued_at,
                                     valid_from, valid_until, uncertainty, unit,
                                     digest_alg, signature_digest, signed_payload,
                                     created_at)
            VALUES (:id, :instrument_code, :cert_number, :issued_at,
                    :valid_from, :valid_until, :uncertainty, :unit,
                    :digest_alg, :signature_digest, :signed_payload, :created_at)
            ON CONFLICT(id) DO UPDATE SET
                instrument_code=excluded.instrument_code,
                cert_number=excluded.cert_number,
                issued_at=excluded.issued_at,
                valid_from=excluded.valid_from,
                valid_until=excluded.valid_until,
                uncertainty=excluded.uncertainty,
                unit=excluded.unit,
                digest_alg=excluded.digest_alg,
                signature_digest=excluded.signature_digest,
                signed_payload=excluded.signed_payload
            """,
            {
                **d,
                "signed_payload": payload,
                "signature_digest": digest,
                "created_at": utcnow_str(),
            },
        )
        cert_ok += 1

    # 3) 边：引用（子/父证书）→ 自环 → 区间（自身区间、与子证书区间相交）
    edge_ok = 0
    for idx, edge in enumerate(edges):
        base = f"edges/{idx}"
        blocking = False

        child = get_certificate(conn, edge.child_cert_id)
        if child is None:
            blocking = True
            add(
                errors,
                f"{base}/child_cert_id",
                "CHILD_CERT_NOT_FOUND",
                f"子证书不存在: {edge.child_cert_id}",
                {"child_cert_id": edge.child_cert_id},
            )

        parent = get_certificate(conn, edge.parent_cert_id)
        if parent is None:
            blocking = True
            add(
                errors,
                f"{base}/parent_cert_id",
                "PARENT_CERT_NOT_FOUND",
                f"父证书不存在: {edge.parent_cert_id}",
                {"parent_cert_id": edge.parent_cert_id},
            )

        # 与正式提交一致：自环检查先于区间检查
        if edge.child_cert_id == edge.parent_cert_id:
            blocking = True
            add(
                errors,
                f"{base}/parent_cert_id",
                "EDGE_SELF_LOOP",
                "溯源边不能指向证书自身（自环）",
                {
                    "child_cert_id": edge.child_cert_id,
                    "parent_cert_id": edge.parent_cert_id,
                },
            )

        if child is not None:
            vf = to_utc_naive(edge.valid_from) if edge.valid_from else None
            vu = to_utc_naive(edge.valid_until) if edge.valid_until else None
            # 未显式给出时，边的有效区间继承子证书区间
            vf_s = fmt(vf) if vf is not None else child["valid_from"]
            vu_s = fmt(vu) if vu is not None else child["valid_until"]

            if vf_s > vu_s:
                blocking = True
                add(
                    errors,
                    f"{base}/valid_from",
                    "EDGE_INTERVAL_INVALID",
                    f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
                    "的 valid_from 必须不晚于 valid_until",
                    {
                        "child_cert_id": edge.child_cert_id,
                        "parent_cert_id": edge.parent_cert_id,
                        "valid_from": vf_s,
                        "valid_until": vu_s,
                    },
                )
            elif vu_s < child["valid_from"] or vf_s > child["valid_until"]:
                blocking = True
                add(
                    errors,
                    f"{base}/valid_from",
                    "EDGE_INTERVAL_DISJOINT",
                    f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
                    "有效区间与子证书区间完全不相交",
                    {
                        "child_cert_id": edge.child_cert_id,
                        "parent_cert_id": edge.parent_cert_id,
                        "edge_valid_from": vf_s,
                        "edge_valid_until": vu_s,
                        "child_valid_from": child["valid_from"],
                        "child_valid_until": child["valid_until"],
                    },
                )

        if blocking:
            continue
        conn.execute(
            """
            INSERT INTO trace_edges(child_cert_id, parent_cert_id, valid_from,
                                    valid_until, note, created_at)
            VALUES (:child, :parent, :vf, :vu, :note, :created_at)
            ON CONFLICT(child_cert_id, parent_cert_id) DO UPDATE SET
                valid_from=excluded.valid_from,
                valid_until=excluded.valid_until,
                note=excluded.note
            """,
            {
                "child": edge.child_cert_id,
                "parent": edge.parent_cert_id,
                "vf": vf_s,
                "vu": vu_s,
                "note": edge.note,
                "created_at": utcnow_str(),
            },
        )
        edge_ok += 1

    errors.sort(key=_issue_sort_key)
    warnings.sort(key=_issue_sort_key)

    return {
        "strict_digest": strict_digest,
        "dry_run": True,
        "ok": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "would_upsert": {
            "instruments": inst_ok,
            "certificates": cert_ok,
            "edges": edge_ok,
            "total": inst_ok + cert_ok + edge_ok,
        },
    }


def reset(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM station_maintenance_windows;
        DELETE FROM calibration_waitlist;
        DELETE FROM calibration_bookings;
        DELETE FROM trace_edges;
        DELETE FROM certificate_revisions;
        DELETE FROM certificates;
        DELETE FROM instruments;
        DELETE FROM sqlite_sequence WHERE name='trace_edges';
        DELETE FROM sqlite_sequence WHERE name='calibration_waitlist';
        """
    )
