"""演示数据：覆盖 唯一成链 / 多候选冲突 / 断裂 / 摘要被篡改 四种场景。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import crud
from .schemas import CertificateIn, InstrumentIn, TraceEdgeIn

DEMO_AT = "2026-06-15T00:00:00Z"


def dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def seed_demo(conn: sqlite3.Connection) -> None:
    # ---------- 仪器 ----------
    instruments = [
        InstrumentIn(code="INST-G", name="国家长度基准激光干涉装置",
                     manufacturer="NIM", is_national_standard=True),
        InstrumentIn(code="INST-R1", name="一等量块标准器", manufacturer="NIM"),
        InstrumentIn(code="INST-W1", name="数显卡尺", manufacturer="Sylvac",
                     serial_no="W1-001"),
        InstrumentIn(code="INST-R2", name="二等量块标准器", manufacturer="NIM"),
        InstrumentIn(code="INST-W2", name="高度尺", manufacturer="Mitutoyo",
                     serial_no="W2-014"),
        InstrumentIn(code="INST-TP", name="温度参考标准器", manufacturer="Fluke"),
        InstrumentIn(code="INST-T1", name="工作用热电偶", serial_no="T1-007"),
        InstrumentIn(code="INST-GP", name="国家压力基准装置",
                     is_national_standard=True),
        InstrumentIn(code="INST-PRA", name="二等活塞式压力计A"),
        InstrumentIn(code="INST-PRB", name="数字压力校准器B"),
        InstrumentIn(code="INST-P1", name="精密压力表", serial_no="P1-099"),
        InstrumentIn(code="INST-B1", name="三坐标测量机", serial_no="B1-203"),
    ]
    for i in instruments:
        crud.upsert_instrument(conn, i)

    # ---------- 证书 ----------
    def cert(cid, inst, number, issued, vf, vu, u, unit="", digest=None):
        return CertificateIn(
            id=cid, instrument_code=inst, cert_number=number,
            issued_at=dt(issued), valid_from=dt(vf), valid_until=dt(vu),
            uncertainty=u, unit=unit, signature_digest=digest,
        )

    certificates = [
        # 长度：工作仪器 -> 一等标准 -> 国家基准（唯一有效链）
        cert("CG-1", "INST-G", "NIM-LEN-2024-001",
             "2024-01-02T03:00:00", "2024-01-01T00:00:00",
             "2029-01-01T00:00:00", 0.000001, "mm"),
        cert("CR1-1", "INST-R1", "CAL-LEN-2025-016",
             "2025-06-05T06:00:00", "2025-06-01T00:00:00",
             "2026-12-31T00:00:00", 0.00005, "mm"),
        cert("CW1-1", "INST-W1", "CAL-LEN-2026-101",
             "2026-01-10T08:00:00", "2026-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.002, "mm"),
        # 长度：另一台工作仪器经二等量块同样溯源到国家长度基准
        # （与 INST-W1 共享 CG-1，可演示批次共同溯源核查）
        cert("CR2-1", "INST-R2", "CAL-LEN-2025-027",
             "2025-08-05T06:00:00", "2025-08-01T00:00:00",
             "2026-12-31T00:00:00", 0.0002, "mm"),
        cert("CW2-1", "INST-W2", "CAL-LEN-2026-205",
             "2026-02-10T08:00:00", "2026-02-01T00:00:00",
             "2027-02-01T00:00:00", 0.003, "mm"),
        # 温度：存在历史区间断档 + 向上溯源到非基准后死路
        cert("CTP-1", "INST-TP", "REF-TMP-2025-003",
             "2025-01-05T02:00:00", "2025-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.05, "C"),
        cert("CT1-1", "INST-T1", "CAL-TMP-2025-020",
             "2025-01-10T04:00:00", "2025-01-01T00:00:00",
             "2025-06-30T00:00:00", 0.5, "C"),
        cert("CT1-2", "INST-T1", "CAL-TMP-2025-188",
             "2025-09-05T04:00:00", "2025-09-01T00:00:00",
             "2026-09-01T00:00:00", 0.4, "C"),
        # 压力：同一压力表有两条都成立的溯源路径 -> 多候选冲突
        cert("CGP-1", "INST-GP", "NIM-PRS-2024-002",
             "2024-01-02T03:00:00", "2024-01-01T00:00:00",
             "2029-01-01T00:00:00", 0.001, "kPa"),
        cert("CPRA-1", "INST-PRA", "CAL-PRS-2025-007",
             "2025-01-10T05:00:00", "2025-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.01, "kPa"),
        cert("CPRB-1", "INST-PRB", "CAL-PRS-2025-008",
             "2025-01-12T05:00:00", "2025-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.02, "kPa"),
        cert("CPP1-1", "INST-P1", "CAL-PRS-2026-033",
             "2026-01-15T07:00:00", "2026-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.1, "kPa"),
        # 签名摘要被篡改：链结构完整但校验不通过
        cert("CBB1-1", "INST-B1", "CAL-CMM-2026-042",
             "2026-01-20T09:00:00", "2026-01-01T00:00:00",
             "2027-01-01T00:00:00", 0.001, "mm",
             digest="0" * 64),
    ]
    for c in certificates:
        crud.upsert_certificate(conn, c)

    # ---------- 父子溯源边 ----------
    def edge(child, parent, note=""):
        return TraceEdgeIn(child_cert_id=child, parent_cert_id=parent, note=note)

    edges = [
        edge("CW1-1", "CR1-1", "量块传递"),
        edge("CR1-1", "CG-1", "基准送检"),
        edge("CW2-1", "CR2-1", "量块传递"),
        edge("CR2-1", "CG-1", "基准送检"),
        edge("CT1-2", "CTP-1", "热电偶比对（参考标准未再溯源）"),
        edge("CPP1-1", "CPRA-1", "活塞压力计通道"),
        edge("CPP1-1", "CPRB-1", "数字校准器通道"),
        edge("CPRA-1", "CGP-1", "基准送检"),
        edge("CPRB-1", "CGP-1", "基准送检"),
        edge("CBB1-1", "CR1-1", "量块传递"),
    ]
    for e in edges:
        crud.upsert_edge(conn, e)

    # ---------- 校准工位预约（单工位，半开区间 [start_at, end_at)） ----------
    # 为数显卡尺 INST-W1 预置一条已确认预约，演示空档查询时工位已被占用；
    # 其起点 2026-06-20 在唯一链 CW1-1 -> CR1-1 -> CG-1 有效期内，
    # 结束早于起点证书 CW1-1 的到期时刻 2027-01-01T00:00:00Z。
    from .booking import insert_booking, insert_waitlist

    insert_booking(
        conn,
        booking_id="BK-DEMO-20260620-W1",
        instrument_code="INST-W1",
        start_at="2026-06-20T09:00:00Z",
        end_at="2026-06-20T10:00:00Z",
        duration_seconds=3600,
        certificate_id="CW1-1",
        certificate_valid_until="2027-01-01T00:00:00Z",
        note="演示数据：数显卡尺例行校准",
        created_at=DEMO_AT,
    )

    # ---------- 校准工位候补（演示：高度尺 INST-W2 候补同一时段） ----------
    # 候补沿用目标预约的起止时段，按登记先后排队；INST-W2 在 2026-06-20
    # 存在唯一有效链 CW2-1 -> CR2-1 -> CG-1，取消 BK-DEMO-20260620-W1
    # 时该候补会被补位为已确认预约。
    insert_waitlist(
        conn,
        waitlist_id="WL-DEMO-20260620-W2",
        booking_id="BK-DEMO-20260620-W1",
        instrument_code="INST-W2",
        start_at="2026-06-20T09:00:00Z",
        end_at="2026-06-20T10:00:00Z",
        duration_seconds=3600,
        note="演示数据：高度尺候补同一时段",
        created_at=DEMO_AT,
    )
