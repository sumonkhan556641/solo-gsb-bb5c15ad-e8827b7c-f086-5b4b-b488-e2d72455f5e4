"""候选链枚举超过 500 条上限时的“结果未定”语义测试。

规则（修复要点）：

* 枚举达到 MAX_PATHS 且上限之外仍有未展开分支时，已枚举路径只是样本；
* 已见合格链不足 2 条 -> ``resolution=undetermined``（结果未定），
  保留样本链与截断说明，但 ``active_chain=None``，不得判为唯一或断裂；
* 已见 >=2 条合格链 -> 仍可判 ``ambiguous``（多候选）；
* 恰好枚举完（无未展开分支）-> 维持 broken/unique/ambiguous 正常定论；
* 共同溯源、测量相容性与预约链路门禁（空档/确认/巡检/补位）一律以专用
  原因码 ``RESULT_UNDETERMINED`` 拒绝未定结果。
"""
from fastapi.testclient import TestClient

AT = "2026-06-15T00:00:00"
AT_Z = AT + "Z"


def _inst(code, root=False):
    return {"code": code, "name": code, "is_national_standard": root}


def _cert(cid, inst, root=False, u=0.002):
    return {
        "id": cid, "instrument_code": inst, "cert_number": cid,
        "issued_at": "2024-01-01T00:00:00",
        "valid_from": "2024-01-01T00:00:00",
        "valid_until": "2030-01-01T00:00:00",
        "uncertainty": 0.0001 if root else u,
        "unit": "mm",
    }


def _dead_end_fan(client, n, *, extra_instruments=None, extra_certs=None,
                  extra_edges=None, start="CW"):
    """构造：起点证书 start 挂 n 条死路父边（B0..Bn-1）。

    死路父证书 id 取 CB{i}（"CB" 字典序在 "CG"/"CAA" 之后），配合
    extra_edges 可控制合格分支是否先于死路被 DFS 枚举。
    """
    instruments = list(extra_instruments or [])
    certs = list(extra_certs or [])
    edges = list(extra_edges or [])
    instruments += [_inst(f"B{i}") for i in range(n)]
    certs += [_cert(f"CB{i}", f"B{i}") for i in range(n)]
    edges += [
        {"child_cert_id": start, "parent_cert_id": f"CB{i}"} for i in range(n)
    ]
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments, "certificates": certs, "edges": edges},
    )
    assert r.status_code == 201, r.text


def _report(client, instrument="W", at=AT):
    return client.get(f"/api/reports/{instrument}?at={at}").json()


# ---------------------------------------------------------------- 结果未定

