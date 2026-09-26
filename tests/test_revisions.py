"""证书修订历史：版本快照、连续追加、同事务回滚、预检不留版本与字段差异。"""
import sqlite3

from app import database
from app.database import get_conn_ctx

CERT_W = {
    "id": "CW", "instrument_code": "W", "cert_number": "W-1",
    "issued_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_until": "2027-01-01T00:00:00Z",
    "uncertainty": 0.01, "unit": "mm",
}


def _post_instrument(client, code="W"):
    r = client.post("/api/instruments", json={"code": code, "name": f"仪器-{code}"})
    assert r.status_code == 201, r.text


def _post_cert(client, cert=None):
    r = client.post("/api/certificates", json=cert or CERT_W)
    assert r.status_code == 201, r.text
    return r


def _revisions(client, cert_id="CW"):
    return client.get(f"/api/certificates/{cert_id}/revisions")


def _drop_revisions(cert_id="CW"):
    """直接删库，模拟“功能上线前已存在、尚无版本记录”的存量证书。"""
    with get_conn_ctx() as conn:
        conn.execute(
            "DELETE FROM certificate_revisions WHERE certificate_id = ?",
            (cert_id,),
        )


def _revision_count():
    with get_conn_ctx() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM certificate_revisions"
        ).fetchone()["n"]


# ---------------------------------------------------------------- 版本写入

def test_first_save_creates_v1_snapshot(client):
    _post_instrument(client)
    _post_cert(client)
    r = _revisions(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["certificate_id"] == "CW"
    assert data["revision_count"] == 1
    rev = data["revisions"][0]
    assert rev["version"] == 1
    assert rev["instrument_code"] == "W"
    assert rev["cert_number"] == "W-1"
    assert rev["uncertainty"] == 0.01
    assert rev["unit"] == "mm"
    assert rev["valid_from"] == "2026-01-01T00:00:00Z"
    assert rev["signature_digest"]  # 服务端重算的摘要随快照保存
    assert rev["signed_payload"]
    assert rev["recorded_at"]


def test_identical_resubmit_does_not_add_version(client):
    _post_instrument(client)
    _post_cert(client)
    _post_cert(client)  # 同内容重交
    data = _revisions(client).json()
    assert data["revision_count"] == 1
    assert [r["version"] for r in data["revisions"]] == [1]


def test_business_change_appends_consecutive_versions(client):
    _post_instrument(client)
    _post_cert(client)
    changed = {**CERT_W, "uncertainty": 0.02}
    _post_cert(client, changed)
    _post_cert(client, {**changed, "cert_number": "W-1R"})
    data = _revisions(client).json()
    assert [r["version"] for r in data["revisions"]] == [1, 2, 3]
    v1, v2, v3 = data["revisions"]
    assert v1["uncertainty"] == 0.01 and v1["cert_number"] == "W-1"
    assert v2["uncertainty"] == 0.02 and v2["cert_number"] == "W-1"
    assert v3["uncertainty"] == 0.02 and v3["cert_number"] == "W-1R"


def test_batch_submission_versions_and_noop_resubmit(client):
    payload = {
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [CERT_W],
        "edges": [],
    }
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text
    assert _revisions(client).json()["revision_count"] == 1
    # 同内容批量重交不增版
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text
    assert _revisions(client).json()["revision_count"] == 1
    # 批量中业务字段实际变化才追加 v2
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [{**CERT_W, "unit": "cm"}],
        "edges": [],
    })
    assert r.status_code == 201, r.text
    data = _revisions(client).json()
    assert [rev["version"] for rev in data["revisions"]] == [1, 2]
    assert data["revisions"][1]["unit"] == "cm"


def test_legacy_certificate_backfills_old_value_before_first_update(client):
    _post_instrument(client)
    _post_cert(client)
    _drop_revisions()  # 模拟存量证书
    _post_cert(client, {**CERT_W, "uncertainty": 0.005})
    data = _revisions(client).json()
    assert [r["version"] for r in data["revisions"]] == [1, 2]
    v1, v2 = data["revisions"]
    assert v1["uncertainty"] == 0.01   # 补录的旧值
    assert v2["uncertainty"] == 0.005  # 本次新值


def test_legacy_certificate_identical_resubmit_backfills_only_v1(client):
    _post_instrument(client)
    _post_cert(client)
    _drop_revisions()
    _post_cert(client)  # 同内容重交：补录 v1 后不再追加
    data = _revisions(client).json()
    assert [r["version"] for r in data["revisions"]] == [1]
    assert data["revisions"][0]["uncertainty"] == 0.01


