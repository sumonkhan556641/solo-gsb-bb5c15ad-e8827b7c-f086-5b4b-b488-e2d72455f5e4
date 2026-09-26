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

SLOT_START = "2026-06-20T09:00:00Z"
SLOT_END = "2026-06-20T10:00:00Z"


def _chain(client, *, parent_until="2030-01-01T00:00:00",
           leaf_until="2027-01-01T00:00:00",
           leaf_from="2026-01-01T00:00:00",
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
         "issued_at": "2026-01-02T00:00:00",
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


def _instrument_only(client, code):
    """只登记仪器、不给任何证书：该仪器在任意时点溯源链均断裂。"""
    r = client.post("/api/submissions", json={
        "instruments": [{"code": code, "name": "无证书仪器"}],
        "certificates": [], "edges": [],
    })
    assert r.status_code == 201, r.text


def _book(client, code="W", start=SLOT_START, duration=3600, note=""):
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": code, "start": start,
        "duration_seconds": duration, "note": note,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _waitlist(client, booking_id, code, note=""):
    r = client.post(f"/api/calibration-bookings/{booking_id}/waitlist",
                    json={"instrument_code": code, "note": note})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 候补登记

def test_register_waitlist_created_and_queued(client):
    _chain(client)
    _chain(client, code="W2")
    bk = _book(client)

    e1 = _waitlist(client, bk["id"], "W2", note="加急")
    assert e1["booking_id"] == bk["id"]
    assert e1["instrument_code"] == "W2"
    # 候补沿用目标预约的起止时段
    assert e1["start_at"] == SLOT_START
    assert e1["end_at"] == SLOT_END
    assert e1["duration_seconds"] == 3600
    assert e1["note"] == "加急"
    assert e1["status"] == "waiting"
    assert e1["status_reason"] is None
    assert e1["promoted_booking_id"] is None
    assert e1["queue_position"] == 1
    assert e1["processed_at"] is None
    assert e1["id"].startswith("WL-")

    # 按登记先后排队：第二名 queue_position=2
    e2 = _waitlist(client, bk["id"], "W")
    assert e2["queue_position"] == 2

    # 持久化到 SQLite：另起连接可见
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM calibration_waitlist WHERE id=?", (e1["id"],)
        ).fetchone()
        assert row is not None
        assert row["booking_id"] == bk["id"]
        assert row["status"] == "waiting"


