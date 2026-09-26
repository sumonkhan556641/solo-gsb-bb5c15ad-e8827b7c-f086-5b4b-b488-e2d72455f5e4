"""证书停用影响评估接口（dry-run 模拟）测试。"""
from fastapi.testclient import TestClient  # noqa: F401  保证 client fixture 依赖已加载

from tests.test_trace import (
    AT,
    _certs,
    _edges,
    _instruments,
    _submit,
)

ASSESS = "/api/assessments/certificate-deactivation"


def test_deactivating_reference_standard_breaks_unique_chain(client):
    _submit(client)
    r = client.post(ASSESS, json={"at": AT, "certificate_ids": ["CR"]})
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["dry_run"] is True
    assert d["disabled_certificate_ids"] == ["CR"]
    assert d["removed_edge_count"] == 2  # CW->CR 与 CR->CG 均被模拟移除
    assert d["statistics"]["certificates_after"] == 2
    assert d["statistics"]["edges_after"] == 0
    # 参考标准器自身 R 与下游工作仪器 W 均受影响
    assert d["affected_instrument_count"] == 2
    by_inst = {x["instrument"]: x for x in d["results"]}
    assert set(by_inst) == {"R", "W"}

    w = by_inst["W"]
    assert w["impacted"] is True
    assert w["transition"] == "unique_to_broken"
    assert w["before"]["resolution"] == "unique"
    assert w["after"]["resolution"] == "broken"
    assert w["alternative_chain"] is None
    severed_ids = [s["certificate_ids"] for s in w["severed_chains"]]
    assert severed_ids == [["CW", "CR", "CG"]]
    note_codes = {n["code"] for n in w["simulation_notes"]}
    assert "SIMULATED_CHAIN_SEVERED" in note_codes
    assert "SIMULATED_NO_ALTERNATIVE_CHAIN" in note_codes


def test_ambiguous_resolves_to_alternative_chain(client):
    insts = _instruments() + [{"code": "R2", "name": "第二参考标准器"}]
    certs = _certs() + [
        {"id": "CR2", "instrument_code": "R2", "cert_number": "R2-1",
         "issued_at": "2025-07-01T00:00:00",
         "valid_from": "2025-07-01T00:00:00",
         "valid_until": "2027-07-01T00:00:00",
         "uncertainty": 0.002, "unit": "mm"},
    ]
    edges = _edges() + [
        {"child_cert_id": "CW", "parent_cert_id": "CR2"},
        {"child_cert_id": "CR2", "parent_cert_id": "CG"},
    ]
    _submit(client, insts, certs, edges)

    d = client.post(
        ASSESS,
        json={"at": AT, "certificate_ids": ["CR"],
              "instrument_codes": ["W"]},
    ).json()
    assert d["instruments_filter"] == ["W"]
    assert d["evaluated_instrument_count"] == 1

    w = d["results"][0]
    assert w["transition"] == "ambiguous_to_unique"
    assert w["before"]["resolution"] == "ambiguous"
    assert w["after"]["resolution"] == "unique"
    assert w["after"]["valid"] is True
    assert w["alternative_chain"]["certificate_ids"] == ["CW", "CR2", "CG"]
    assert w["alternative_chain"]["reaches_national_standard"] is True
    assert w["severed_chains"][0]["role"] == "candidate"
    codes = {n["code"] for n in w["simulation_notes"]}
    assert {"SIMULATED_CHAIN_SEVERED",
            "SIMULATED_ALTERNATIVE_CHAIN_AVAILABLE"} <= codes


def test_deactivating_start_certificate_removes_start(client):
    _submit(client)
    d = client.post(ASSESS, json={"at": AT, "certificate_ids": ["CW"]}).json()
    w = next(x for x in d["results"] if x["instrument"] == "W")
    assert w["impacted"] is True
    assert w["before"]["starts"] == ["CW"]
    assert w["after"]["starts"] == []
    assert w["after"]["resolution"] == "broken"
    codes = {n["code"] for n in w["simulation_notes"]}
    assert "SIMULATED_START_CERTIFICATE_REMOVED" in codes
    assert any(
        c["code"] == "NO_ACTIVE_CERTIFICATE"
        for c in w["after"]["conflicts"]
    )


def test_instrument_filter_can_select_unaffected_instrument(client):
    _submit(client)
    # 显式筛选不受影响的国家基准仪器 G：停用 CR 与其无关
    d = client.post(
        ASSESS,
        json={"at": AT, "certificate_ids": ["CR"],
              "instrument_codes": ["G"]},
    ).json()
    assert d["affected_instrument_count"] == 0
    g = d["results"][0]
    assert g["impacted"] is False
    assert g["transition"] == "unchanged"
    assert any(
        n["code"] == "SIMULATED_NO_EFFECT" for n in g["simulation_notes"]
    )


def test_unknown_certificates_and_instruments_rejected(client):
    _submit(client)
    r = client.post(ASSESS, json={"at": AT, "certificate_ids": ["NOPE"]})
    assert r.status_code == 422
    assert "NOPE" in r.json()["detail"]

    r = client.post(
        ASSESS,
        json={"at": AT, "certificate_ids": ["CR"],
              "instrument_codes": ["NO-INST"]},
    )
    assert r.status_code == 422
    assert "NO-INST" in r.json()["detail"]

    assert client.post(ASSESS, json={"at": AT, "certificate_ids": []}).status_code == 422
    assert client.post(
        ASSESS, json={"at": AT, "certificate_ids": ["   "]}
    ).status_code == 422


def test_inactive_certificate_at_query_time_has_no_effect(client):
    _submit(client)
    client.post(
        "/api/certificates",
        json={"id": "CW-OLD", "instrument_code": "W", "cert_number": "W-OLD",
              "issued_at": "2020-01-01T00:00:00",
              "valid_from": "2020-01-01T00:00:00",
              "valid_until": "2021-01-01T00:00:00",
              "uncertainty": 0.02, "unit": "mm"},
    )
    d = client.post(
        ASSESS, json={"at": AT, "certificate_ids": ["CW-OLD"]}
    ).json()
    # 归属仪器 W 在该时点的链不经过该历史证书 -> 无实际影响
    assert d["affected_instrument_count"] == 0
    assert all(x["impacted"] is False for x in d["results"])


def test_deactivating_national_standard_certificate(client):
    _submit(client)
    d = client.post(ASSESS, json={"at": AT, "certificate_ids": ["CG"]}).json()
    assert d["affected_owner_instruments"] == ["G"]
    by_inst = {x["instrument"]: x for x in d["results"]}
    assert by_inst["W"]["after"]["resolution"] == "broken"
    # 国家基准仪器自身失去全部起点
    assert by_inst["G"]["after"]["starts"] == []


def test_simulation_is_dry_run_and_deduplicates_input(client):
    _submit(client)
    d = client.post(
        ASSESS, json={"at": AT, "certificate_ids": ["CR", "CR", " CR "]}
    ).json()
    assert d["disabled_certificate_ids"] == ["CR"]

    # 模拟后数据库状态完全不变
    report = client.get(f"/api/trace/W?at={AT}").json()
    assert report["resolution"] == "unique"
    assert client.get("/api/certificates/CR").status_code == 200
    edges = client.get("/api/edges").json()
    assert len(edges) == 2
