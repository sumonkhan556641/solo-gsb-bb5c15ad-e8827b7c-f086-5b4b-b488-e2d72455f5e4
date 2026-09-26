"""测量相容性评估（POST /api/assessments/measurement-compatibility）测试。"""
import math

import pytest
from fastapi.testclient import TestClient

from app.engine import (
    EN_RULE_RATIO,
    EN_RULE_ZERO_UNCERTAINTY,
    CompatibilityNumericalError,
    _en_score,
    maximal_compatible_groups,
)

AT = "2026-06-15T00:00:00Z"
URL = "/api/assessments/measurement-compatibility"


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


def _mm_lab(client, codes=("W1", "W2", "W3", "W4"), u=1.0):
    """国家基准 G（mm）+ 若干工作仪器各自唯一有效链到 G（mm，U=u）。"""
    _submit(
        client,
        [_inst("G", root=True)] + [_inst(c) for c in codes],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0001, unit="mm"),
            *[_cert(f"C{c}", c, u=u, unit="mm") for c in codes],
        ],
        [{"child_cert_id": f"C{c}", "parent_cert_id": "CG"} for c in codes],
    )


def _post(client, readings, target_unit="mm", at=AT):
    return client.post(
        URL,
        json={"at": at, "target_unit": target_unit, "readings": readings},
    )


def _reading(code, value):
    return {"instrument_code": code, "value": value}


def test_en_pairs_and_maximal_groups_not_connected_components(client: TestClient):
    """A-B-C 两两相容成三角、C-D 相容、D 与 A/B 不相容：

    极大两两相容分组为 {W1,W2,W3} 与 {W3,W4}（可重叠）；
    连通分量会错误地并成 {W1,W2,W3,W4}。
    """
    _mm_lab(client)
    r = _post(client, [
        _reading("W1", 0.0),
        _reading("W2", 0.5),
        _reading("W3", 1.0),
        _reading("W4", 2.0),
    ])
    assert r.status_code == 200, r.text
    d = r.json()

    assert d["dry_run"] is True
    assert d["target_unit"] == "mm"
    assert d["participants"] == ["W1", "W2", "W3", "W4"]
    assert d["nonparticipants"] == []
    assert d["reason_code"] is None

    # 4 台全参与 -> 6 对，按仪器编码排序
    assert [(p["instrument_a"], p["instrument_b"]) for p in d["pairs"]] == [
        ("W1", "W2"), ("W1", "W3"), ("W1", "W4"),
        ("W2", "W3"), ("W2", "W4"), ("W3", "W4"),
    ]
    pairs = {(p["instrument_a"], p["instrument_b"]): p for p in d["pairs"]}
    denom = math.sqrt(1.0 ** 2 + 1.0 ** 2)

    p = pairs[("W1", "W2")]
    assert p["difference"] == pytest.approx(0.5)
    assert p["en"] == pytest.approx(0.5 / denom)
    assert p["compatible"] is True
    assert p["rule"] == "en_le_1"
    assert p["uncertainty_a"] == 1.0 and p["uncertainty_b"] == 1.0

    assert pairs[("W1", "W3")]["en"] == pytest.approx(1.0 / denom)
    assert pairs[("W1", "W3")]["compatible"] is True
    assert pairs[("W3", "W4")]["en"] == pytest.approx(1.0 / denom)
    assert pairs[("W3", "W4")]["compatible"] is True

    # En > 1 -> 不相容
    assert pairs[("W1", "W4")]["en"] == pytest.approx(2.0 / denom)
    assert pairs[("W1", "W4")]["compatible"] is False
    assert pairs[("W2", "W4")]["compatible"] is False

    # 极大团而非连通分量：{W1,W2,W3} 与 {W3,W4}，可重叠、按编码稳定排序
    assert d["groups"] == [
        {"members": ["W1", "W2", "W3"], "size": 3},
        {"members": ["W3", "W4"], "size": 2},
    ]
    assert d["statistics"]["pair_count"] == 6
    assert d["statistics"]["compatible_pair_count"] == 4
    assert d["statistics"]["group_count"] == 2


