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

WINDOW = {"start": "2026-06-20T00:00:00Z", "end": "2026-06-21T00:00:00Z"}


def _chain(client, *, code="W"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链（证书 id 以仪器编码为后缀）。"""
    cg, cr, cw = f"CG-{code}", f"CR-{code}", f"CW-{code}"
    r = client.post("/api/submissions", json={
        "instruments": [
            {"code": f"G-{code}", "name": f"国家基准-{code}",
             "is_national_standard": True},
            {"code": f"R-{code}", "name": f"参考标准器-{code}"},
            {"code": code, "name": "工作仪器"},
        ],
        "certificates": [
            {"id": cg, "instrument_code": f"G-{code}", "cert_number": f"{code}-G1",
             "issued_at": "2024-01-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00",
             "uncertainty": 0.0001, "unit": "mm"},
            {"id": cr, "instrument_code": f"R-{code}", "cert_number": f"{code}-R1",
             "issued_at": "2025-06-01T00:00:00",
             "valid_from": "2025-06-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
             "issued_at": "2026-01-02T00:00:00",
             "valid_from": "2026-01-01T00:00:00", "valid_until": "2027-01-01T00:00:00",
             "uncertainty": 0.01, "unit": "mm"},
        ],
        "edges": [
            {"child_cert_id": cw, "parent_cert_id": cr},
            {"child_cert_id": cr, "parent_cert_id": cg},
        ],
    })
    assert r.status_code == 201, r.text


def _recert(client, code, valid_until, *,
            valid_from="2026-01-01T00:00:00", issued_at="2026-01-02T00:00:00"):
    """按原字段重写某仪器的起点证书（仅调整有效区间），服务端重算摘要。"""
    r = client.post("/api/certificates", json={
        "id": f"CW-{code}", "instrument_code": code, "cert_number": f"{code}-W1",
        "issued_at": issued_at, "valid_from": valid_from,
        "valid_until": valid_until, "uncertainty": 0.01, "unit": "mm"})
    assert r.status_code == 201, r.text


def _book(client, code, start, duration=3600):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code, "start": start, "duration_seconds": duration})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 入参与窗口

def test_inspection_empty_window(client):
    r = client.get("/api/calibration-bookings/inspection", params=WINDOW)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["window_start"] == WINDOW["start"]
    assert d["window_end"] == WINDOW["end"]
    assert d["summary"] == {"total": 0, "valid": 0, "changed": 0, "invalid": 0}
    assert d["items"] == []


def test_inspection_window_must_be_strictly_ordered(client):
    # start == end -> 422
    r = client.get("/api/calibration-bookings/inspection", params={
        "start": "2026-06-20T09:00:00Z", "end": "2026-06-20T09:00:00Z"})
    assert r.status_code == 422
    assert "严格早于" in r.text
    # start > end -> 422
    r = client.get("/api/calibration-bookings/inspection", params={
        "start": "2026-06-21T00:00:00Z", "end": "2026-06-20T00:00:00Z"})
    assert r.status_code == 422
    assert "严格早于" in r.text


def test_inspection_missing_params_422(client):
    assert client.get("/api/calibration-bookings/inspection").status_code == 422
    assert client.get(
        "/api/calibration-bookings/inspection",
        params={"start": "2026-06-20T00:00:00Z"},
    ).status_code == 422


def test_inspection_half_open_window_filter(client):
    _chain(client)
    # 三条预约：08:00 / 09:00 / 10:00（半开占用互不冲突）
    _book(client, "W", "2026-06-20T08:00:00Z")
    _book(client, "W", "2026-06-20T09:00:00Z")
    _book(client, "W", "2026-06-20T10:00:00Z")

    # 半开窗 [09:00, 10:00)：起点恰在窗起点的计入，恰在窗终点的不计入
    r = client.get("/api/calibration-bookings/inspection", params={
        "start": "2026-06-20T09:00:00Z", "end": "2026-06-20T10:00:00Z"})
    d = r.json()
    assert [i["start_at"] for i in d["items"]] == ["2026-06-20T09:00:00Z"]
    assert d["summary"]["total"] == 1

    # 全天窗：三条全部命中，按开始时刻升序
    r = client.get("/api/calibration-bookings/inspection", params={
        "start": "2026-06-20T00:00:00Z", "end": "2026-06-21T00:00:00Z"})
    d = r.json()
    assert [i["start_at"] for i in d["items"]] == [
        "2026-06-20T08:00:00Z",
        "2026-06-20T09:00:00Z",
        "2026-06-20T10:00:00Z",
    ]
    assert d["summary"] == {"total": 3, "valid": 3, "changed": 0, "invalid": 0}


# ---------------------------------------------------------------- 状态判定

def test_inspection_valid_booking(client):
    _chain(client)
    bid = _book(client, "W", "2026-06-20T09:00:00Z")
    r = client.get("/api/calibration-bookings/inspection", params=WINDOW)
    d = r.json()
    (it,) = d["items"]
    assert it["booking_id"] == bid
    assert it["instrument_code"] == "W"
    assert it["status"] == "valid"
    assert it["reason_code"] is None
    assert it["reason"] is None
    # 当前证书与确认时快照一致
    assert it["current_certificate_id"] == "CW-W"
    assert it["current_certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert it["snapshot_certificate_id"] == "CW-W"
    assert it["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"


def test_inspection_changed_when_valid_until_moves(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 起点证书到期提前，但仍覆盖预约结束（10:00）-> changed 而非 invalid
    _recert(client, "W", "2026-11-01T00:00:00")
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "changed"
    assert it["reason_code"] == "CERTIFICATE_SNAPSHOT_CHANGED"
    assert it["current_certificate_id"] == "CW-W"
    assert it["current_certificate_valid_until"] == "2026-11-01T00:00:00Z"
    assert it["snapshot_certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert "2026-11-01" in it["reason"]


def test_inspection_changed_when_start_certificate_switches(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 旧证书截短至 2026-06-01，新证书 CW-W2 与之首尾相接覆盖预约开始时刻
    _recert(client, "W", "2026-06-01T00:00:00")
    assert client.post("/api/certificates", json={
        "id": "CW-W2", "instrument_code": "W", "cert_number": "W-W2",
        "issued_at": "2026-06-02T00:00:00",
        "valid_from": "2026-06-01T00:00:00", "valid_until": "2027-06-01T00:00:00",
        "uncertainty": 0.01, "unit": "mm"}).status_code == 201
    assert client.post("/api/edges", json={
        "child_cert_id": "CW-W2", "parent_cert_id": "CR-W"}).status_code == 201
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "changed"
    assert it["reason_code"] == "CERTIFICATE_SNAPSHOT_CHANGED"
    assert it["current_certificate_id"] == "CW-W2"
    assert it["snapshot_certificate_id"] == "CW-W"


def test_inspection_invalid_when_chain_broken_at_start(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 起点证书改为 7 月才生效：预约开始时刻无有效证书，链断裂
    _recert(client, "W", "2027-01-01T00:00:00",
            valid_from="2026-07-01T00:00:00", issued_at="2026-07-02T00:00:00")
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "invalid"
    assert it["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert it["current_certificate_id"] is None
    assert it["current_certificate_valid_until"] is None
    # 原快照仍随条目返回
    assert it["snapshot_certificate_id"] == "CW-W"


def test_inspection_invalid_when_deadline_insufficient(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 证书在预约开始时刻仍有效，但 09:30 到期、早于预约结束 10:00
    _recert(client, "W", "2026-06-20T09:30:00")
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "invalid"
    assert it["reason_code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"
    assert it["current_certificate_id"] == "CW-W"
    assert it["current_certificate_valid_until"] == "2026-06-20T09:30:00Z"


def test_inspection_invalid_when_multiple_active_certs(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 再登记一张在预约开始时刻同样有效的证书 -> 起点不唯一
    assert client.post("/api/certificates", json={
        "id": "CW-WX", "instrument_code": "W", "cert_number": "W-WX",
        "issued_at": "2026-06-02T00:00:00",
        "valid_from": "2026-01-01T00:00:00", "valid_until": "2028-01-01T00:00:00",
        "uncertainty": 0.01, "unit": "mm"}).status_code == 201
    assert client.post("/api/edges", json={
        "child_cert_id": "CW-WX", "parent_cert_id": "CR-W"}).status_code == 201
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "invalid"
    assert it["reason_code"] == "MULTIPLE_ACTIVE_CERTIFICATES"


def test_inspection_invalid_when_digest_tampered(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 显式提交错误摘要 -> 链校验失败（有到基准的链但未通过校验）
    r = client.post("/api/certificates", json={
        "id": "CW-W", "instrument_code": "W", "cert_number": "W-W1",
        "issued_at": "2026-01-02T00:00:00",
        "valid_from": "2026-01-01T00:00:00", "valid_until": "2027-01-01T00:00:00",
        "uncertainty": 0.01, "unit": "mm",
        "signature_digest": "00" * 32})
    assert r.status_code == 201, r.text
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "invalid"
    assert it["reason_code"] == "ALL_CHAINS_FAILED_CHECKS"


def test_inspection_invalid_when_instrument_deleted(client):
    _chain(client)
    _book(client, "W", "2026-06-20T09:00:00Z")
    # 仪器档案被直接移除（无公开删除接口，直连库模拟；预约行保留）
    with get_conn_ctx() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM instruments WHERE code='W'")
        conn.execute("PRAGMA foreign_keys = ON")
    (it,) = client.get(
        "/api/calibration-bookings/inspection", params=WINDOW
    ).json()["items"]
    assert it["status"] == "invalid"
    assert it["reason_code"] == "INSTRUMENT_NOT_FOUND"
    assert it["current_certificate_id"] is None


# ---------------------------------------------------------------- 汇总与排序

def test_inspection_summary_counts_and_stable_sort(client):
    for code in ("WA", "WB", "WC"):
        _chain(client, code=code)
    # 三天各一条预约（单工位不重叠）
    _book(client, "WA", "2026-06-20T09:00:00Z")   # 保持 valid
    _book(client, "WB", "2026-06-21T09:00:00Z")   # 到期提前 -> changed
    _book(client, "WC", "2026-06-22T09:00:00Z")   # 起点无有效证书 -> invalid
    _recert(client, "WB", "2026-11-01T00:00:00")
    _recert(client, "WC", "2027-01-01T00:00:00",
            valid_from="2026-07-01T00:00:00", issued_at="2026-07-02T00:00:00")

    r = client.get("/api/calibration-bookings/inspection", params={
        "start": "2026-06-20T00:00:00Z", "end": "2026-06-23T00:00:00Z"})
    d = r.json()
    assert d["summary"] == {"total": 3, "valid": 1, "changed": 1, "invalid": 1}
    assert [i["instrument_code"] for i in d["items"]] == ["WA", "WB", "WC"]
    assert [i["status"] for i in d["items"]] == ["valid", "changed", "invalid"]


def test_inspection_sort_tiebreak_by_booking_id(client):
    _chain(client)
    # 同一开始时刻的两条预约（直连库写入，绕开工位占用校验），按预约 id 决胜
    from app.booking import insert_booking

    with get_conn_ctx() as conn:
        insert_booking(
            conn, booking_id="BK-TIE-B", instrument_code="W",
            start_at="2026-06-20T09:00:00Z", end_at="2026-06-20T10:00:00Z",
            duration_seconds=3600, certificate_id="CW-W",
            certificate_valid_until="2027-01-01T00:00:00Z")
        insert_booking(
            conn, booking_id="BK-TIE-A", instrument_code="W",
            start_at="2026-06-20T09:00:00Z", end_at="2026-06-20T10:00:00Z",
            duration_seconds=3600, certificate_id="CW-W",
            certificate_valid_until="2027-01-01T00:00:00Z")
    d = client.get("/api/calibration-bookings/inspection", params=WINDOW).json()
    assert [i["booking_id"] for i in d["items"]] == ["BK-TIE-A", "BK-TIE-B"]


# ---------------------------------------------------------------- 只读语义

def test_inspection_is_read_only(client):
    _chain(client)
    bid = _book(client, "W", "2026-06-20T09:00:00Z")
    before = client.get("/api/calibration-bookings").json()
    first = client.get("/api/calibration-bookings/inspection", params=WINDOW)
    second = client.get("/api/calibration-bookings/inspection", params=WINDOW)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    after = client.get("/api/calibration-bookings").json()
    # 巡检不产生任何写副作用：预约列表（含证书快照）保持原样
    assert after == before
    assert after[0]["id"] == bid
    assert after[0]["certificate_id"] == "CW-W"
