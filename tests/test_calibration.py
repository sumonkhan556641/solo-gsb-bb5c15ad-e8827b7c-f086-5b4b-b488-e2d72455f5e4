import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DB_PATH"] = _tmp.name
os.environ["SEED_DEMO"] = "0"

from app.database import get_conn_ctx, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()


def _chain(client, *, parent_until="2030-01-01T00:00:00",
           leaf_until="2027-01-01T00:00:00",
           leaf_from="2026-01-01T00:00:00",
           issued_leaf="2026-01-02T00:00:00",
           code="W"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链。

    默认令上级证书有效期长于起点证书，从而在起点证书到期后链本身仍成立，
    可专门验证“预约必须在起点证书到期前结束”的截止规则。证书 id 以仪器
    编码为后缀，保证同一测试里可提交多台仪器而不互相 upsert 覆盖。
    """
    cg, cr, cw = f"CG-{code}", f"CR-{code}", f"CW-{code}"
    instruments = [
        {"code": f"G-{code}", "name": f"国家基准-{code}",
         "is_national_standard": True},
        {"code": f"R-{code}", "name": f"参考标准器-{code}"},
        {"code": code, "name": "工作仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": parent_until,
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
         "issued_at": issued_leaf,
         "valid_from": leaf_from, "valid_until": leaf_until,
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": cw, "parent_cert_id": cr},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions",
                    json={"instruments": instruments, "certificates": certs, "edges": edges})
    assert r.status_code == 201, r.text


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- GET slots

def test_slot_earliest_when_empty(client):
    _chain(client)
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["traceability_available"] is True
    assert d["certificate_id"] == "CW-W"
    assert d["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    # 工位空闲：最早时段即窗口起点
    assert d["slot"] == {
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-20T01:00:00Z",
        "ends_by_certificate_valid_until": True,
    }
    assert d["busy"] == []
    assert d["reason_code"] is None
    # 附带完整溯源报告以便解释
    assert d["trace_report"]["resolution"] == "unique"


def test_slot_jumps_over_half_open_occupancy(client):
    _chain(client)
    # 占用 [09:00,10:00)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T08:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # [08,09) 只有 1 小时空档 -> 放得下，最早从 08:00 开始
    assert d["slot"]["start"] == "2026-06-20T08:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T09:00:00Z"
    assert len(d["busy"]) == 1
    # 半开：[08,09) 与占用 [09,10) 不冲突（ends 恰在 starts）
    assert d["busy"][0]["start_at"] == "2026-06-20T09:00:00Z"


def test_slot_back_to_back_boundary_not_overlap(client):
    _chain(client)
    # 占满 [09,11)，从 09:00 查询 1 小时 -> 最早 11:00（可相接）
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 7200,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"]["start"] == "2026-06-20T11:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T12:00:00Z"


def test_slot_no_free_returns_reason(client):
    _chain(client)
    # 占住 [08:30, 11:30)，窗口 09..12 求 2 小时 -> 剩余空档不足
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T08:30:00Z", "duration_seconds": 3 * 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 7200,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_FREE_SLOT_IN_WINDOW"
    assert "单个校准工位" in d["reason"]


def test_slot_search_window_not_clipped_but_candidate_rechecked(client):
    """搜索窗不再按查询起点证书截止裁剪：搜索覆盖整个请求窗，沿证书
    截止等有效期边界逐候选重验。本用例中查询起点之后整窗空闲，但起点
    证书 12:00 到期且其后无换新证书，最早空档 [00:00,01:00) 本就可确认，
    故仍返回窗口起点的时段。"""
    _chain(client, leaf_until="2026-06-20T12:00:00Z")
    # 查询窗远超起点证书到期时刻
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2027-06-20T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # 搜索窗右端等于请求窗右端（不再按单张证书截止裁剪）
    assert d["search_window_end"] == "2027-06-20T00:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T01:00:00Z"
    assert d["slot"]["ends_by_certificate_valid_until"] is True
    # 建议起点即查询起点，溯源证据对应查询起点
    assert d["traceability_at"] == "2026-06-20T00:00:00Z"
    assert d["certificate_id"] == "CW-W"
    assert d["certificate_valid_until"] == "2026-06-20T12:00:00Z"


def test_slot_pushed_past_certificate_expiry_rechecks_boundaries(client):
    """占用把建议起点一路推后到起点证书到期之后：必须对推迟后的候选
    起点重放确认门禁（不沿用查询起点的链结论），沿有效期边界继续寻找；
    到期后无换新证书 -> 空建议，原因码 CERTIFICATE_DEADLINE_TOO_EARLY，
    证书/截止/溯源说明对应最后一个链仍成立的候选。"""
    _chain(client, code="W", leaf_until="2026-06-20T10:30:00Z")
    # 另一台仪器（长有效期证书）占用 [10:00,11:00)：单工位不分仪器，
    # 把 W 的最早空档推到 11:00 —— 已越过 W 起点证书 10:30 截止
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T10:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T10:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "CERTIFICATE_DEADLINE_TOO_EARLY"
    assert "到期" in d["reason"]
    # 搜索窗覆盖整个请求窗（未裁剪）；物理候选 11:00 无有效证书，但
    # 占用段起点之后的截止边界点 10:30 也被单独重验——证据取最后链
    # 成立的候选（截止边界点 10:30，CW-W 闭区间端点仍有效）
    assert d["search_window_end"] == "2026-06-21T00:00:00Z"
    assert d["traceability_at"] == "2026-06-20T10:30:00Z"
    assert d["certificate_id"] == "CW-W"
    assert d["certificate_valid_until"] == "2026-06-20T10:30:00Z"
    assert d["trace_report"]["at"] == "2026-06-20T10:30:00Z"
    assert d["trace_report"]["resolution"] == "unique"


def test_slot_deadline_too_early(client):
    _chain(client, leaf_until="2026-06-20T10:30:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T10:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "CERTIFICATE_DEADLINE_TOO_EARLY"
    assert "到期" in d["reason"]
    # 搜索窗不再裁剪；溯源证据取最后链成立的候选（截止边界点）
    assert d["search_window_end"] == "2026-06-21T00:00:00Z"
    assert d["traceability_at"] == "2026-06-20T10:30:00Z"
    assert d["certificate_valid_until"] == "2026-06-20T10:30:00Z"


def test_slot_deadline_clips_otherwise_fitting_window(client):
    """时长本在原窗内放得下，但最早可用候选无法在起点证书到期前结束、
    其后也无换新链 -> 归因为截止过早，而不是窗口过短或物理空档不足。"""
    _chain(client, code="W", leaf_until="2026-06-20T10:30:00Z")
    # 另一台仪器占用 [10:00,11:00)：W 最早候选 11:00（需 2h）越过 10:30 截止
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T10:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T10:00:00Z",
        "end": "2026-06-21T00:00:00Z",   # 原窗 14 小时，足够 2 小时
        "duration_seconds": 7200,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "CERTIFICATE_DEADLINE_TOO_EARLY"
    assert d["search_window_end"] == "2026-06-21T00:00:00Z"


def test_slot_exact_fit_to_deadline(client):
    _chain(client, leaf_until="2026-06-20T12:00:00Z")
    # [11:00,12:00) 恰在到期瞬间结束：半开，允许
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T11:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] == {
        "start": "2026-06-20T11:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "ends_by_certificate_valid_until": True,
    }


def test_slot_chain_unavailable_explains_reason(client):
    # 只有仪器与一张失效起点证书：无有效证书 -> 链不可开始
    client.post("/api/submissions", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [
            {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00",
             "valid_until": "2026-01-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [],
    })
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["traceability_available"] is False
    assert d["slot"] is None
    assert d["reason_code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert d["chain_reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert "没有处于有效区间的校准证书" in d["reason"]
    assert d["trace_report"]["resolution"] == "broken"


def test_slot_ambiguous_chain_explains(client):
    # 同一起点挂两条都到基准的父链 -> 多候选
    client.post("/api/submissions", json={
        "instruments": [
            {"code": "G", "name": "基准", "is_national_standard": True},
            {"code": "RA", "name": "参考A"},
            {"code": "RB", "name": "参考B"},
            {"code": "W", "name": "工作仪器"},
        ],
        "certificates": [
            {"id": "CG", "instrument_code": "G", "cert_number": "G",
             "issued_at": "2024-01-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
             "uncertainty": 0.0001, "unit": "mm"},
            {"id": "CRA", "instrument_code": "RA", "cert_number": "RA",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": "CRB", "instrument_code": "RB", "cert_number": "RB",
             "issued_at": "2025-01-01T00:00:00",
             "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": "CW", "instrument_code": "W", "cert_number": "W",
             "issued_at": "2026-01-02T00:00:00",
             "valid_from": "2026-01-01T00:00:00", "valid_until": "2027-01-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [
            {"child_cert_id": "CW", "parent_cert_id": "CRA"},
            {"child_cert_id": "CW", "parent_cert_id": "CRB"},
            {"child_cert_id": "CRA", "parent_cert_id": "CG"},
            {"child_cert_id": "CRB", "parent_cert_id": "CG"},
        ],
    })
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z", "duration_seconds": 600,
    })
    d = r.json()
    assert d["traceability_available"] is False
    assert d["chain_reason_code"] == "MULTIPLE_VALID_CHAINS"
    assert d["trace_report"]["resolution"] == "ambiguous"


def test_slot_unknown_instrument_404(client):
    r = client.get("/api/calibration-slots", params={
        "instrument": "GHOST",
        "start": "2026-06-20T00:00:00Z", "end": "2026-06-21T00:00:00Z",
        "duration_seconds": 60,
    })
    assert r.status_code == 404


@pytest.mark.parametrize("params", [
    {"start": "2026-06-21T00:00:00Z", "end": "2026-06-20T00:00:00Z",
     "duration_seconds": 60},                       # 起晚于止
    {"start": "2026-06-20T00:00:00Z", "end": "2026-06-20T01:00:00Z",
     "duration_seconds": 7200},                     # 时长超过窗长
    {"start": "2026-06-20T00:00:00Z", "end": "2026-06-20T00:00:00Z",
     "duration_seconds": 60},                       # 空窗（start==end）
])
def test_slot_invalid_window_422(client, params):
    _chain(client)
    r = client.get("/api/calibration-slots",
                   params={"instrument": "W", **params})
    assert r.status_code == 422


def test_slot_nonpositive_duration_422(client):
    _chain(client)
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T00:00:00Z",
        "end": "2026-06-21T00:00:00Z", "duration_seconds": 0,
    })
    assert r.status_code == 422


def test_slot_station_shared_across_instruments(client):
    """单工位：另一台仪器的占用同样阻塞本仪器（占用不分仪器）。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z", "duration_seconds": 3600,
    })
    d = r.json()
    # [09,10) 被另一台仪器占用 -> 最早 10:00
    assert d["slot"]["start"] == "2026-06-20T10:00:00Z"
    assert d["busy"][0]["instrument_code"] == "W2"