def test_truncated_without_valid_chain_is_undetermined_not_broken(client: TestClient):
    # 506 条死路 + 1 条合格直连基准链；DFS 按父证书 id 排序，
    # CB0..CB499 先填满 500 上限，合格链 CG 完全未被枚举。
    _dead_end_fan(
        client, 506,
        extra_instruments=[_inst("G", root=True), _inst("W")],
        extra_certs=[_cert("CG", "G", root=True), _cert("CW", "W", u=0.01)],
        extra_edges=[{"child_cert_id": "CW", "parent_cert_id": "CG"}],
    )
    rep = _report(client)
    assert rep["resolution"] == "undetermined"
    assert rep["valid"] is False
    assert rep["active_chain"] is None  # 未定报告不提供有效主链
    assert rep["statistics"]["paths_truncated"] is True
    assert rep["statistics"]["paths_enumerated"] == 500
    assert rep["statistics"]["max_paths"] == 500

    codes = {c["code"] for c in rep["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "TOO_MANY_CANDIDATES" in codes
    # 不得把样本误报为断裂
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    # 500 条死路样本链保留
    assert len(rep["broken_chains"]) == 500
    assert rep["candidates"] == []

    und = next(c for c in rep["conflicts"] if c["code"] == "RESULT_UNDETERMINED")
    assert und["severity"] == "ERROR"
    assert und["detail"]["branches_remaining_beyond_limit"] is True
    assert und["detail"]["valid_root_chains_in_sample"] == 0


def test_truncated_with_one_valid_chain_is_undetermined_not_unique(client: TestClient):
    # 1 条合格链（CAA->CG，id 字典序先于 CB）先枚举，随后 503 条死路
    # 推过上限：已见 1 条合格链仍不足 2 条 -> 未定，不得判唯一。
    _dead_end_fan(
        client, 503,
        extra_instruments=[
            _inst("G", root=True), _inst("AA"), _inst("W"),
        ],
        extra_certs=[
            _cert("CG", "G", root=True),
            _cert("CAA", "AA", u=0.001),
            _cert("CW", "W", u=0.01),
        ],
        extra_edges=[
            {"child_cert_id": "CW", "parent_cert_id": "CAA"},
            {"child_cert_id": "CAA", "parent_cert_id": "CG"},
        ],
    )
    rep = _report(client)
    assert rep["resolution"] == "undetermined"
    assert rep["active_chain"] is None
    # 已见的那条合格链作为样本保留在 candidates 中（但不作为主链）
    assert len(rep["candidates"]) == 1
    assert [n["certificate_id"] for n in rep["candidates"][0]["nodes"]] == [
        "CW", "CAA", "CG"
    ]
    assert len(rep["broken_chains"]) == 499
    assert rep["statistics"]["valid_root_chains"] == 1
    codes = {c["code"] for c in rep["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "CANDIDATE_CHAIN_FAILED_CHECKS" not in codes
    assert "ALTERNATIVE_BRANCH_PRESENT" not in codes


def test_truncated_with_two_valid_chains_still_ambiguous(client: TestClient):
    # 2 条合格链（CAA1/CAA2 -> CG，先枚举）+ 504 条死路推过上限：
    # 已见 2 条合格链，多候选结论不受截断影响。
    _dead_end_fan(
        client, 504,
        extra_instruments=[
            _inst("G", root=True), _inst("AA1"), _inst("AA2"), _inst("W"),
        ],
        extra_certs=[
            _cert("CG", "G", root=True),
            _cert("CAA1", "AA1", u=0.001),
            _cert("CAA2", "AA2", u=0.001),
            _cert("CW", "W", u=0.01),
        ],
        extra_edges=[
            {"child_cert_id": "CW", "parent_cert_id": "CAA1"},
            {"child_cert_id": "CW", "parent_cert_id": "CAA2"},
            {"child_cert_id": "CAA1", "parent_cert_id": "CG"},
            {"child_cert_id": "CAA2", "parent_cert_id": "CG"},
        ],
    )
    rep = _report(client)
    assert rep["resolution"] == "ambiguous"
    assert rep["valid"] is False
    assert len(rep["candidates"]) == 2
    assert rep["statistics"]["paths_truncated"] is True
    codes = {c["code"] for c in rep["conflicts"]}
    assert "MULTIPLE_VALID_CHAINS" in codes
    assert "RESULT_UNDETERMINED" not in codes


def test_exactly_max_paths_exhausted_is_normal_broken(client: TestClient):
    # 恰好 500 条死路、枚举完后没有未展开分支 -> 正常判断裂
    _dead_end_fan(
        client, 500,
        extra_instruments=[_inst("W")],
        extra_certs=[_cert("CW", "W", u=0.01)],
    )
    rep = _report(client)
    assert rep["resolution"] == "broken"
    assert rep["statistics"]["paths_truncated"] is False
    codes = {c["code"] for c in rep["conflicts"]}
    assert "CHAIN_BROKEN" in codes
    assert "RESULT_UNDETERMINED" not in codes
    assert "TOO_MANY_CANDIDATES" not in codes


# ------------------------------------------------- 门禁：空档/确认/巡检/补位

def test_slot_gate_rejects_undetermined(client: TestClient):
    _dead_end_fan(
        client, 506,
        extra_instruments=[_inst("G", root=True), _inst("W")],
        extra_certs=[_cert("CG", "G", root=True), _cert("CW", "W", u=0.01)],
        extra_edges=[{"child_cert_id": "CW", "parent_cert_id": "CG"}],
    )
    r = client.get(
        "/api/calibration-slots",
        params=[
            ("instrument", "W"),
            ("start", AT_Z),
            ("end", "2026-06-16T00:00:00Z"),
            ("duration_seconds", "3600"),
        ],
    )
    d = r.json()
    assert d["traceability_available"] is False
    assert d["reason_code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert d["chain_reason_code"] == "RESULT_UNDETERMINED"
    assert d["slot"] is None
    assert d["trace_report"]["resolution"] == "undetermined"


def test_confirm_booking_rejects_undetermined_409(client: TestClient):
    _dead_end_fan(
        client, 506,
        extra_instruments=[_inst("G", root=True), _inst("W")],
        extra_certs=[_cert("CG", "G", root=True), _cert("CW", "W", u=0.01)],
        extra_edges=[{"child_cert_id": "CW", "parent_cert_id": "CG"}],
    )
    r = client.post(
        "/api/calibration-bookings",
        json={
            "instrument_code": "W",
            "start": AT_Z,
            "duration_seconds": 1800,
        },
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert detail["detail"]["reason_code"] == "RESULT_UNDETERMINED"
    # 拒绝后不得写入任何预约
    assert client.get("/api/calibration-bookings?instrument=W").json() == []


# ------------------------------------------------------- 门禁：共同溯源/相容性

def test_common_traceability_rejects_undetermined(client: TestClient):
    _dead_end_fan(
        client, 506,
        extra_instruments=[_inst("G", root=True), _inst("W")],
        extra_certs=[_cert("CG", "G", root=True), _cert("CW", "W", u=0.01)],
        extra_edges=[{"child_cert_id": "CW", "parent_cert_id": "CG"}],
    )
    d = client.post(
        "/api/assessments/common-traceability",
        json={"at": AT_Z, "instrument_codes": ["W", "G"]},
    ).json()
    row = next(x for x in d["nonparticipants"] if x["instrument"] == "W")
    assert row["reason_code"] == "RESULT_UNDETERMINED"
    assert row["resolution"] == "undetermined"
    assert "W" not in d["participants"]


def test_measurement_compatibility_rejects_undetermined(client: TestClient):
    _dead_end_fan(
        client, 506,
        extra_instruments=[_inst("G", root=True), _inst("W")],
        extra_certs=[_cert("CG", "G", root=True), _cert("CW", "W", u=0.01)],
        extra_edges=[{"child_cert_id": "CW", "parent_cert_id": "CG"}],
    )
    d = client.post(
        "/api/assessments/measurement-compatibility",
        json={
            "at": AT_Z,
            "target_unit": "mm",
            "readings": [
                {"instrument_code": "W", "value": 1.0},
                {"instrument_code": "G", "value": 1.0},
            ],
        },
    ).json()
    row = next(x for x in d["nonparticipants"] if x["instrument"] == "W")
    assert row["reason_code"] == "RESULT_UNDETERMINED"
    assert row["resolution"] == "undetermined"
    reading = next(x for x in d["readings"] if x["instrument"] == "W")
    assert reading["participated"] is False


def _book_unique_then_make_undetermined(client, instrument, start, cert_id):
    """先在唯一链下确认预约，再把该仪器链路改造成截断未定结构。"""
    bk = client.post(
        "/api/calibration-bookings",
        json={"instrument_code": instrument, "start": start, "duration_seconds": 3600},
    )
    assert bk.status_code == 201, bk.text
    # 506 条死路父边：CB* 字典序先于 CG，填满上限后合格链不再被枚举
    fan_insts = [_inst(f"B{i}") for i in range(506)]
    fan_certs = [_cert(f"CB{i}", f"B{i}") for i in range(506)]
    fan_edges = [
        {"child_cert_id": cert_id, "parent_cert_id": f"CB{i}"} for i in range(506)
    ]
    r = client.post(
        "/api/submissions",
        json={"instruments": fan_insts, "certificates": fan_certs, "edges": fan_edges},
    )
    assert r.status_code == 201, r.text
    return bk.json()


def test_inspection_marks_undetermined_invalid(client: TestClient):
    base_insts = [_inst("G", root=True), _inst("W")]
    base_certs = [
        _cert("CG", "G", root=True),
        _cert("CW", "W", u=0.01),
    ]
    base_edges = [{"child_cert_id": "CW", "parent_cert_id": "CG"}]
    r = client.post(
        "/api/submissions",
        json={"instruments": base_insts, "certificates": base_certs,
              "edges": base_edges},
    )
    assert r.status_code == 201, r.text
    bk = _book_unique_then_make_undetermined(
        client, "W", "2026-06-21T09:00:00Z", "CW"
    )
    d = client.get(
        "/api/calibration-bookings/inspection",
        params=[
            ("start", "2026-06-21T00:00:00Z"),
            ("end", "2026-06-22T00:00:00Z"),
        ],
    ).json()
    item = next(x for x in d["items"] if x["booking_id"] == bk["id"])
    assert item["status"] == "invalid"
    assert item["reason_code"] == "RESULT_UNDETERMINED"
    assert item["current_certificate_id"] is None


def test_waitlist_promotion_skips_undetermined(client: TestClient):
    # W（唯一）持有预约，W2（唯一）登记候补；随后把 W2 变成未定，
    # 取消 W 的预约时补位必须以 RESULT_UNDETERMINED 跳过 W2。
    base_insts = [_inst("G", root=True), _inst("W"), _inst("W2")]
    base_certs = [
        _cert("CG", "G", root=True),
        _cert("CW", "W", u=0.01),
        _cert("CW2", "W2", u=0.01),
    ]
    base_edges = [
        {"child_cert_id": "CW", "parent_cert_id": "CG"},
        {"child_cert_id": "CW2", "parent_cert_id": "CG"},
    ]
    r = client.post(
        "/api/submissions",
        json={"instruments": base_insts, "certificates": base_certs,
              "edges": base_edges},
    )
    assert r.status_code == 201, r.text
    bk = client.post(
        "/api/calibration-bookings",
        json={"instrument_code": "W", "start": "2026-06-20T09:00:00Z",
              "duration_seconds": 3600},
    ).json()
    wl = client.post(
        f"/api/calibration-bookings/{bk['id']}/waitlist",
        json={"instrument_code": "W2"},
    ).json()

    # CW2 挂 506 条死路（CB* 先于 CG 枚举）-> 未定
    fan_insts = [_inst(f"B{i}") for i in range(506)]
    fan_certs = [_cert(f"CB{i}", f"B{i}") for i in range(506)]
    fan_edges = [
        {"child_cert_id": "CW2", "parent_cert_id": f"CB{i}"} for i in range(506)
    ]
    r = client.post(
        "/api/submissions",
        json={"instruments": fan_insts, "certificates": fan_certs, "edges": fan_edges},
    )
    assert r.status_code == 201, r.text

    d = client.delete(f"/api/calibration-bookings/{bk['id']}").json()
    assert d["promoted"] is False
    assert len(d["skipped"]) == 1
    assert d["skipped"][0]["waitlist_id"] == wl["id"]
    assert d["skipped"][0]["reason_code"] == "RESULT_UNDETERMINED"
