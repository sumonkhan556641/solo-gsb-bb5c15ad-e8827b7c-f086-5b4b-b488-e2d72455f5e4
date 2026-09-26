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
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链（同 test_calibration）。"""
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
                    json={"instruments": instruments, "certificates": certs,
                          "edges": edges})
    assert r.status_code == 201, r.text


def _book(client, code, start, duration=3600, note=""):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code, "start": start,
        "duration_seconds": duration, "note": note,
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 成功路径

def test_reschedule_two_bookings_success(client):
    """两条预约整体后移一天：时段更新、仪器与备注保留、证书快照刷新。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600, note="早班")
    b2 = _book(client, "W2", "2026-06-20T10:00:00Z", 1800, note="")

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": b2["id"], "start": "2026-06-21T10:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    # 响应按请求顺序
    assert [o["id"] for o in out] == [b1["id"], b2["id"]]
    # 缺省沿用原时长
    assert out[0]["start_at"] == "2026-06-21T09:00:00Z"
    assert out[0]["end_at"] == "2026-06-21T10:00:00Z"
    assert out[0]["duration_seconds"] == 3600
    assert out[1]["start_at"] == "2026-06-21T10:00:00Z"
    assert out[1]["end_at"] == "2026-06-21T10:30:00Z"
    assert out[1]["duration_seconds"] == 1800
    # 仪器与备注保留
    assert out[0]["instrument_code"] == "W"
    assert out[0]["note"] == "早班"
    assert out[1]["instrument_code"] == "W2"
    # 证书快照按新起点重查（同一证书，时刻不变）
    assert out[0]["certificate_id"] == "CW-W"
    assert out[1]["certificate_id"] == "CW-W2"

    # 持久化：另起连接可见新时段
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_bookings WHERE id=?", (b1["id"],)
        ).fetchone()
        assert row["start_at"] == "2026-06-21T09:00:00Z"
        assert row["note"] == "早班"