def test_en_exactly_one_is_compatible(client: TestClient):
    """En 恰为 1（U=0.6/0.8 -> 分母 1.0，差值 1.0）判为相容。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("A"), _inst("B")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0001, unit="mm"),
            _cert("CA", "A", u=0.6, unit="mm"),
            _cert("CB", "B", u=0.8, unit="mm"),
        ],
        [
            {"child_cert_id": "CA", "parent_cert_id": "CG"},
            {"child_cert_id": "CB", "parent_cert_id": "CG"},
        ],
    )
    d = _post(client, [_reading("A", 0.0), _reading("B", 1.0)]).json()
    assert d["pairs"][0]["en"] == pytest.approx(1.0)
    assert d["pairs"][0]["compatible"] is True
    assert d["groups"] == [{"members": ["A", "B"], "size": 2}]


def test_uncertainty_taken_from_start_certificate(client: TestClient):
    """U 必须取仪器起点证书的不确定度，而非链上其他证书。"""
    _submit(
        client,
        [_inst("G", root=True), _inst("R"), _inst("W")],
        [
            _cert("CG", "G", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0001, unit="mm"),
            _cert("CR", "R", u=0.05, unit="mm"),
            _cert("CW", "W", u=0.5, unit="mm"),
        ],
        [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    )
    _mm_lab(client, codes=("W2",), u=0.5)
    d = _post(client, [_reading("W", 0.0), _reading("W2", 0.7)]).json()
    row = next(r for r in d["readings"] if r["instrument"] == "W")
    assert row["participated"] is True
    assert row["start_certificate_id"] == "CW"
    assert row["uncertainty"] == 0.5
    # En = 0.7 / sqrt(0.5^2 + 0.5^2) ≈ 0.9899 -> 相容
    assert d["pairs"][0]["en"] == pytest.approx(0.7 / math.sqrt(0.5))
    assert d["pairs"][0]["compatible"] is True


def test_zero_uncertainty_only_equal_values_compatible(client: TestClient):
    """两 U 均为 0：En 无定义（null），仅读数相等才相容。"""
    _submit(
        client,
        [_inst("GZ", root=True), _inst("ZA"), _inst("ZB"), _inst("ZC")],
        [
            _cert("CGZ", "GZ", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0, unit="mm"),
            _cert("CZA", "ZA", u=0.0, unit="mm"),
            _cert("CZB", "ZB", u=0.0, unit="mm"),
            _cert("CZC", "ZC", u=0.0, unit="mm"),
        ],
        [
            {"child_cert_id": "CZA", "parent_cert_id": "CGZ"},
            {"child_cert_id": "CZB", "parent_cert_id": "CGZ"},
            {"child_cert_id": "CZC", "parent_cert_id": "CGZ"},
        ],
    )
    d = _post(client, [
        _reading("ZA", 5.0),
        _reading("ZB", 5.0),
        _reading("ZC", 6.0),
    ]).json()
    pairs = {(p["instrument_a"], p["instrument_b"]): p for p in d["pairs"]}
    assert pairs[("ZA", "ZB")]["en"] is None
    assert pairs[("ZA", "ZB")]["compatible"] is True
    assert pairs[("ZA", "ZB")]["rule"] == "zero_uncertainty_equality"
    assert pairs[("ZA", "ZC")]["en"] is None
    assert pairs[("ZA", "ZC")]["compatible"] is False
    assert pairs[("ZB", "ZC")]["compatible"] is False
    assert d["groups"] == [{"members": ["ZA", "ZB"], "size": 2}]


def test_one_zero_uncertainty_uses_normal_formula(client: TestClient):
    """仅一方 U 为 0 时分母非零，仍按常式 En 计算。"""
    _submit(
        client,
        [_inst("GZ", root=True), _inst("ZA"), _inst("ZD")],
        [
            _cert("CGZ", "GZ", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0, unit="mm"),
            _cert("CZA", "ZA", u=0.0, unit="mm"),
            _cert("CZD", "ZD", u=0.5, unit="mm"),
        ],
        [
            {"child_cert_id": "CZA", "parent_cert_id": "CGZ"},
            {"child_cert_id": "CZD", "parent_cert_id": "CGZ"},
        ],
    )
    d = _post(client, [_reading("ZA", 0.0), _reading("ZD", 0.4)]).json()
    p = d["pairs"][0]
    assert p["en"] == pytest.approx(0.4 / 0.5)
    assert p["compatible"] is True
    assert p["rule"] == "en_le_1"


def test_unit_mismatch_reading_excluded_with_reason(client: TestClient):
    """起点证书单位与目标单位不符：逐条返回原因，不参与计算。"""
    _mm_lab(client, codes=("W1", "W2"))
    _submit(
        client,
        [_inst("GT", root=True), _inst("T1")],
        [
            _cert("CGT", "GT", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.01, unit="C"),
            _cert("CT1", "T1", u=0.5, unit="C"),
        ],
        [{"child_cert_id": "CT1", "parent_cert_id": "CGT"}],
    )
    d = _post(client, [
        _reading("W1", 0.0),
        _reading("T1", 20.0),
        _reading("W2", 0.5),
    ]).json()
    assert d["participants"] == ["W1", "W2"]
    assert d["nonparticipant_count"] == 1
    np_ = d["nonparticipants"][0]
    assert np_["instrument"] == "T1"
    assert np_["reason_code"] == "CERTIFICATE_UNIT_MISMATCH"
    assert np_["certificate_unit"] == "C"
    assert np_["start_certificate_id"] == "CT1"
    assert np_["resolution"] == "unique"  # 链本身唯一且校验通过
    assert np_["valid"] is True
    # 逐条读数结果中同样给出原因
    row = next(r for r in d["readings"] if r["instrument"] == "T1")
    assert row["participated"] is False
    assert row["nonparticipation_reason_code"] == "CERTIFICATE_UNIT_MISMATCH"
    assert row["uncertainty"] is None
    # 不参与计算：仅 W1-W2 一对
    assert [(p["instrument_a"], p["instrument_b"]) for p in d["pairs"]] == [
        ("W1", "W2")
    ]


def test_chain_unavailable_readings_excluded_with_reasons(client: TestClient):
    """链不可用（多候选/断裂/不存在）逐条返回原因，不参与计算。"""
    _mm_lab(client, codes=("W1", "W2"))
    _submit(
        client,
        [_inst("GP", root=True), _inst("R1"), _inst("R2"), _inst("P"),
         _inst("TP"), _inst("T")],
        [
            _cert("CGP", "GP", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.001, unit="mm"),
            _cert("CR1", "R1", u=0.01, unit="mm"),
            _cert("CR2", "R2", u=0.02, unit="mm"),
            _cert("CP", "P", u=0.1, unit="mm"),
            _cert("CTP", "TP", u=0.05, unit="mm"),
            _cert("CT", "T", u=0.5, unit="mm"),
        ],
        [
            # P 有两条均有效的候选链 -> 多候选，绝不任取一条
            {"child_cert_id": "CP", "parent_cert_id": "CR1"},
            {"child_cert_id": "CP", "parent_cert_id": "CR2"},
            {"child_cert_id": "CR1", "parent_cert_id": "CGP"},
            {"child_cert_id": "CR2", "parent_cert_id": "CGP"},
            # T 的参考标准 TP 无向上溯源边 -> 死路
            {"child_cert_id": "CT", "parent_cert_id": "CTP"},
        ],
    )
    d = _post(client, [
        _reading("W1", 0.0),
        _reading("P", 1.0),
        _reading("T", 2.0),
        _reading("NOPE", 3.0),
        _reading("W2", 0.5),
    ]).json()
    assert d["participants"] == ["W1", "W2"]
    reasons = {r["instrument"]: r["reason_code"] for r in d["nonparticipants"]}
    assert reasons == {
        "P": "MULTIPLE_VALID_CHAINS",
        "T": "CHAIN_BROKEN",
        "NOPE": "INSTRUMENT_NOT_FOUND",
    }
    nope = next(r for r in d["nonparticipants"] if r["instrument"] == "NOPE")
    assert nope["resolution"] is None
    assert nope["conflicts"] == []
    # 断裂仪器附完整冲突明细
    t = next(r for r in d["nonparticipants"] if r["instrument"] == "T")
    assert any(c["code"] == "DEAD_END" for c in t["conflicts"])
    assert len(d["pairs"]) == 1
    assert d["groups"] == [{"members": ["W1", "W2"], "size": 2}]


def test_insufficient_participants(client: TestClient):
    """可参与读数不足 2 条：无配对、无分组，给出明确原因码。"""
    _mm_lab(client, codes=("W1",))
    d = _post(client, [_reading("W1", 0.0), _reading("NOPE", 1.0)]).json()
    assert d["participants"] == ["W1"]
    assert d["pairs"] == []
    assert d["groups"] == []
    assert d["reason_code"] == "INSUFFICIENT_PARTICIPANTS"
    assert d["reason"]


def test_isolated_participant_in_no_group(client: TestClient):
    """与谁都不相容的参与者不出现在任何分组（人数 ≥2 才成组）。"""
    _mm_lab(client, codes=("W1", "W2", "W3"))
    d = _post(client, [
        _reading("W1", 0.0),
        _reading("W2", 0.5),
        _reading("W3", 100.0),
    ]).json()
    assert d["participants"] == ["W1", "W2", "W3"]
    assert d["groups"] == [{"members": ["W1", "W2"], "size": 2}]
    assert d["statistics"]["pair_count"] == 3
    assert d["statistics"]["compatible_pair_count"] == 1


def test_groups_sorted_by_instrument_code_regardless_of_input_order(
    client: TestClient,
):
    """分组与配对按仪器编码稳定排序，与请求顺序无关。"""
    _mm_lab(client)
    d = _post(client, [
        _reading("W4", 2.0),
        _reading("W3", 1.0),
        _reading("W2", 0.5),
        _reading("W1", 0.0),
    ]).json()
    # participants 保持请求顺序；pairs/groups 按编码稳定排序
    assert d["participants"] == ["W4", "W3", "W2", "W1"]
    assert d["pairs"][0]["instrument_a"] == "W1"
    assert d["groups"] == [
        {"members": ["W1", "W2", "W3"], "size": 3},
        {"members": ["W3", "W4"], "size": 2},
    ]


def test_request_validation_422(client: TestClient):
    _mm_lab(client, codes=("W1", "W2"))
    # 少于 2 条
    assert _post(client, [_reading("W1", 0.0)]).status_code == 422
    # 多于 12 条
    many = [_reading(f"X{i}", 0.0) for i in range(13)]
    assert _post(client, many).status_code == 422
    # 重复仪器编码
    assert _post(
        client, [_reading("W1", 0.0), _reading("W1", 1.0)]
    ).status_code == 422
    # 空白编码
    assert _post(
        client, [_reading("W1", 0.0), _reading("  ", 1.0)]
    ).status_code == 422
    # 非有限测量值（手工构造 JSON 文本：服务端 json 解析接受 NaN/Infinity
    # 字面量，再由 FiniteFloat 校验拒绝）
    for bad in ("NaN", "Infinity", "-Infinity"):
        body = (
            '{"at": "%s", "target_unit": "mm", "readings": ['
            '{"instrument_code": "W1", "value": 0.0}, '
            '{"instrument_code": "W2", "value": %s}]}' % (AT, bad)
        )
        r = client.post(
            URL, content=body, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 422, bad
    # 缺 target_unit
    r = client.post(URL, json={"at": AT, "readings": [
        _reading("W1", 0.0), _reading("W2", 1.0)]})
    assert r.status_code == 422
    # 缺 readings
    r = client.post(URL, json={"at": AT, "target_unit": "mm"})
    assert r.status_code == 422


def test_maximum_12_readings_accepted(client: TestClient):
    codes = [f"W{i}" for i in range(12)]
    _mm_lab(client, codes=codes)
    d = _post(client, [_reading(c, 0.0) for c in codes]).json()
    assert d["participant_count"] == 12
    assert d["statistics"]["pair_count"] == 66
    # 全部相等 -> 一个大极大团
    assert d["groups"] == [{"members": sorted(codes), "size": 12}]


def test_measurement_compatibility_does_not_write_business_data(
    client: TestClient,
):
    _mm_lab(client)

    def counts():
        return (
            client.get("/api/certificates").json().__len__(),
            client.get("/api/edges").json().__len__(),
            client.get("/api/instruments").json().__len__(),
        )

    before = counts()
    r = _post(client, [_reading("W1", 0.0), _reading("W2", 0.5)])
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert counts() == before


def test_en_score_stable_against_square_overflow():
    """大不确定度平方溢出为 inf 的场景：旧式分母 inf -> En=0 把差异巨大
    的一对误判相容；稳定缩放后 En≈0.707 仍相容但有限，差异更大时必须
    判不相容（绝不能让溢出翻转判定）。"""
    huge = 1.5e308
    s = _en_score(huge, 0.0, huge, huge)
    assert math.isfinite(s["en"])
    assert s["en"] == pytest.approx(1.0 / math.sqrt(2.0))
    assert s["compatible"] is True
    assert s["rule"] == EN_RULE_RATIO

    # 两个 U 都大到平方溢出但合并范数仍有限：差值超过真实合并 U
    # （En>1）而差值本身有限——旧式分母 inf、en=0 误判相容
    s = _en_score(1e308, -5e307, 1e308, 1e308)
    assert math.isfinite(s["en"])
    assert s["en"] == pytest.approx(1.5 / math.sqrt(2.0))
    assert s["en"] > 1.0
    assert s["compatible"] is False

    # 仅一方 U 极大：旧式 en=0 误判，新式差值/U 正确判不相容
    s = _en_score(huge, -1e307, huge, 0.0)
    assert math.isfinite(s["en"])
    assert s["en"] == pytest.approx(1.6 / 1.5)
    assert s["compatible"] is False


def test_en_score_stable_against_subnormal_underflow():
    """非零次正规 U 平方下溢为 0：旧式误入双零规则（按读数相等判定），
    稳定缩放后 hypot 仍识别非零分母，按真实 En 判定。"""
    tiny = 5e-324  # 最小正次正规值，其平方下溢为 0
    assert tiny * tiny == 0.0

    # 相等读数：旧式双零规则相容；新式 En=0 同样相容（结论一致、走常式）
    s = _en_score(1.0, 1.0, tiny, tiny)
    assert s["en"] == pytest.approx(0.0)
    assert s["compatible"] is True
    assert s["rule"] == EN_RULE_RATIO

    # 相差一个次正规单位：旧式误判不相容（双零 + 不相等），
    # 真实 En=1/sqrt2 <=1，应判相容
    s = _en_score(tiny, 0.0, tiny, tiny)
    assert math.isfinite(s["en"])
    assert s["en"] == pytest.approx(1.0 / math.sqrt(2.0))
    assert s["compatible"] is True
    assert s["rule"] == EN_RULE_RATIO

    # 一方为 0、另一方为次正规：旧式分母 0 误走相等规则
    s = _en_score(tiny, 0.0, 0.0, tiny)
    assert s["en"] == pytest.approx(1.0)
    assert s["compatible"] is True

    # 两个 U 确实都为 0 时仍走读数相等规则
    s = _en_score(3.0, 3.0, 0.0, 0.0)
    assert s["en"] is None
    assert s["compatible"] is True
    assert s["rule"] == EN_RULE_ZERO_UNCERTAINTY
    s = _en_score(3.0, 4.0, 0.0, 0.0)
    assert s["en"] is None
    assert s["compatible"] is False
    assert s["rule"] == EN_RULE_ZERO_UNCERTAINTY


def test_en_score_raises_on_non_representable_results():
    """读数差溢出或 En 超过最大有限浮点：抛数值错误（路由层映射 422），
    不返回 inf。"""
    with pytest.raises(CompatibilityNumericalError) as ei:
        _en_score(1.7e308, -1.7e308, 1.0, 1.0)
    assert ei.value.reason_code == "READING_DIFFERENCE_NOT_FINITE"

    # 差值有限但相对 U 过大：En 超过最大有限浮点
    with pytest.raises(CompatibilityNumericalError) as ei:
        _en_score(1e308, 0.0, 0.5, 0.5)
    assert ei.value.reason_code == "EN_SCORE_NOT_FINITE"

    # 恰可表示的极大 En（En<=1）不报错
    s = _en_score(1e308, 0.0, 1e308, 1e308)
    assert math.isfinite(s["en"]) and s["compatible"] is True


def test_huge_uncertainties_overflow_no_false_compatibility_group(
    client: TestClient,
):
    """端到端：U≈1.5e308 平方溢出的旧 bug 会把不相容对并入极大分组；
    修复后 pairs 与 groups 共用稳定判定，不相容仪器不得成组。"""
    _submit(
        client,
        [_inst("GH", root=True), _inst("H1"), _inst("H2"), _inst("H3")],
        [
            _cert("CGH", "GH", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0, unit="mm"),
            _cert("CH1", "H1", u=1e308, unit="mm"),
            _cert("CH2", "H2", u=1e308, unit="mm"),
            _cert("CH3", "H3", u=1e308, unit="mm"),
        ],
        [
            {"child_cert_id": "CH1", "parent_cert_id": "CGH"},
            {"child_cert_id": "CH2", "parent_cert_id": "CGH"},
            {"child_cert_id": "CH3", "parent_cert_id": "CGH"},
        ],
    )
    r = _post(client, [
        _reading("H1", 1e308),      # H1/H2 差 1.5e308，En≈1.06>1 -> 不相容
        _reading("H2", -5e307),     # （差值本身有限，但旧平方求和溢出）
        _reading("H3", 0.0),        # H1-H3、H3-H2 均 En<=1 相容
    ])
    assert r.status_code == 200, r.text
    d = r.json()
    verdict = {
        (p["instrument_a"], p["instrument_b"]): p for p in d["pairs"]
    }
    assert math.isfinite(verdict[("H1", "H2")]["en"])
    assert verdict[("H1", "H2")]["compatible"] is False
    assert verdict[("H1", "H3")]["compatible"] is True
    assert verdict[("H2", "H3")]["compatible"] is True
    # 旧 bug 下三者会被错误并成 {H1,H2,H3}；修复后仅两个二人团
    assert d["groups"] == [
        {"members": ["H1", "H3"], "size": 2},
        {"members": ["H2", "H3"], "size": 2},
    ]


def test_subnormal_uncertainties_end_to_end(client: TestClient):
    """端到端：非零次正规 U 不再误入双零规则污染极大相容分组。"""
    tiny = 5e-324
    _submit(
        client,
        [_inst("GT", root=True), _inst("T1"), _inst("T2")],
        [
            _cert("CGT", "GT", issued="2024-01-01T00:00:00",
                  vf="2024-01-01T00:00:00", vu="2030-01-01T00:00:00",
                  u=0.0, unit="mm"),
            _cert("CT1", "T1", u=tiny, unit="mm"),
            _cert("CT2", "T2", u=tiny, unit="mm"),
        ],
        [
            {"child_cert_id": "CT1", "parent_cert_id": "CGT"},
            {"child_cert_id": "CT2", "parent_cert_id": "CGT"},
        ],
    )
    # 两读数相差一个次正规单位：真实 En=1/sqrt2<=1，应相容
    r = _post(client, [_reading("T1", tiny), _reading("T2", 0.0)])
    assert r.status_code == 200, r.text
    d = r.json()
    p = d["pairs"][0]
    assert p["rule"] == "en_le_1"
    assert p["en"] == pytest.approx(1.0 / math.sqrt(2.0))
    assert p["compatible"] is True
    assert d["groups"] == [{"members": ["T1", "T2"], "size": 2}]


def test_difference_overflow_returns_422(client: TestClient):
    """读数差绝对值溢出为无穷：422 并说明原因，响应不含 Infinity。"""
    _mm_lab(client, codes=("W1", "W2"), u=1.0)
    r = _post(client, [_reading("W1", 1.7e308), _reading("W2", -1.7e308)])
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["code"] == "READING_DIFFERENCE_NOT_FINITE"
    assert body["message"]
    assert body["detail"]["instrument_a"] == "W1"
    assert body["detail"]["instrument_b"] == "W2"
    assert "Infinity" not in r.text and "NaN" not in r.text


def test_en_overflow_returns_422(client: TestClient):
    """读数差有限但 En 超出有限浮点范围：422 说明原因，响应不含 Infinity。"""
    _mm_lab(client, codes=("W1", "W2"), u=0.5)
    r = _post(client, [_reading("W1", 1e308), _reading("W2", 0.0)])
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["code"] == "EN_SCORE_NOT_FINITE"
    assert body["detail"]["instrument_a"] == "W1"
    assert "Infinity" not in r.text and "NaN" not in r.text


def test_maximal_compatible_groups_unit():
    """极大团枚举：重叠团、孤立点、非连通分量。"""
    # 三角 ABC + 边 CD + 孤立 E
    groups = maximal_compatible_groups(
        ["A", "B", "C", "D", "E"],
        [("A", "B"), ("A", "C"), ("B", "C"), ("C", "D")],
    )
    assert groups == [["A", "B", "C"], ["C", "D"]]
    # 链式相容 A-B、B-C 但 A-C 不相容：两个二人团，而非一个连通分量
    groups = maximal_compatible_groups(["A", "B", "C"], [("A", "B"), ("B", "C")])
    assert groups == [["A", "B"], ["B", "C"]]
    # 完全图 -> 单一大团
    groups = maximal_compatible_groups(
        ["X", "Y", "Z"], [("X", "Y"), ("X", "Z"), ("Y", "Z")]
    )
    assert groups == [["X", "Y", "Z"]]
    # 无相容对 -> 无分组
    assert maximal_compatible_groups(["X", "Y"], []) == []
