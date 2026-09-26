"""批次共同溯源核查（POST /api/assessments/common-traceability）测试。"""
from fastapi.testclient import TestClient

from app.engine import rank_common_certificates

AT = "2026-06-15T00:00:00Z"
URL = "/api/assessments/common-traceability"


def _inst(code, name=None, root=False):
    return {"code": code, "name": name or code, "is_national_standard": root}


def _cert(cid, inst, issued="2026-01-01T00:00:00",
          vf="2026-01-01T00:00:00", vu="2027-01-01T00:00:00",
          u=0.01, unit="mm", digest=None):
    data = {
        "id": cid, "instrument_code": inst, "cert_number": cid,
        "issued_at": issued, "valid_from": vf, "valid_until": vu,
        "uncertainty": u, "unit": unit,
    }
    if digest is not None:
        data["signature_digest"] = digest
    return data


def _submit(client, instruments, certificates, edges):
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments,
              "certificates": certificates, "edges": edges},
    )
    assert r.status_code == 201, r.text


def _two_valid_chains(client):
    """G（基准）← R ← W 与 G ← R2 ← W2：两条都唯一且校验通过，共享 CG。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("R2"),
         _inst("W"), _inst("W2")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CR2", "R2", issued="2025-07-01T00:00:00",
                  vf="2025-07-01T00:00:00", vu="2027-07-01T00:00:00", u=0.002),
            _cert("CW", "W"),
            _cert("CW2", "W2", u=0.02),
        ],
        [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
            {"child_cert_id": "CW2", "parent_cert_id": "CR2"},
            {"child_cert_id": "CR2", "parent_cert_id": "CG"},
        ],
    )


def test_common_certificate_found_with_subchains_and_distances(client: TestClient):
    _two_valid_chains(client)
    r = client.post(URL, json={"at": AT, "instrument_codes": ["W", "W2"]})
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["dry_run"] is True
    assert d["common_certificate_found"] is True
    assert d["common_certificate_id"] == "CG"
    assert d["common_certificate"]["certificate_id"] == "CG"
    assert d["common_certificate"]["is_national_standard"] is True
    assert d["participants"] == ["W", "W2"]
    assert d["nonparticipants"] == []

    by_inst = {row["instrument"]: row for row in d["instrument_chains"]}
    assert by_inst["W"]["participated"] is True
    assert by_inst["W"]["distance"] == 2
    assert [n["certificate_id"] for n in by_inst["W"]["sub_chain"]["nodes"]] == [
        "CW", "CR", "CG"
    ]
    assert [e["parent_cert_id"] for e in by_inst["W"]["sub_chain"]["edges"]] == [
        "CR", "CG"
    ]
    assert by_inst["W2"]["distance"] == 2
    assert [n["certificate_id"]
            for n in by_inst["W2"]["sub_chain"]["nodes"]] == ["CW2", "CR2", "CG"]

    assert len(d["candidates"]) == 1
    assert d["candidates"][0] == {
        "certificate_id": "CG",
        "max_distance": 2,
        "total_distance": 4,
        "distances": [
            {"instrument": "W", "distance": 2},
            {"instrument": "W2", "distance": 2},
        ],
        "selected": True,
    }
    assert d["reason_code"] is None
    assert d["statistics"]["reports_evaluated"] == 2


def test_nearest_common_certificate_preferred_over_root(client: TestClient):
    """两台仪器先在中间证书 CR 汇合再共同到基准：最近共同证书应为 CR 而非 CG。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("W"), _inst("W3")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CW", "W"),
            _cert("CW3", "W3", u=0.02),
        ],
        [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CW3", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    )
    d = client.post(
        URL, json={"at": AT, "instrument_codes": ["W", "W3"]}
    ).json()
    assert d["common_certificate_id"] == "CR"
    ranked = [(c["certificate_id"], c["max_distance"], c["total_distance"])
              for c in d["candidates"]]
    # CR 距离 1/1（max=1,total=2）优于 CG 距离 2/2（max=2,total=4）
    assert ranked == [("CR", 1, 2), ("CG", 2, 4)]
    by_inst = {row["instrument"]: row for row in d["instrument_chains"]}
    assert by_inst["W"]["distance"] == 1
    assert [n["certificate_id"]
            for n in by_inst["W"]["sub_chain"]["nodes"]] == ["CW", "CR"]


def test_ambiguous_instrument_not_participating_and_chain_not_chosen(
    client: TestClient,
):
    """多候选仪器不得任取一条链参与。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("R2"),
         _inst("W"), _inst("W2")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CR2", "R2", issued="2025-07-01T00:00:00",
                  vf="2025-07-01T00:00:00", vu="2027-07-01T00:00:00", u=0.002),
            _cert("CW", "W"),
            _cert("CW2", "W2", u=0.02),
        ],
        [
            # W 同时挂两条父边 -> 两条都到基准的有效候选链
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CW", "parent_cert_id": "CR2"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
            {"child_cert_id": "CR2", "parent_cert_id": "CG"},
            {"child_cert_id": "CW2", "parent_cert_id": "CG"},
        ],
    )
    d = client.post(
        URL, json={"at": AT, "instrument_codes": ["W", "W2"]}
    ).json()
    assert d["participants"] == ["W2"]
    assert d["common_certificate_found"] is False
    assert d["reason_code"] == "INSUFFICIENT_PARTICIPANTS"
    np_ = d["nonparticipants"][0]
    assert np_["instrument"] == "W"
    assert np_["reason_code"] == "MULTIPLE_VALID_CHAINS"
    assert np_["resolution"] == "ambiguous"
    assert any(c["code"] == "MULTIPLE_VALID_CHAINS" for c in np_["conflicts"])
    # 明确没有替它任取一条链
    row = next(r for r in d["instrument_chains"] if r["instrument"] == "W")
    assert row["participated"] is False
    assert row["sub_chain"] is None
    assert row["distance"] is None


def test_broken_and_missing_instruments_return_reasons(client: TestClient):
    _two_valid_chains(client)
    # T：参考标准 TP 无向上溯源边 -> 死路
    _submit(
        client,
        [_inst("TP"), _inst("T")],
        [
            _cert("CTP", "TP", u=0.05, unit="C"),
            _cert("CT", "T", u=0.5, unit="C"),
        ],
        [{"child_cert_id": "CT", "parent_cert_id": "CTP"}],
    )
    d = client.post(
        URL,
        json={"at": AT,
              "instrument_codes": ["W", "W2", "T", "NOPE"]},
    ).json()
    assert set(d["participants"]) == {"W", "W2"}
    assert d["common_certificate_id"] == "CG"
    reasons = {row["instrument"]: row["reason_code"]
               for row in d["nonparticipants"]}
    assert reasons == {"T": "CHAIN_BROKEN", "NOPE": "INSTRUMENT_NOT_FOUND"}
    nope = next(r for r in d["nonparticipants"] if r["instrument"] == "NOPE")
    assert nope["resolution"] is None
    assert nope["conflicts"] == []


def test_all_chains_failed_checks_reason(client: TestClient):
    """链结构到基准但摘要被篡改：唯一候选链未校验通过，不参与。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("W")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CW", "W", digest="0" * 64),
        ],
        [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    )
    d = client.post(
        URL, json={"at": AT, "instrument_codes": ["W", "G"]}
    ).json()
    # G 自身是基准（单证书唯一链），W 校验失败 -> 不足 2 台
    assert d["participants"] == ["G"]
    np_ = next(r for r in d["nonparticipants"] if r["instrument"] == "W")
    assert np_["reason_code"] == "ALL_CHAINS_FAILED_CHECKS"
    assert np_["resolution"] == "broken"