def test_legacy_certificate_without_update_has_empty_revision_list(client):
    _post_instrument(client)
    _post_cert(client)
    _drop_revisions()
    r = _revisions(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision_count"] == 0
    assert data["revisions"] == []


# ---------------------------------------------------------------- 事务与预检

def test_failed_batch_rolls_back_certificate_and_revision(client):
    # 有效证书 + 引用缺失证书的边：整批 422 回滚，证书与版本都不留
    r = client.post("/api/submissions", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [CERT_W],
        "edges": [{"child_cert_id": "CW", "parent_cert_id": "MISSING"}],
    })
    assert r.status_code == 422, r.text
    assert client.get("/api/certificates/CW").status_code == 404
    assert _revisions(client).status_code == 404
    assert _revision_count() == 0


def test_preflight_leaves_no_revision(client):
    r = client.post("/api/submissions/preflight", json={
        "instruments": [{"code": "W", "name": "工作仪器"}],
        "certificates": [CERT_W],
        "edges": [],
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert client.get("/api/certificates/CW").status_code == 404
    assert _revisions(client).status_code == 404
    assert _revision_count() == 0


def test_preflight_on_existing_cert_adds_no_version(client):
    _post_instrument(client)
    _post_cert(client)
    r = client.post("/api/submissions/preflight", json={
        "instruments": [],
        "certificates": [{**CERT_W, "uncertainty": 9.9}],
        "edges": [],
    })
    assert r.status_code == 200, r.text
    data = _revisions(client).json()
    assert [rev["version"] for rev in data["revisions"]] == [1]
    # 证书现值也未被预检改动
    assert client.get("/api/certificates/CW").json()["uncertainty"] == 0.01


# ---------------------------------------------------------------- 差异查询

def test_revision_diff_reports_field_changes(client):
    _post_instrument(client)
    _post_cert(client)
    _post_cert(client, {**CERT_W, "uncertainty": 0.02, "unit": "cm"})
    r = client.get(
        "/api/certificates/CW/revisions/diff",
        params={"from_version": 1, "to_version": 2},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["certificate_id"] == "CW"
    assert data["from_version"] == 1
    assert data["to_version"] == 2
    # 载荷/摘要由业务字段重算，随 uncertainty/unit 一并变化
    assert data["changed_fields"] == [
        "uncertainty", "unit", "signature_digest", "signed_payload"
    ]
    assert data["changes"]["uncertainty"] == {"from": 0.01, "to": 0.02}
    assert data["changes"]["unit"] == {"from": "mm", "to": "cm"}
    assert data["changes"]["signature_digest"]["from"] != \
        data["changes"]["signature_digest"]["to"]


def test_revision_diff_same_version_has_no_changes(client):
    _post_instrument(client)
    _post_cert(client)
    r = client.get(
        "/api/certificates/CW/revisions/diff",
        params={"from_version": 1, "to_version": 1},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["changed_fields"] == []
    assert data["changes"] == {}


def test_revision_diff_missing_version_returns_404(client):
    _post_instrument(client)
    _post_cert(client)
    r = client.get(
        "/api/certificates/CW/revisions/diff",
        params={"from_version": 1, "to_version": 2},
    )
    assert r.status_code == 404, r.text
    assert "版本 2" in r.json()["detail"]
    r = client.get(
        "/api/certificates/CW/revisions/diff",
        params={"from_version": 9, "to_version": 1},
    )
    assert r.status_code == 404, r.text
    assert "版本 9" in r.json()["detail"]


def test_revision_endpoints_unknown_certificate_404(client):
    r = _revisions(client, "GHOST")
    assert r.status_code == 404, r.text
    assert "GHOST" in r.json()["detail"]
    r = client.get(
        "/api/certificates/GHOST/revisions/diff",
        params={"from_version": 1, "to_version": 2},
    )
    assert r.status_code == 404, r.text
    assert "GHOST" in r.json()["detail"]


# ---------------------------------------------------------------- 建库兼容

def test_init_db_creates_revision_table_on_existing_database(
    tmp_path, monkeypatch
):
    """旧库（无版本表）启动建表后兼容升级出 certificate_revisions，旧数据保留。"""
    db_file = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db_file))
    legacy.executescript(
        """
        CREATE TABLE instruments (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            manufacturer TEXT, model TEXT, serial_no TEXT,
            is_national_standard INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE certificates (
            id TEXT PRIMARY KEY, instrument_code TEXT NOT NULL,
            cert_number TEXT NOT NULL, issued_at TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
            uncertainty REAL NOT NULL, unit TEXT NOT NULL DEFAULT '',
            digest_alg TEXT NOT NULL DEFAULT 'sha256',
            signature_digest TEXT NOT NULL, signed_payload TEXT NOT NULL,
            created_at TEXT NOT NULL);
        INSERT INTO instruments VALUES
            ('W', '工作仪器', NULL, NULL, NULL, 0, '2026-01-01T00:00:00Z');
        """
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    database.init_db()

    check = sqlite3.connect(str(db_file))
    tables = {
        r[0]
        for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "certificate_revisions" in tables
    assert check.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 1
    check.close()
