"""校准工位维护封锁：登记/列表/撤销，以及对空档查询、预约确认、
批量改期、取消候补补位的半开区间封锁联动。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TRACE_DB_PATH"] = _tmp.name
os.environ["SEED_DEMO"] = "0"

from app import booking as booking_svc  # noqa: E402
from app import maintenance as maint_svc  # noqa: E402
from app.database import get_conn_ctx, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()

SLOT_START = "2026-06-20T09:00:00Z"
SLOT_END = "2026-06-20T10:00:00Z"


def _chain(client, *, code="W", leaf_until="2027-01-01T00:00:00"):
    """提交 基准 G -> 参考 R -> 工作仪器 W 的唯一有效链。"""
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
         "valid_from": "2025-06-01T00:00:00", "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": cw, "instrument_code": code, "cert_number": f"{code}-W1",
         "issued_at": "2026-01-02T00:00:00",
         "valid_from": "2026-01-01T00:00:00", "valid_until": leaf_until,
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


def _maintenance(client, start, end, reason="季度保养"):
    r = client.post("/api/calibration-maintenance-windows", json={
        "start": start, "end": end, "reason": reason,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _waitlist(client, booking_id, code, note=""):
    r = client.post(f"/api/calibration-bookings/{booking_id}/waitlist",
                    json={"instrument_code": code, "note": note})
    assert r.status_code == 201, r.text
    return r.json()


def _seed_overlapping_booking_and_waitlist(client, code, *, note="",
                                           with_waitlist=True):
    """绕过写事务封锁检查直接播种一条与维护窗口重叠的预约及其候补。

    经正式 API，维护窗口与预约不可能重叠（登记任一方都会因对方存在而
    409）；取消补位对维护窗口的重查是纵深防御，只可能命中历史脏数据或
    维护窗口在取消事务并发窗口内登记的极端情形，故测试以此方式造数据。
    返回 (booking_id, waitlist_id | None)。
    """
    with get_conn_ctx() as conn:
        bk = booking_svc.insert_booking(
            conn,
            instrument_code=code,
            start_at=SLOT_START,
            end_at=SLOT_END,
            duration_seconds=3600,
            certificate_id=f"CW-{code}",
            certificate_valid_until="2027-01-01T00:00:00Z",
            note=note,
        )
        wl = None
        if with_waitlist:
            wl = booking_svc.insert_waitlist(
                conn,
                booking_id=bk["id"],
                instrument_code=code,
                start_at=SLOT_START,
                end_at=SLOT_END,
                duration_seconds=3600,
                note=note,
            )
    return bk["id"], (wl["id"] if wl else None)


def _seed_overlapping_maintenance(client, start=SLOT_START, end=SLOT_END,
                                  reason="紧急抢修"):
    """绕过登记查重直接播种一条生效中维护窗口（与播种的预约重叠）。"""
    with get_conn_ctx() as conn:
        from datetime import datetime
        w = maint_svc.insert_window(
            conn,
            start=datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ"),
            end=datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ"),
            reason=reason,
        )
    return w["id"]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/admin/reset")
        yield c


# ---------------------------------------------------------------- 登记

def test_register_window_created_and_persisted(client):
    d = _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z",
                     reason="  激光器检修  ")
    # 原因去空白保存
    assert d["reason"] == "激光器检修"
    assert d["status"] == "active"
    assert d["revoked_at"] is None
    assert d["start_at"] == "2026-06-20T12:00:00Z"
    assert d["end_at"] == "2026-06-20T13:00:00Z"
    assert d["id"].startswith("MW-")
    assert d["created_at"]

    # 持久化到 SQLite
    with get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM station_maintenance_windows WHERE id=?", (d["id"],)
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert row["reason"] == "激光器检修"


def test_register_conflict_with_booking_returns_409_and_ids(client):
    _chain(client)
    bk = _book(client, start=SLOT_START, duration=3600)

    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T09:30:00Z", "end": "2026-06-20T10:30:00Z",
        "reason": "保养",
    })
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "STATION_UNDER_MAINTENANCE"
    conflicts = d["detail"]["conflicting_bookings"]
    assert [c["booking_id"] for c in conflicts] == [bk["id"]]
    assert d["detail"]["conflicting_maintenance_windows"] == []


def test_register_conflict_with_existing_window_returns_409(client):
    w1 = _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T14:00:00Z")

    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T13:00:00Z", "end": "2026-06-20T15:00:00Z",
        "reason": "二次保养",
    })
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "STATION_UNDER_MAINTENANCE"
    assert d["detail"]["conflicting_bookings"] == []
    ids = [w["id"] for w in d["detail"]["conflicting_maintenance_windows"]]
    assert ids == [w1["id"]]


def test_register_back_to_back_allowed(client):
    _chain(client)
    # 预约占用 [09,10)
    _book(client, start=SLOT_START, duration=3600)
    # 维护 [10,11)：起点恰在预约结束点（半开相接）允许
    w1 = _maintenance(client, SLOT_END, "2026-06-20T11:00:00Z")
    assert w1["status"] == "active"
    # 维护 [08,09)：终点恰在预约开始点，同样允许
    w2 = _maintenance(client, "2026-06-20T08:00:00Z", SLOT_START)
    assert w2["status"] == "active"
    # 维护窗口之间首尾相接也允许：[11,12) 接 [10,11)
    w3 = _maintenance(client, "2026-06-20T11:00:00Z", "2026-06-20T12:00:00Z")
    assert w3["status"] == "active"


def test_register_failure_does_not_write(client):
    _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z")
    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T12:30:00Z", "end": "2026-06-20T13:30:00Z",
        "reason": "失败的登记",
    })
    assert r.status_code == 409
    # 失败不写入：列表中仍只有先前 1 条
    listed = client.get("/api/calibration-maintenance-windows").json()
    assert len(listed) == 1
    assert all(w["reason"] != "失败的登记" for w in listed)


def test_register_revoked_window_does_not_block(client):
    w = _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z")
    assert client.delete(f"/api/calibration-maintenance-windows/{w['id']}").status_code == 200
    # 已撤销窗口不再封锁，重叠时段可登记
    w2 = _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z",
                      reason="重新封锁")
    assert w2["status"] == "active"


def test_register_invalid_interval_and_blank_reason_422(client):
    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T12:00:00Z", "end": "2026-06-20T12:00:00Z",
        "reason": "零长度",
    })
    assert r.status_code == 422, r.text

    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T13:00:00Z", "end": "2026-06-20T12:00:00Z",
        "reason": "倒置",
    })
    assert r.status_code == 422, r.text

    r = client.post("/api/calibration-maintenance-windows", json={
        "start": "2026-06-20T12:00:00Z", "end": "2026-06-20T13:00:00Z",
        "reason": "   ",
    })
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 列表

def test_list_windows_filters(client):
    w1 = _maintenance(client, "2026-06-20T09:00:00Z", "2026-06-20T10:00:00Z",
                      reason="甲")
    w2 = _maintenance(client, "2026-06-20T11:00:00Z", "2026-06-20T12:00:00Z",
                      reason="乙")
    assert client.delete(f"/api/calibration-maintenance-windows/{w2['id']}").status_code == 200

    # 默认全部，按起点升序
    all_w = client.get("/api/calibration-maintenance-windows").json()
    assert [w["id"] for w in all_w] == [w1["id"], w2["id"]]

    active = client.get("/api/calibration-maintenance-windows",
                        params={"status": "active"}).json()
    assert [w["id"] for w in active] == [w1["id"]]

    revoked = client.get("/api/calibration-maintenance-windows",
                         params={"status": "revoked"}).json()
    assert [w["id"] for w in revoked] == [w2["id"]]
    assert revoked[0]["status"] == "revoked"
    assert revoked[0]["revoked_at"] is not None

    r = client.get("/api/calibration-maintenance-windows",
                   params={"status": "bogus"})
    assert r.status_code == 422

    # 半开时间筛选：恰在 10:00 查询，[09,10) 已结束（end > 10:00 为假）
    rows = client.get("/api/calibration-maintenance-windows", params={
        "start": "2026-06-20T10:00:00Z",
    }).json()
    assert {w["id"] for w in rows} == {w2["id"]}

    # 与半开窗 [09:30, 10:30) 相交者：[09,10) 相交；[11,12) 不相交
    rows = client.get("/api/calibration-maintenance-windows", params={
        "start": "2026-06-20T09:30:00Z", "end": "2026-06-20T10:30:00Z",
    }).json()
    assert {w["id"] for w in rows} == {w1["id"]}


# ---------------------------------------------------------------- 撤销

def test_revoke_unknown_and_repeat_404(client):
    r = client.delete("/api/calibration-maintenance-windows/MW-GHOST")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_WINDOW_NOT_FOUND"

    w = _maintenance(client, "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z")
    assert client.delete(f"/api/calibration-maintenance-windows/{w['id']}").status_code == 200
    r = client.delete(f"/api/calibration-maintenance-windows/{w['id']}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "MAINTENANCE_WINDOW_NOT_FOUND"


# ---------------------------------------------------------------- 空档查询联动

def test_slots_treat_maintenance_as_busy(client):
    _chain(client)
    _book(client, start=SLOT_START, duration=3600)          # [09,10)
    _maintenance(client, "2026-06-20T10:00:00Z",
                 "2026-06-20T11:00:00Z", reason="保养")     # [10,11)

    # 从 09:00 起查 1 小时：先被预约推到 10:00，再被维护推到 11:00
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T14:00:00Z",
        "duration_seconds": 3600,
    })
    d = r.json()
    assert d["slot"]["start"] == "2026-06-20T11:00:00Z"
    assert d["slot"]["end"] == "2026-06-20T12:00:00Z"
    # busy 列预约，维护窗口单独列出，两者都参与空档计算
    assert [b["booking_id"] for b in d["busy"]]
    assert [b["start_at"] for b in d["busy"]] == [SLOT_START]
    mw = d["maintenance_windows"]
    assert len(mw) == 1
    assert mw[0]["start_at"] == "2026-06-20T10:00:00Z"
    assert mw[0]["end_at"] == "2026-06-20T11:00:00Z"
    assert mw[0]["reason"] == "保养"


def test_slots_no_free_slot_when_maintenance_covers_window(client):
    _chain(client)
    _maintenance(client, "2026-06-20T09:00:00Z",
                 "2026-06-20T12:00:00Z")
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T13:00:00Z",
        "duration_seconds": 7200,
    })
    d = r.json()
    assert d["slot"] is None
    assert d["reason_code"] == "NO_FREE_SLOT_IN_WINDOW"


def test_slots_back_to_back_with_maintenance(client):
    _chain(client)
    _maintenance(client, "2026-06-20T10:00:00Z",
                 "2026-06-20T11:00:00Z")
    # [09,10) 终点恰为维护起点 -> 可用
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.json()["slot"]["start"] == "2026-06-20T09:00:00Z"

    # 从维护结束点 11:00 起查 -> 立即可用（半开相接）
    r = client.get("/api/calibration-slots", params={
        "instrument": "W",
        "start": "2026-06-20T11:00:00Z",
        "end": "2026-06-20T12:00:00Z",
        "duration_seconds": 3600,
    })
    assert r.json()["slot"]["start"] == "2026-06-20T11:00:00Z"


def test_slots_free_after_maintenance_revoked(client):
    _chain(client)
    w = _maintenance(client, "2026-06-20T09:00:00Z",
                     "2026-06-20T13:00:00Z")
    params = {
        "instrument": "W",
        "start": "2026-06-20T09:00:00Z",
        "end": "2026-06-20T13:00:00Z",
        "duration_seconds": 3600,
    }
    assert client.get("/api/calibration-slots", params=params).json()["slot"] is None

    assert client.delete(f"/api/calibration-maintenance-windows/{w['id']}").status_code == 200
    d = client.get("/api/calibration-slots", params=params).json()
    assert d["slot"]["start"] == "2026-06-20T09:00:00Z"
    assert d["maintenance_windows"] == []


# ---------------------------------------------------------------- 确认预约联动

def test_confirm_booking_blocked_by_maintenance_409(client):
    _chain(client)
    w = _maintenance(client, "2026-06-20T09:00:00Z",
                     "2026-06-20T10:00:00Z", reason="年度检修")
    r = client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T09:30:00Z",
        "duration_seconds": 3600,
    })
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "STATION_UNDER_MAINTENANCE"
    ids = [x["maintenance_window_id"]
           for x in d["detail"]["conflicting_maintenance_windows"]]
    assert ids == [w["id"]]
    # 失败不写入
    assert client.get("/api/calibration-bookings").json() == []


def test_confirm_booking_back_to_back_with_maintenance(client):
    _chain(client)
    _maintenance(client, "2026-06-20T09:00:00Z", "2026-06-20T10:00:00Z")
    # 恰在维护结束点开始 / 恰在维护起点结束：半开相接允许
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T10:00:00Z",
        "duration_seconds": 3600,
    }).status_code == 201
    assert client.post("/api/calibration-bookings", json={
        "instrument_code": "W", "start": "2026-06-20T08:00:00Z",
        "duration_seconds": 3600,
    }).status_code == 201


# ---------------------------------------------------------------- 批量改期联动

def test_reschedule_blocked_by_maintenance_rolls_back(client):
    _chain(client)
    _chain(client, code="W2")
    b1 = _book(client, code="W", start=SLOT_START, duration=3600)
    b2 = _book(client, code="W2", start="2026-06-20T10:00:00Z",
               duration=3600)
    _maintenance(client, "2026-06-20T14:00:00Z", "2026-06-20T15:00:00Z")

    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-20T14:00:00Z"},
            {"booking_id": b2["id"], "start": "2026-06-20T15:00:00Z"},
        ],
    })
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["code"] == "STATION_UNDER_MAINTENANCE"
    assert d["detail"]["booking_id"] == b1["id"]

    # 整体回滚：两条预约保持原时段
    rows = {b["id"]: b for b in client.get("/api/calibration-bookings").json()}
    assert rows[b1["id"]]["start_at"] == SLOT_START
    assert rows[b2["id"]]["start_at"] == "2026-06-20T10:00:00Z"


def test_reschedule_away_from_maintenance_succeeds(client):
    _chain(client)
    _chain(client, code="W2")
    b1 = _book(client, start=SLOT_START, duration=3600)
    b2 = _book(client, code="W2", start="2026-06-20T10:00:00Z",
               duration=3600)
    _maintenance(client, "2026-06-20T14:00:00Z", "2026-06-20T15:00:00Z")

    # 移入维护时段：409（整批回滚已由上一个用例断言，这里只看原因码）
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-20T14:00:00Z"},
            {"booking_id": b2["id"], "start": "2026-06-20T15:00:00Z"},
        ],
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "STATION_UNDER_MAINTENANCE"

    # 移到维护之后（新起点 15:00 恰为维护结束点，半开相接允许）：成功
    r = client.post("/api/calibration-bookings/reschedule", json={
        "items": [
            {"booking_id": b1["id"], "start": "2026-06-20T15:00:00Z"},
            {"booking_id": b2["id"], "start": "2026-06-20T16:00:00Z"},
        ],
    })
    assert r.status_code == 200, r.text
    starts = {x["id"]: x["start_at"] for x in r.json()}
    assert starts[b1["id"]] == "2026-06-20T15:00:00Z"
    assert starts[b2["id"]] == "2026-06-20T16:00:00Z"


# ---------------------------------------------------------------- 取消补位联动

def test_cancel_promotion_skipped_due_to_maintenance(client):
    _chain(client)
    _chain(client, code="W2")
    # 播种一条 09:00~10:00 的预约及 INST-W2 候补（经 API 维护与预约不能
    # 重叠，故以播种方式构造补位重查命中维护封锁的纵深防御场景）
    bk_id, _ = _seed_overlapping_booking_and_waitlist(client, "W2", note="候补")
    # 维护恰好封锁该候补时段（直接播种，绕过登记时对既有预约的查重）
    _seed_overlapping_maintenance(client, reason="紧急抢修")

    r = client.delete(f"/api/calibration-bookings/{bk_id}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["promoted"] is False
    assert d["promotion"] is None
    assert len(d["skipped"]) == 1
    assert d["skipped"][0]["reason_code"] == "STATION_UNDER_MAINTENANCE"
    assert "维护" in d["skipped"][0]["reason"]
    # 时段未补位：无新预约
    assert client.get("/api/calibration-bookings").json() == []
    # 候补标记 failed 并记录跳过原因
    wl = client.get("/api/calibration-waitlist",
                    params={"booking_id": bk_id}).json()
    assert wl[0]["status"] == "failed"
    assert wl[0]["status_reason"] == d["skipped"][0]["reason"]
    assert wl[0]["promoted_booking_id"] is None


def test_cancel_promotion_skips_chain_then_maintenance_in_order(client):
    _chain(client)
    _chain(client, code="W2")
    _instrument_only(client, "T1")  # 无证书：补位重验链不可用
    # 只播种预约（不附候补），再按 T1（链断裂）→ W2（撞维护）顺序排队
    bk_id, _ = _seed_overlapping_booking_and_waitlist(
        client, "W2", with_waitlist=False
    )
    _waitlist(client, bk_id, "T1")   # 队列第 1：链断裂
    _waitlist(client, bk_id, "W2")   # 队列第 2：链可用但撞维护
    _seed_overlapping_maintenance(client)

    d = client.delete(f"/api/calibration-bookings/{bk_id}").json()
    assert d["promoted"] is False
    assert len(d["skipped"]) == 2
    assert d["skipped"][0]["instrument_code"] == "T1"
    assert d["skipped"][0]["reason_code"] != "STATION_UNDER_MAINTENANCE"
    assert d["skipped"][1]["instrument_code"] == "W2"
    assert d["skipped"][1]["reason_code"] == "STATION_UNDER_MAINTENANCE"


def test_cancel_promotion_succeeds_after_maintenance_revoked(client):
    _chain(client)
    _chain(client, code="W2")
    bk_id, _ = _seed_overlapping_booking_and_waitlist(client, "W2")
    w = _seed_overlapping_maintenance(client)
    # 撤销维护后取消：候补正常补位
    assert client.delete(f"/api/calibration-maintenance-windows/{w}").status_code == 200

    d = client.delete(f"/api/calibration-bookings/{bk_id}").json()
    assert d["promoted"] is True
    assert d["skipped"] == []
    new_booking = d["promotion"]["booking"]
    assert new_booking["instrument_code"] == "W2"
    assert new_booking["start_at"] == SLOT_START
    assert new_booking["end_at"] == SLOT_END