# --------------------------------------------- 推迟后按边界重验（修复核心）

def _renewal_chain(client, *, code="WR", old_until, new_from, new_until,
                   new_issued=None):
    """提交 G -> R -> W 链，其中 W 有两张闭区间首尾相接的换新证书。

    CWa 为旧证（valid_until=old_until），CWb 为新证
    （valid_from=new_from），new_from == old_until 时交接点两证同时
    有效（闭区间）；两证均挂同一条到参考标准 R 的溯源边，保证换新后
    唯一链仍成立。历史区间闭区间并集连续，不报 INTERVAL_GAP。
    """
    cg, cr, cwa, cwb = f"CG-{code}", f"CR-{code}", f"CWa-{code}", f"CWb-{code}"
    instruments = [
        {"code": f"G-{code}", "name": f"国家基准-{code}",
         "is_national_standard": True},
        {"code": f"R-{code}", "name": f"参考标准器-{code}"},
        {"code": code, "name": "换新仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cwa, "instrument_code": code, "cert_number": f"{code}-Wa",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": "2026-01-01T00:00:00", "valid_until": old_until,
         "uncertainty": 0.01, "unit": "mm"},
        {"id": cwb, "instrument_code": code, "cert_number": f"{code}-Wb",
         "issued_at": new_issued or new_from,
         "valid_from": new_from, "valid_until": new_until,
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": cwa, "parent_cert_id": cr},
        {"child_cert_id": cwb, "parent_cert_id": cr},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions",
                    json={"instruments": instruments, "certificates": certs,
                          "edges": edges})
    assert r.status_code == 201, r.text
    return cwa, cwb


def test_slot_pushed_into_renewed_certificate_uses_new_chain(client):
    """核心修复：占用把建议起点推后到旧证到期之后，旧证结论不可沿用；
    换新证书 CWb 在新起点构成唯一有效链时，应在新证书上给出最早可确认
    时段，且证书/截止/溯源报告对应实际建议起点。"""
    cwa, cwb = _renewal_chain(
        client,
        old_until="2026-06-20T10:00:00Z",
        new_from="2026-06-20T10:00:00Z",
        new_until="2027-06-01T00:00:00Z",
    )
    # 另一台仪器占用 [09:00,10:00)：WR 最早空档被推到 10:00（交接点）
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201

    r = client.get("/api/calibration-slots", params={
        "instrument": "WR",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    # 交接点 10:00 两证同时有效（闭区间），不可确认；从 10:00:01 重找
    # -> 10:00:01 起新证 CWb 唯一有效，最早可确认时段 10:00:01 ~ 11:00:01
    assert d["reason_code"] is None
    assert d["slot"]["start"] == "2026-06-20T10:00:01Z"
    assert d["slot"]["end"] == "2026-06-20T11:00:01Z"
    assert d["slot"]["ends_by_certificate_valid_until"] is True
    # 证据全部对应实际建议起点（新证），而非查询起点的旧证
    assert d["traceability_at"] == "2026-06-20T10:00:01Z"
    assert d["certificate_id"] == cwb
    assert d["certificate_valid_until"] == "2027-06-01T00:00:00Z"
    assert d["trace_report"]["at"] == "2026-06-20T10:00:01Z"
    assert d["trace_report"]["resolution"] == "unique"
    assert d["trace_report"]["starts"] == [cwb]
    # 查询起点的旧证仍然列出（它在查询起点有效），仅不作为建议证据
    assert cwa != d["certificate_id"]


def test_slot_closed_handoff_point_revalidated(client):
    """闭区间交接点本身必须单独重验：新证 valid_from == 旧证 valid_until
    的整秒点两证同时有效 -> MULTIPLE_ACTIVE_CERTIFICATES，该点给出的
    时段不可确认；其后开区间内新证唯一，最早可确认时段从 +1 秒开始。"""
    _renewal_chain(
        client,
        old_until="2026-06-20T10:00:00Z",
        new_from="2026-06-20T10:00:00Z",
        new_until="2027-06-01T00:00:00Z",
    )
    # 09:30 起求 1 小时：[09:30,10:00) 仅 30 分钟放不下 -> 候选 10:00
    # 恰为闭区间交接点（双证有效，结构性不可确认）-> 10:00:01 起新证
    # 唯一，[10:00:01,11:00:01) 可确认
    r = client.get("/api/calibration-slots", params={
        "instrument": "WR",
        "start": "2026-06-20T09:30:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"]["start"] == "2026-06-20T10:00:01Z"
    assert d["slot"]["end"] == "2026-06-20T11:00:01Z"
    assert d["traceability_at"] == "2026-06-20T10:00:01Z"
    assert d["trace_report"]["starts"] == ["CWb-WR"]
    assert d["trace_report"]["resolution"] == "unique"


def test_slot_handoff_point_blocked_then_region_occupied_no_slot(client):
    """闭区间交接点重验不通过（双证有效），且交接后整个剩余查询窗被
    工位占用 -> 全窗无合格时段，NO_CONFIRMABLE_SLOT_IN_WINDOW，
    chain_reason_code/trace_report 对应最后重验的交接点候选。"""
    _renewal_chain(
        client,
        old_until="2026-06-20T10:00:00Z",
        new_from="2026-06-20T10:00:00Z",
        new_until="2027-06-01T00:00:00Z",
    )
    # 另一台仪器占满交接点之后的整个区域 [10:00,12:00)
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T10:00:00Z", "duration_seconds": 7200,
    }).status_code == 201
    r = client.get("/api/calibration-slots", params={
        "instrument": "WR",
        "start": "2026-06-20T09:30:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_CONFIRMABLE_SLOT_IN_WINDOW"
    # 最后重验候选为闭区间交接点 10:00（双证有效）；10:00:01 之后虽新证
    # 唯一但工位被占满，无物理候选可供重验
    assert d["traceability_at"] == "2026-06-20T10:00:00Z"
    assert d["traceability_available"] is False
    assert d["chain_reason_code"] == "MULTIPLE_ACTIVE_CERTIFICATES"
    assert d["trace_report"]["at"] == "2026-06-20T10:00:00Z"


def test_slot_pushed_past_trace_edge_validity_boundary(client):
    """溯源边自身有效期边界同样驱动继续寻找：起点证书长期有效，但其
    唯一向上溯源边在 10:00 失效。占用把候选推过该边界后，候选起点
    链断裂（DEAD_END）-> 结构性不可确认，全窗无恢复点则返回
    NO_CONFIRMABLE_SLOT_IN_WINDOW，证据对应最后重验候选。"""
    cg, cr, cw = "CG-WE", "CR-WE", "CW-WE"
    instruments = [
        {"code": "G-WE", "name": "国家基准", "is_national_standard": True},
        {"code": "R-WE", "name": "参考"},
        {"code": "WE", "name": "边截止仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": "G-WE", "cert_number": "g",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": cr, "instrument_code": "R-WE", "cert_number": "r",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": "WE", "cert_number": "w",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": "2026-01-01T00:00:00", "valid_until": "2028-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]
    # 溯源边 CW -> CR 仅有效到 2026-06-20T10:00:00Z（与子证书区间相交）
    edges = [
        {"child_cert_id": cw, "parent_cert_id": cr,
         "valid_from": "2026-01-01T00:00:00Z",
         "valid_until": "2026-06-20T10:00:00Z"},
        {"child_cert_id": cr, "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions", json={
        "instruments": instruments, "certificates": certs, "edges": edges})
    assert r.status_code == 201, r.text

    # 另一台仪器占用 [09:00,10:30)：WE 最早候选 10:30 越过边有效期 10:00
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 5400,
    }).status_code == 201

    r = client.get("/api/calibration-slots", params={
        "instrument": "WE",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 1800,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_CONFIRMABLE_SLOT_IN_WINDOW"
    # 边界点 10:00 是占用段内事件（边刚失效：闭区间端点上边仍有效，
    # 但该点放不下 30 分钟空档）；最早物理候选 10:30：证书仍有效但无
    # 有效向上溯源边 -> CHAIN_BROKEN（结构性）
    assert d["traceability_at"] == "2026-06-20T10:30:00Z"
    assert d["traceability_available"] is False
    assert d["chain_reason_code"] == "CHAIN_BROKEN"
    assert d["trace_report"]["resolution"] == "broken"


def test_slot_ambiguous_handoff_after_push_returns_no_confirmable(client):
    """占用推后到某条溯源边的有效期边界后链变多候选：闭区间交接点与
    其后开区间均不可确认，全窗无合格时段 -> NO_CONFIRMABLE_SLOT_IN_WINDOW，
    chain_reason_code 给出最后的结构性原因（MULTIPLE_VALID_CHAINS）。"""
    # WR 旧证 CWa -> CR 单链；新证 CWb 在交接点起同时挂 CRA/CRB 两条父边
    cg = "CG-WRA"
    instruments = [
        {"code": "G-WRA", "name": "基准", "is_national_standard": True},
        {"code": "RA-WRA", "name": "参考A"},
        {"code": "RB-WRA", "name": "参考B"},
        {"code": "WRA", "name": "双路换新仪器"},
    ]
    certs = [
        {"id": cg, "instrument_code": "G-WRA", "cert_number": "G",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CRA-WRA", "instrument_code": "RA-WRA", "cert_number": "RA",
         "issued_at": "2025-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CRB-WRA", "instrument_code": "RB-WRA", "cert_number": "RB",
         "issued_at": "2025-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CWa-WRA", "instrument_code": "WRA", "cert_number": "Wa",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2026-06-20T10:00:00Z",
         "uncertainty": 0.01, "unit": "mm"},
        {"id": "CWb-WRA", "instrument_code": "WRA", "cert_number": "Wb",
         "issued_at": "2026-06-01T00:00:00",
         "valid_from": "2026-06-20T10:00:00Z",
         "valid_until": "2027-06-01T00:00:00Z",
         "uncertainty": 0.01, "unit": "mm"},
    ]
    edges = [
        {"child_cert_id": "CWa-WRA", "parent_cert_id": "CRA-WRA"},
        {"child_cert_id": "CWb-WRA", "parent_cert_id": "CRA-WRA"},
        {"child_cert_id": "CWb-WRA", "parent_cert_id": "CRB-WRA"},
        {"child_cert_id": "CRA-WRA", "parent_cert_id": cg},
        {"child_cert_id": "CRB-WRA", "parent_cert_id": cg},
    ]
    r = client.post("/api/submissions", json={
        "instruments": instruments, "certificates": certs, "edges": edges})
    assert r.status_code == 201, r.text

    # 另一台仪器占用 [09:00,10:00)，把 WRA 最早空档推到交接点 10:00
    _chain(client, code="W2", leaf_until="2028-01-01T00:00:00Z")
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201

    r = client.get("/api/calibration-slots", params={
        "instrument": "WRA",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_CONFIRMABLE_SLOT_IN_WINDOW"
    # 10:00 交接点为多证（双起点）+ 双父边，10:00:01 起新证双父边多候选；
    # 最后一次重验（10:00:01）的结构性原因应是多候选链
    assert d["traceability_at"] == "2026-06-20T10:00:01Z"
    assert d["chain_reason_code"] == "MULTIPLE_VALID_CHAINS"
    assert d["trace_report"]["resolution"] == "ambiguous"
    assert d["certificate_id"] is None


# ---------------------------------------------------------------- POST bookings

def test_booking_created_and_persisted(client):
    _chain(client)
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z",
        "duration_seconds": 5400,
        "note": "例行校准",
    })
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["instrument_code"] == "W"
    assert b["start_at"] == "2026-06-20T09:00:00Z"
    assert b["end_at"] == "2026-06-20T10:30:00Z"
    assert b["duration_seconds"] == 5400
    assert b["certificate_id"] == "CW-W"
    assert b["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert b["note"] == "例行校准"
    assert b["id"]

    # 持久化到 SQLite：另起请求/连接可见
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_bookings WHERE id=?", (b["id"],)
        ).fetchone()
        assert row is not None
        assert row["instrument_code"] == "W"


def test_booking_overlap_returns_409_with_clashes(client):
    _chain(client)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    # 另一台仪器在同一时间预约单工位 -> 占用冲突 409
    _chain(client, code="W2")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W2",
        "start": "2026-06-20T09:30:00Z", "duration_seconds": 3600,
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "STATION_OCCUPIED"
    assert len(err["detail"]["conflicting_bookings"]) == 1
    assert err["detail"]["conflicting_bookings"][0]["start_at"] == \
        "2026-06-20T09:00:00Z"
    # 冲突预约未写入
    rl = client.get("/api/calibration-bookings")
    assert len(rl.json()) == 1


def test_booking_touching_ends_allowed(client):
    """半开：后一段 start 恰等于前一段 end，不冲突。"""
    _chain(client)
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 3600,
    }).status_code == 201
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T10:00:00Z", "duration_seconds": 1800,
    })
    assert r.status_code == 201, r.text
    assert r.json()["end_at"] == "2026-06-20T10:30:00Z"


def test_booking_ends_after_certificate_expiry_409(client):
    # 上级证书远晚于起点证书：链成立，但预约越过起点证书截止
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-12-31T23:00:00Z",
        "duration_seconds": 7200,   # end 2027-01-01T01:00 > 截止 2027-01-01T00:00
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert err["detail"]["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert client.get("/api/calibration-bookings").json() == []


def test_booking_end_exactly_at_expiry_allowed(client):
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-12-31T23:00:00Z",
        "duration_seconds": 3600,   # end == valid_until
    })
    assert r.status_code == 201, r.text
    assert r.json()["end_at"] == "2027-01-01T00:00:00Z"


def test_booking_chain_broken_409(client):
    client.post("/api/submissions", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [], "edges": [],
    })
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 600,
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert err["detail"]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    # detail 内附完整溯源报告说明原因
    assert err["detail"]["trace_report"]["resolution"] == "broken"