def test_reschedule_swap_slots_allowed(client):
    """本批旧占用整体排除：两条预约互换时段不视为冲突。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W2", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-20T11:00:00Z"},
            {"booking_id": b2["id"], "start": "2026-06-20T09:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    out = {o["id"]: o for o in r.json()}
    assert out[b1["id"]]["start_at"] == "2026-06-20T11:00:00Z"
    assert out[b2["id"]]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_back_to_back_allowed(client):
    """半开：新时段与既有预约（或彼此）首尾相接不冲突。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    other = _book(client, "W2", "2026-06-20T10:00:00Z", 3600)  # 不参与改期
    b1 = _book(client, "W", "2026-06-20T14:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T16:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            # 新结束恰等于 other 的起点（相接）
            {"booking_id": b1["id"], "start": "2026-06-20T09:00:00Z"},
            # 新起点恰等于 other 的结束（相接）
            {"booking_id": b2["id"], "start": "2026-06-20T11:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    rows = client.get("/api/calibration-bookings").json()
    assert len(rows) == 3
    by_id = {x["id"]: x for x in rows}
    assert by_id[other["id"]]["start_at"] == "2026-06-20T10:00:00Z"
    assert by_id[b1["id"]]["end_at"] == "2026-06-20T10:00:00Z"
    assert by_id[b2["id"]]["start_at"] == "2026-06-20T11:00:00Z"


def test_reschedule_new_duration_overrides(client):
    """显式 duration_seconds 改变时长；缺省项沿用原时长。"""
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z",
             "duration_seconds": 5400},
            {"booking_id": b2["id"], "start": "2026-06-21T11:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out[0]["duration_seconds"] == 5400
    assert out[0]["end_at"] == "2026-06-21T10:30:00Z"
    assert out[1]["duration_seconds"] == 3600
    assert out[1]["end_at"] == "2026-06-21T12:00:00Z"


def test_reschedule_onto_own_old_slot_allowed(client):
    """新时段与自身旧占用重叠（含原位不动）不视为冲突。"""
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T12:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-20T09:00:00Z"},  # 原位
            # 新段 [12:30,13:30) 与自身旧段 [12:00,13:00) 重叠：允许
            {"booking_id": b2["id"], "start": "2026-06-20T12:30:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out[0]["start_at"] == "2026-06-20T09:00:00Z"
    assert out[1]["start_at"] == "2026-06-20T12:30:00Z"
    assert out[1]["end_at"] == "2026-06-20T13:30:00Z"


def test_reschedule_refreshes_certificate_snapshot(client):
    """新起点落在更换后的证书上：证书快照更新为新起点证书。"""
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)
    assert b1["certificate_id"] == "CW-W"

    # 旧证 2027-01-01 到期，新证无缝衔接（闭区间相接，无断档）
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [
            {"id": "CW-W-2", "instrument_code": "W", "cert_number": "W-W2",
             "issued_at": "2026-12-20T00:00:00",
             "valid_from": "2027-01-01T00:00:00",
             "valid_until": "2028-06-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [{"child_cert_id": "CW-W-2", "parent_cert_id": "CR-W"}],
    })
    assert r.status_code == 201, r.text

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2027-01-02T09:00:00Z"},
            {"booking_id": b2["id"], "start": "2027-01-02T11:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    for o in r.json():
        assert o["certificate_id"] == "CW-W-2"
        assert o["certificate_valid_until"] == "2028-06-01T00:00:00Z"


# ---------------------------------------------------------------- 409 冲突

def test_reschedule_duplicate_booking_id_409(client):
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": b1["id"], "start": "2026-06-21T10:00:00Z"},
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "DUPLICATE_BOOKING_ID"
    assert err["detail"]["booking_id"] == b1["id"]


def test_reschedule_unknown_booking_409(client):
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": "BK-GHOST", "start": "2026-06-21T10:00:00Z"},
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "BOOKING_NOT_FOUND"
    assert err["detail"]["booking_id"] == "BK-GHOST"
    # 整体回滚：b1 未被改动
    rows = client.get("/api/calibration-bookings").json()
    assert rows[0]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_conflict_with_other_booking_409_rolls_back(client):
    """新时段撞上批次之外的预约：409 且整批回滚。"""
    _chain(client, code="W")
    _chain(client, code="W2")
    other = _book(client, "W2", "2026-06-21T09:30:00Z", 3600)  # 批次外占用
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},  # 撞 other
            {"booking_id": b2["id"], "start": "2026-06-21T13:00:00Z"},  # 本身合法
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "STATION_OCCUPIED"
    assert err["detail"]["booking_id"] == b1["id"]
    assert [c["id"] for c in err["detail"]["conflicting_bookings"]] == [other["id"]]
    # 整批回滚：两条都保持原时段
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[b1["id"]]["start_at"] == "2026-06-20T09:00:00Z"
    assert rows[b2["id"]]["start_at"] == "2026-06-20T11:00:00Z"


def test_reschedule_batch_internal_conflict_409(client):
    """两条新时段彼此重叠：409 且整批回滚。"""
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},  # [09,10)
            {"booking_id": b2["id"], "start": "2026-06-21T09:30:00Z"},  # [09:30,10:30)
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "STATION_OCCUPIED"
    assert err["detail"]["booking_id"] == b2["id"]
    pair = err["detail"]["conflicting_batch_bookings"]
    assert {p["booking_id"] for p in pair} == {b1["id"], b2["id"]}
    # 整批回滚
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[b1["id"]]["start_at"] == "2026-06-20T09:00:00Z"
    assert rows[b2["id"]]["start_at"] == "2026-06-20T11:00:00Z"


def test_reschedule_chain_unavailable_409(client):
    """新起点落在证书生效前：唯一有效链不可用 -> 409 并回滚。"""
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},  # 合法
            {"booking_id": b2["id"], "start": "2025-06-21T09:00:00Z"},  # 证书未生效
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert err["detail"]["booking_id"] == b2["id"]
    assert err["detail"]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert err["detail"]["trace_report"]["resolution"] == "broken"
    # 整批回滚
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[b1["id"]]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_ends_after_cert_expiry_409(client):
    """新结束越过起点证书到期 -> 409 并回滚。"""
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},  # 合法
            {"booking_id": b2["id"], "start": "2026-12-31T23:00:00Z",
             "duration_seconds": 7200},  # end 2027-01-01T01:00 > 截止
        ],
    })
    assert r.status_code == 409
    err = r.json()["detail"]
    assert err["code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert err["detail"]["booking_id"] == b2["id"]
    assert err["detail"]["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    rows = {x["id"]: x for x in client.get("/api/calibration-bookings").json()}
    assert rows[b1["id"]]["start_at"] == "2026-06-20T09:00:00Z"


def test_reschedule_end_exactly_at_expiry_allowed(client):
    """半开：新结束恰等于起点证书到期时刻，允许。"""
    _chain(client, leaf_until="2027-01-01T00:00:00Z")
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-12-31T22:00:00Z"},  # end 23:00
            {"booking_id": b2["id"], "start": "2026-12-31T23:00:00Z"},  # end == 截止
        ],
    })
    assert r.status_code == 200, r.text
    assert r.json()[1]["end_at"] == "2027-01-01T00:00:00Z"


# ---------------------------------------------------------------- 入参校验

def test_reschedule_item_count_bounds_422(client):
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    # 少于 2 条
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [{"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"}],
    })
    assert r.status_code == 422
    # 多于 10 条
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": f"BK-{i}", "start": "2026-06-21T09:00:00Z"}
            for i in range(11)
        ],
    })
    assert r.status_code == 422


def test_reschedule_invalid_duration_422(client):
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    b2 = _book(client, "W", "2026-06-20T11:00:00Z", 3600)
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z",
             "duration_seconds": 0},
            {"booking_id": b2["id"], "start": "2026-06-21T11:00:00Z"},
        ],
    })
    assert r.status_code == 422


def test_reschedule_blank_booking_id_422(client):
    _chain(client)
    b1 = _book(client, "W", "2026-06-20T09:00:00Z", 3600)
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": "   ", "start": "2026-06-21T10:00:00Z"},
        ],
    })
    assert r.status_code == 422