def test_register_waitlist_unknown_booking_404(client):
    _chain(client)
    r = client.post("/api/calibration-bookings/BK-GHOST/waitlist",
                    json={"instrument_code": "W"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "BOOKING_NOT_FOUND"


def test_register_waitlist_unknown_instrument_409(client):
    _chain(client)
    bk = _book(client)
    r = client.post(f"/api/calibration-bookings/{bk['id']}/waitlist",
                    json={"instrument_code": "GHOST"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "INSTRUMENT_NOT_FOUND"


def test_register_waitlist_bad_body_422(client):
    _chain(client)
    bk = _book(client)
    r = client.post(f"/api/calibration-bookings/{bk['id']}/waitlist",
                    json={"instrument_code": ""})
    assert r.status_code == 422


def test_register_on_cancelled_booking_404(client):
    _chain(client)
    bk = _book(client)
    assert client.delete(f"/api/calibration-bookings/{bk['id']}").status_code == 200
    r = client.post(f"/api/calibration-bookings/{bk['id']}/waitlist",
                    json={"instrument_code": "W"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 状态查询

def test_list_waitlist_filters(client):
    _chain(client)
    _chain(client, code="W2")
    bk1 = _book(client, code="W", start=SLOT_START)
    bk2 = _book(client, code="W2", start="2026-06-20T11:00:00Z")
    e1 = _waitlist(client, bk1["id"], "W2")
    e2 = _waitlist(client, bk1["id"], "W")
    e3 = _waitlist(client, bk2["id"], "W")

    all_rows = client.get("/api/calibration-waitlist").json()
    assert [e["id"] for e in all_rows] == [e1["id"], e2["id"], e3["id"]]

    by_booking = client.get("/api/calibration-waitlist",
                            params={"booking_id": bk1["id"]}).json()
    assert [e["id"] for e in by_booking] == [e1["id"], e2["id"]]
    assert [e["queue_position"] for e in by_booking] == [1, 2]

    by_instrument = client.get("/api/calibration-waitlist",
                               params={"instrument": "W2"}).json()
    assert [e["id"] for e in by_instrument] == [e1["id"]]

    by_status = client.get("/api/calibration-waitlist",
                           params={"status": "waiting"}).json()
    assert len(by_status) == 3
    assert client.get("/api/calibration-waitlist",
                      params={"status": "promoted"}).json() == []


def test_list_waitlist_unknown_instrument_404(client):
    r = client.get("/api/calibration-waitlist", params={"instrument": "GHOST"})
    assert r.status_code == 404


def test_list_waitlist_bad_status_422(client):
    r = client.get("/api/calibration-waitlist", params={"status": "bogus"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 取消与补位

def test_cancel_without_waitlist(client):
    _chain(client)
    bk = _book(client)
    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["cancelled_booking"]["id"] == bk["id"]
    assert d["waitlist_processed"] == 0
    assert d["promoted"] is False
    assert d["promotion"] is None
    assert d["skipped"] == []
    assert d["expired_waitlist_ids"] == []
    # 占用已释放
    assert client.get("/api/calibration-bookings").json() == []


def test_cancel_promotes_first_qualified(client):
    _chain(client)
    _chain(client, code="W2")
    bk = _book(client, code="W")
    e = _waitlist(client, bk["id"], "W2", note="候补贴")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["promoted"] is True
    assert d["waitlist_processed"] == 1
    promo = d["promotion"]
    assert promo["waitlist_id"] == e["id"]
    assert promo["instrument_code"] == "W2"
    assert promo["queue_position"] == 1
    nb = promo["booking"]
    # 补位预约沿用原时段与时长，保存起点证书快照与候补备注
    assert nb["id"] != bk["id"]
    assert nb["instrument_code"] == "W2"
    assert nb["start_at"] == SLOT_START
    assert nb["end_at"] == SLOT_END
    assert nb["duration_seconds"] == 3600
    assert nb["certificate_id"] == "CW-W2"
    assert nb["certificate_valid_until"] == "2027-01-01T00:00:00Z"
    assert nb["note"] == "候补贴"

    # 旧预约已删除，新预约占用同一时段
    rows = client.get("/api/calibration-bookings").json()
    assert [b["id"] for b in rows] == [nb["id"]]

    # 候补记录转为 promoted 终态
    entry = client.get("/api/calibration-waitlist",
                       params={"booking_id": bk["id"]}).json()[0]
    assert entry["status"] == "promoted"
    assert entry["promoted_booking_id"] == nb["id"]
    assert entry["queue_position"] is None
    assert entry["processed_at"] is not None


def test_cancel_skips_unqualified_and_promotes_next(client):
    """队列顺序重验：断裂链、证书到期不足依次被跳过，首个合格者补位。"""
    _chain(client)
    _instrument_only(client, "WB")                      # 无证书 -> 链断裂
    _chain(client, code="WE", leaf_until="2026-06-20T09:30:00Z")  # 证书早夭
    _chain(client, code="W2")
    bk = _book(client, code="W")
    e1 = _waitlist(client, bk["id"], "WB")
    e2 = _waitlist(client, bk["id"], "WE")
    e3 = _waitlist(client, bk["id"], "W2")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promoted"] is True
    assert d["promotion"]["waitlist_id"] == e3["id"]
    assert d["promotion"]["queue_position"] == 3
    assert d["promotion"]["booking"]["instrument_code"] == "W2"

    skipped = d["skipped"]
    assert [s["waitlist_id"] for s in skipped] == [e1["id"], e2["id"]]
    assert skipped[0]["reason_code"] == "NO_ACTIVE_CERTIFICATE"
    assert skipped[1]["reason_code"] == "BOOKING_ENDS_AFTER_CERT_EXPIRY"

    rows = {e["id"]: e for e in client.get(
        "/api/calibration-waitlist", params={"booking_id": bk["id"]}).json()}
    assert rows[e1["id"]]["status"] == "failed"
    assert "溯源链" in rows[e1["id"]]["status_reason"]
    assert rows[e2["id"]]["status"] == "failed"
    assert "到期" in rows[e2["id"]]["status_reason"]
    assert rows[e3["id"]]["status"] == "promoted"


def test_cancel_all_unqualified_frees_slot(client):
    """全部候补不合格：无人补位，时段真正释放，候补逐条给出失败原因。"""
    _chain(client)
    _instrument_only(client, "WB")
    bk = _book(client, code="W")
    e1 = _waitlist(client, bk["id"], "WB")
    e2 = _waitlist(client, bk["id"], "WB")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promoted"] is False
    assert d["promotion"] is None
    assert [s["waitlist_id"] for s in d["skipped"]] == [e1["id"], e2["id"]]
    assert all(s["reason_code"] == "NO_ACTIVE_CERTIFICATE"
               for s in d["skipped"])
    assert client.get("/api/calibration-bookings").json() == []
    # 时段已空：空档查询立即可用
    slots = client.get("/api/calibration-slots", params={
        "instrument": "W", "start": SLOT_START,
        "end": "2026-06-20T12:00:00Z", "duration_seconds": 3600,
    }).json()
    assert slots["slot"]["start"] == SLOT_START


def test_cancel_expires_entries_after_promoted(client):
    """中签者之后、未及评估的候补中条目：时段已被补位，标记失效。"""
    _chain(client)
    _chain(client, code="W2")
    _chain(client, code="W3")
    bk = _book(client, code="W")
    e1 = _waitlist(client, bk["id"], "W2")
    e2 = _waitlist(client, bk["id"], "W3")
    e3 = _waitlist(client, bk["id"], "W")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promotion"]["waitlist_id"] == e1["id"]
    assert d["skipped"] == []
    assert d["expired_waitlist_ids"] == [e2["id"], e3["id"]]

    rows = {e["id"]: e for e in client.get(
        "/api/calibration-waitlist", params={"booking_id": bk["id"]}).json()}
    assert rows[e1["id"]]["status"] == "promoted"
    assert rows[e2["id"]]["status"] == "expired"
    assert "补位" in rows[e2["id"]]["status_reason"]
    assert rows[e3["id"]]["status"] == "expired"


def test_cancel_station_conflict_skips_entry(client):
    """剩余预约与候补时段半开冲突：该申请被跳过（占用重验不沿用旧结果）。"""
    from app.booking import insert_booking

    _chain(client)
    _chain(client, code="W2")
    bk = _book(client, code="W")
    e = _waitlist(client, bk["id"], "W2")
    # 直接落库一条与目标时段重叠的“剩余预约”（绕过确认校验，
    # 模拟占用在登记候补之后发生变化）
    with get_conn_ctx() as conn:
        insert_booking(
            conn, booking_id="BK-SQUAT", instrument_code="W2",
            start_at="2026-06-20T09:30:00Z", end_at="2026-06-20T10:30:00Z",
            duration_seconds=3600, certificate_id="CW-W2",
            certificate_valid_until="2027-01-01T00:00:00Z",
        )

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promoted"] is False
    assert d["skipped"][0]["waitlist_id"] == e["id"]
    assert d["skipped"][0]["reason_code"] == "STATION_OCCUPIED"
    entry = client.get("/api/calibration-waitlist").json()[0]
    assert entry["status"] == "failed"
    assert "占用" in entry["status_reason"]


def test_cancel_promotion_respects_half_open_neighbors(client):
    """半开：与相邻预约首尾相接的候补时段可正常补位。"""
    _chain(client)
    _chain(client, code="W2")
    _book(client, code="W2", start="2026-06-20T08:00:00Z")   # [08,09)
    bk = _book(client, code="W", start=SLOT_START)           # [09,10) 目标
    _book(client, code="W2", start="2026-06-20T10:00:00Z")   # [10,11)
    e = _waitlist(client, bk["id"], "W2")

    r = client.delete(f"/api/calibration-bookings/{bk['id']}")
    d = r.json()
    assert d["promoted"] is True
    assert d["promotion"]["waitlist_id"] == e["id"]
    assert d["promotion"]["booking"]["start_at"] == SLOT_START
    assert len(client.get("/api/calibration-bookings").json()) == 3


def test_repeated_cancel_no_double_promotion(client):
    """重复取消不得重复补位：第二次取消返回 404，预约数量不变。"""
    _chain(client)
    _chain(client, code="W2")
    _chain(client, code="W3")
    bk = _book(client, code="W")
    _waitlist(client, bk["id"], "W2")
    _waitlist(client, bk["id"], "W3")

    r1 = client.delete(f"/api/calibration-bookings/{bk['id']}")
    assert r1.status_code == 200
    assert r1.json()["promoted"] is True
    # 队列中第二人未及评估即失效，不会留待下次补位
    assert len(r1.json()["expired_waitlist_ids"]) == 1

    r2 = client.delete(f"/api/calibration-bookings/{bk['id']}")
    assert r2.status_code == 404
    assert r2.json()["detail"]["code"] == "BOOKING_NOT_FOUND"
    # 只有补位产生的一条预约，未发生第二次补位
    rows = client.get("/api/calibration-bookings").json()
    assert len(rows) == 1
    assert rows[0]["instrument_code"] == "W2"


def test_cancel_unknown_booking_404(client):
    r = client.delete("/api/calibration-bookings/BK-GHOST")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "BOOKING_NOT_FOUND"


def test_concurrent_cancel_single_promotion(client):
    """并发取消同一预约：写事务串行化，恰一个 200 完成补位，另一个 404。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _chain(client)
    _chain(client, code="W2")
    bk = _book(client, code="W")
    _waitlist(client, bk["id"], "W2")

    barrier = threading.Barrier(2)

    def cancel():
        barrier.wait(timeout=5)
        with TestClient(app) as local:
            return local.delete(
                f"/api/calibration-bookings/{bk['id']}").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: cancel(), range(2)))

    assert sorted(results) == [200, 404]
    rows = client.get("/api/calibration-bookings").json()
    assert len(rows) == 1
    assert rows[0]["instrument_code"] == "W2"


# ---------------------------------------------------------------- 改期使候补失效

def test_reschedule_expires_waiting_entries(client):
    """目标预约改期：其未补位候补随旧时段失效；时段未变的预约队列不受影响。"""
    _chain(client)
    _chain(client, code="W2")
    bk_a = _book(client, code="W", start=SLOT_START)          # [09,10)
    bk_b = _book(client, code="W2", start="2026-06-20T11:00:00Z")  # [11,12)
    e_a = _waitlist(client, bk_a["id"], "W2")
    e_b = _waitlist(client, bk_b["id"], "W")

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": bk_a["id"], "start": "2026-06-21T09:00:00Z"},
            # bk_b 时段不变（原起点原时长），其候补队列应保持候补中
            {"booking_id": bk_b["id"], "start": "2026-06-20T11:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text

    rows = {e["id"]: e for e in client.get("/api/calibration-waitlist").json()}
    assert rows[e_a["id"]]["status"] == "expired"
    assert "改期" in rows[e_a["id"]]["status_reason"]
    assert rows[e_a["id"]]["processed_at"] is not None
    assert rows[e_b["id"]]["status"] == "waiting"
    assert rows[e_b["id"]]["queue_position"] == 1


def test_reschedule_failed_keeps_waitlist(client):
    """改期整批回滚时，候补队列不受影响（同一事务）。"""
    _chain(client)
    _chain(client, code="W2")
    bk_a = _book(client, code="W", start=SLOT_START)
    bk_b = _book(client, code="W2", start="2026-06-20T11:00:00Z")
    _book(client, code="W2", start="2026-06-20T13:00:00Z")   # 批次外占用
    e_a = _waitlist(client, bk_a["id"], "W2")

    # bk_b 新时段与批次外预约 [13,14) 冲突 -> 整批 409 回滚
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": bk_a["id"], "start": "2026-06-21T09:00:00Z"},
            {"booking_id": bk_b["id"], "start": "2026-06-20T13:00:00Z"},
        ],
    })
    assert r.status_code == 409
    entry = client.get("/api/calibration-waitlist").json()[0]
    assert entry["id"] == e_a["id"]
    assert entry["status"] == "waiting"
    assert entry["queue_position"] == 1