def test_booking_unknown_instrument_409(client):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "GHOST",
        "start": "2026-06-20T09:00:00Z", "duration_seconds": 60,
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "INSTRUMENT_NOT_FOUND"


def test_booking_bad_body_422(client):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W",
        "start": "2026-06-20T09:00:00Z",
        "duration_seconds": -5,
    })
    assert r.status_code == 422


def test_concurrent_overlapping_bookings_single_winner(client):
    """并发确认同一时段：写事务（BEGIN IMMEDIATE）串行化，恰一个成功，
    另一个在事务内重查到已提交的占用后返回 409（不会出现两条重叠预约）。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _chain(client)
    barrier = threading.Barrier(2)
    results = []

    def post(start):
        barrier.wait(timeout=5)
        with TestClient(app) as local:
            r = local.post("/api/calibration-bookings", json={
                "instrument_code": "W",
                "start": start, "duration_seconds": 5400,
            })
            return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        for code in pool.map(post, [
            "2026-06-20T09:00:00Z",   # [09:00,10:30)
            "2026-06-20T09:30:00Z",   # [09:30,11:00) 与前者重叠
        ]):
            results.append(code)

    assert sorted(results) == [201, 409]
    rows = client.get("/api/calibration-bookings").json()
    # 恰一条落库（写事务串行化，不会出现两条重叠预约）；赢家不确定，
    # 但必为两笔之一的起点。
    assert len(rows) == 1
    assert rows[0]["start_at"] in (
        "2026-06-20T09:00:00Z", "2026-06-20T09:30:00Z")
    assert rows[0]["duration_seconds"] == 5400


def test_booking_failed_recheck_rolls_back_entire_transaction(client):
    """占用重查失败时，同一写事务内此前的任何写入均随回滚消失。

    替换确认模块内的占用重查函数使其返回一条冲突占用，验证
    confirm 路径抛 BookingConflict 且没有部分写入落库。
    """
    from datetime import datetime

    from app import booking
    from app.database import get_conn_ctx

    _chain(client)

    def fake_clashes(conn, start_s, end_s):
        return [{
            "id": "BK-FAKE", "instrument_code": "OTHER",
            "start_at": "2026-06-20T09:00:00Z", "end_at": "2026-06-20T12:00:00Z",
            "duration_seconds": 10800, "certificate_id": "CW-OTHER",
            "certificate_valid_until": "2030-01-01T00:00:00Z",
            "note": "", "created_at": "2026-06-20T00:00:00Z",
        }]

    mp = pytest.MonkeyPatch()
    mp.setattr(booking, "find_clashes", fake_clashes)
    try:
        with pytest.raises(booking.BookingConflict) as excinfo:
            with get_conn_ctx() as conn:
                conn.execute("BEGIN IMMEDIATE")
                booking.confirm_booking(
                    conn, "W", datetime(2026, 6, 20, 10), 3600)
        assert excinfo.value.code == "STATION_OCCUPIED"
    finally:
        mp.undo()

    # 事务回滚：无任何预约残留
    assert client.get("/api/calibration-bookings").json() == []


# ---------------------------------------------------------------- GET bookings

def test_list_bookings_by_instrument(client):
    _chain(client, code="W")
    _chain(client, code="W2")
    for code, ts in (("W", "09:00:00"), ("W2", "11:00:00"), ("W", "13:00:00")):
        assert client.post("/api/calibration-bookings", json={
            "instrument_code": code,
            "start": f"2026-06-20T{ts}Z", "duration_seconds": 600,
        }).status_code == 201

    all_rows = client.get("/api/calibration-bookings").json()
    assert len(all_rows) == 3
    # 按起点升序
    assert [r["start_at"] for r in all_rows] == [
        "2026-06-20T09:00:00Z", "2026-06-20T11:00:00Z", "2026-06-20T13:00:00Z",
    ]
    w_rows = client.get("/api/calibration-bookings",
                        params={"instrument": "W"}).json()
    assert {r["instrument_code"] for r in w_rows} == {"W"}
    assert len(w_rows) == 2


def test_list_bookings_unknown_instrument_404(client):
    r = client.get("/api/calibration-bookings", params={"instrument": "GHOST"})
    assert r.status_code == 404