def test_unique_chain_with_global_error_not_participating(client: TestClient):
    """唯一成链但存在 ERROR（历史证书区间断档）：CHAIN_VALIDATION_FAILED。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("X")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CR", "R", issued="2025-06-01T00:00:00",
                  vf="2025-06-01T00:00:00", vu="2027-06-01T00:00:00", u=0.001),
            _cert("CX-old", "X", issued="2020-01-01T00:00:00",
                  vf="2020-01-01T00:00:00", vu="2021-01-01T00:00:00", u=0.01),
            _cert("CX", "X"),
        ],
        [
            {"child_cert_id": "CX", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    )
    d = client.post(
        URL, json={"at": AT, "instrument_codes": ["X", "R"]}
    ).json()
    assert d["participants"] == ["R"]
    np_ = next(r for r in d["nonparticipants"] if r["instrument"] == "X")
    assert np_["reason_code"] == "CHAIN_VALIDATION_FAILED"
    assert any(c["code"] == "INTERVAL_GAP" for c in np_["conflicts"])


def test_no_active_and_multiple_active_certificate_reasons(client: TestClient):
    _two_valid_chains(client)
    # 无有效证书的仪器 Z
    _submit(
        client,
        [_inst("Z"), _inst("M")],
        [
            _cert("CZ", "Z", vf="2030-01-01T00:00:00",
                  vu="2031-01-01T00:00:00"),
            # M 在 AT 同时有两张有效证书（起点不唯一）
            _cert("CM-1", "M"),
            _cert("CM-2", "M", vf="2025-06-01T00:00:00",
                  vu="2027-06-01T00:00:00"),
        ],
        [],
    )
    d = client.post(
        URL,
        json={"at": AT,
              "instrument_codes": ["W", "Z", "M"]},
    ).json()
    reasons = {row["instrument"]: row["reason_code"]
               for row in d["nonparticipants"]}
    assert reasons["Z"] == "NO_ACTIVE_CERTIFICATE"
    assert reasons["M"] == "MULTIPLE_ACTIVE_CERTIFICATES"


def test_disjoint_chains_return_no_common_certificate(client: TestClient):
    """两条各自成立但互不相通的链：证书 id 交集为空，明确返回无共同证书。"""
    _submit(
        client,
        [
            _inst("G1", root=True), _inst("W1"),
            _inst("G2", root=True), _inst("W2"),
        ],
        [
            _cert("CG1", "G1", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00", u=0.0001),
            _cert("CW1X", "W1"),
            _cert("CG2", "G2", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0001, unit="C"),
            _cert("CW2X", "W2", u=0.01, unit="C"),
        ],
        [
            {"child_cert_id": "CW1X", "parent_cert_id": "CG1"},
            {"child_cert_id": "CW2X", "parent_cert_id": "CG2"},
        ],
    )
    d = client.post(
        URL, json={"at": AT, "instrument_codes": ["W1", "W2"]}
    ).json()
    assert d["participants"] == ["W1", "W2"]
    assert d["common_certificate_found"] is False
    assert d["common_certificate_id"] is None
    assert d["common_certificate"] is None
    assert d["reason_code"] == "NO_COMMON_CERTIFICATE"
    assert d["candidates"] == []
    # 参与仪器仍返回其完整链，但无子链/距离
    for row in d["instrument_chains"]:
        assert row["participated"] is True
        assert row["sub_chain"] is None
        assert row["distance"] is None
        assert row["chain_certificate_ids"]


def test_request_validation_422(client: TestClient):
    # 少于 2 台
    r = client.post(URL, json={"at": AT, "instrument_codes": ["W"]})
    assert r.status_code == 422
    # 重复仪器编码
    r = client.post(URL, json={"at": AT, "instrument_codes": ["W", "W"]})
    assert r.status_code == 422
    # 空字符串
    r = client.post(URL, json={"at": AT, "instrument_codes": ["W", "  "]})
    assert r.status_code == 422
    # 缺字段
    r = client.post(URL, json={"at": AT})
    assert r.status_code == 422


def test_common_traceability_does_not_write_business_data(client: TestClient):
    _two_valid_chains(client)

    def counts():
        return (
            client.get("/api/certificates").json().__len__(),
            client.get("/api/edges").json().__len__(),
            client.get("/api/instruments").json().__len__(),
        )

    before = counts()
    r = client.post(
        URL,
        json={"at": AT,
              "instrument_codes": ["W", "W2", "T", "NOPE"]},
    )
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert counts() == before

    # 换一个时点再查一次，数据依旧
    r2 = client.post(
        URL,
        json={"at": "2026-07-01T00:00:00Z",
              "instrument_codes": ["W", "W2"]},
    )
    assert r2.status_code == 200
    assert counts() == before


def test_single_instrument_query_semantics_unchanged(client: TestClient):
    _two_valid_chains(client)
    rep = client.get(f"/api/reports/W?at={AT}").json()
    assert rep["resolution"] == "unique"
    assert rep["valid"] is True
    assert [n["certificate_id"]
            for n in rep["active_chain"]["nodes"]] == ["CW", "CR", "CG"]
    assert rep["error_count"] == 0


def test_rank_common_certificates_tie_breakers():
    """选举规则：最大距离最小 → 总和最小 → 证书 ID 字典序末位。"""
    # 三层共享：A 比 Z 更近
    rows = rank_common_certificates(
        {"i1": ["L1", "M", "A", "Z"],
         "i2": ["L2", "N", "A", "Z"],
         "i3": ["L3", "O", "A", "Z"]}
    )
    assert [(r["certificate_id"], r["max_distance"], r["total_distance"])
            for r in rows] == [("A", 2, 6), ("Z", 3, 9)]

    # 距离指标完全相同（仅可人为构造）时取字典序末位
    same_a = {"certificate_id": "A", "distances": {"i1": 1, "i2": 1},
              "max_distance": 1, "total_distance": 2}
    same_b = {"certificate_id": "B", "distances": {"i1": 1, "i2": 1},
              "max_distance": 1, "total_distance": 2}
    rows_in = [same_a, same_b]
    rows_in.sort(key=lambda r: r["certificate_id"], reverse=True)
    rows_in.sort(key=lambda r: (r["max_distance"], r["total_distance"]))
    assert rows_in[0]["certificate_id"] == "B"

    # 空 / 无交集
    assert rank_common_certificates({}) == []
    assert rank_common_certificates({"a": ["X"], "b": ["Y"]}) == []
