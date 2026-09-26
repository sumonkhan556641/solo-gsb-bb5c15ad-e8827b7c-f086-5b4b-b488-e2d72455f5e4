"""溯源路径枚举的深链回归测试。

修复点：路径枚举原为递归 DFS，单条有效链超过 Python 默认递归深度
（``sys.getrecursionlimit()``，默认 1000）时抛出 ``RecursionError``，
共用报告接口返回 500。枚举已改为显式栈遍历，本组验证：

* 超过默认递归深度的单条深链正常成链（unique/valid），共用报告接口不再 500；
* 深链上的既有校验规则（不确定度收缩）仍给出判定与冲突；
* 深链末端的死路 / 失效父证书 / 成环终止记录与断裂判定保持不变；
* 深主干末端的分叉达到 500 条上限时仍判 undetermined，
  不会把样本误判为唯一或断裂；恰好枚举完则正常判定。
"""
import sys

from fastapi.testclient import TestClient

from app.engine import MAX_PATHS, Catalog, _build_report

AT = "2026-06-15T00:00:00"
AT_Z = AT + "Z"

# 中间标准器级数：整条链节点数 = N + 2，超过 Python 默认递归深度
N = sys.getrecursionlimit() + 500

FROM = "2024-06-01T00:00:00"
UNTIL = "2029-01-01T00:00:00"
ISSUED = "2025-01-01T00:00:00"


def _inst(code, root=False):
    return {"code": code, "name": code, "is_national_standard": root}


def _cert(cid, inst, u, root=False, issued=ISSUED, valid_from=FROM, valid_until=UNTIL):
    return {
        "id": cid, "instrument_code": inst, "cert_number": cid,
        "issued_at": issued,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "uncertainty": 0.0001 if root else u,
        "unit": "mm",
    }


def _chain_payload(n, *, root_id="CG", leaf_id="CW",
                   leaf_u=0.01, root_u=0.0001,
                   root_valid=(FROM, UNTIL),
                   leaf_uncertainty_note=None,
                   uncertainty_at=None):
    """构造 W 经 n 级中间标准器 C1..Cn 到国家基准 G 的单条长链。

    不确定度默认沿溯源方向严格逐级放大（子 > 父，单位一致），
    故链上不产生 UNCERTAINTY_* 冲突；uncertainty_at 可覆盖指定中间
    证书的不确定度（置为比父级还小 -> UNCERTAINTY_REGRESSION）。
    root_valid 可把根证书的有效区间改到查询时点之外。
    """
    instruments = [_inst("G", root=True), _inst("W")]
    instruments += [_inst(f"I{i}") for i in range(1, n + 1)]
    rvf, rvu = root_valid
    certs = [
        _cert(root_id, "G", root_u, root=True, valid_from=rvf, valid_until=rvu),
        _cert(leaf_id, "W", leaf_u),
    ]

    def u_at(i):
        # 从叶子 0.01 线性降到接近根 0.0001，严格递减（child 恒大于 parent）
        return leaf_u - i * (leaf_u - root_u) / (n + 1)

    for i in range(1, n + 1):
        u = u_at(i)
        if uncertainty_at is not None and i == uncertainty_at:
            # 比下一级（父）还小 -> 沿链收缩
            u = u_at(i + 1) / 2
        certs.append(_cert(f"C{i}", f"I{i}", u))
    edges = [{"child_cert_id": leaf_id, "parent_cert_id": "C1"}]
    edges += [
        {"child_cert_id": f"C{i}", "parent_cert_id": f"C{i + 1}"}
        for i in range(1, n)
    ]
    edges.append({"child_cert_id": f"C{n}", "parent_cert_id": root_id})
    return {"instruments": instruments, "certificates": certs, "edges": edges}


def _submit_chain(client, **kwargs):
    payload = _chain_payload(N, **kwargs)
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text[:500]


def _report(client, instrument="W", at=AT):
    return client.get(f"/api/reports/{instrument}?at={at}").json()


# ------------------------------------------------------------ 深链不再崩溃

def test_deep_valid_chain_enumerated_without_recursion_error(client: TestClient):
    _submit_chain(client)
    rep = _report(client)
    assert rep["resolution"] == "unique"
    assert rep["valid"] is True
    assert rep["error_count"] == 0
    chain = rep["active_chain"]
    assert chain["reaches_national_standard"] is True
    # 节点数 = 叶子 + N 级中间证书 + 根
    assert len(chain["nodes"]) == N + 2
    assert chain["nodes"][0]["certificate_id"] == "CW"
    assert chain["nodes"][-1]["certificate_id"] == "CG"
    assert chain["nodes"][-1]["is_national_standard"] is True
    assert rep["statistics"]["active_chain_depth"] == N + 2
    assert rep["statistics"]["paths_enumerated"] == 1
    assert rep["statistics"]["paths_truncated"] is False


def test_deep_chain_common_traceability_does_not_500(client: TestClient):
    # 共用报告接口（共同溯源逐台复用单仪器报告）在深链上曾因
    # RecursionError 返回 500；两条各自超过递归深度的长链汇于同一基准。
    _submit_chain(client)

    # 第二条独立长链：仪器 W2，叶子 D W、中间证书 D1..Dn，汇于同一基准 CG
    instruments = [_inst("W2")] + [_inst(f"J{i}") for i in range(1, N + 1)]
    certs = [_cert("DW", "W2", 0.01)]
    root_u, leaf_u = 0.0001, 0.01
    for i in range(1, N + 1):
        certs.append(
            _cert(f"D{i}", f"J{i}", leaf_u - i * (leaf_u - root_u) / (N + 1))
        )
    edges = [{"child_cert_id": "DW", "parent_cert_id": "D1"}]
    edges += [
        {"child_cert_id": f"D{i}", "parent_cert_id": f"D{i + 1}"}
        for i in range(1, N)
    ]
    edges.append({"child_cert_id": f"D{N}", "parent_cert_id": "CG"})
    r = client.post(
        "/api/submissions",
        json={"instruments": instruments, "certificates": certs, "edges": edges},
    )
    assert r.status_code == 201, r.text[:500]

    r = client.post(
        "/api/assessments/common-traceability",
        json={"at": AT_Z, "instrument_codes": ["W", "W2"]},
    )
    assert r.status_code == 200, r.text[:500]
    d = r.json()
    assert d["participant_count"] == 2
    assert d["common_certificate_found"] is True
    assert d["common_certificate_id"] == "CG"
    rows = {x["instrument"]: x for x in d["instrument_chains"]}
    assert rows["W"]["distance"] == N + 1
    assert rows["W2"]["distance"] == N + 1


# ------------------------------------------------------- 深链上既有校验规则

def test_deep_chain_with_uncertainty_regression_reports_conflict(
    client: TestClient,
):
    # 深链中段某级不确定度比父级还小 -> 既有 UNCERTAINTY_REGRESSION
    # 规则照常给出判定：链到基准但校验失败（broken）。
    _submit_chain(client, uncertainty_at=N // 2)
    rep = _report(client)
    assert rep["resolution"] == "broken"
    assert rep["valid"] is False
    assert rep["active_chain"] is None
    assert rep["statistics"]["root_reaching_chains"] == 1
    assert rep["statistics"]["valid_root_chains"] == 0
    codes = {c["code"] for c in rep["conflicts"]}
    assert "UNCERTAINTY_REGRESSION" in codes
    assert "ALL_CHAINS_FAILED_CHECKS" in codes
    assert len(rep["candidates"]) == 1
    assert len(rep["candidates"][0]["nodes"]) == N + 2


# --------------------------------------------------- 深链末端的各类终止记录

def test_deep_chain_dead_end_termination(client: TestClient):
    # 最末级中间证书不挂父边（去掉到根的边）-> 深链末端死路
    payload = _chain_payload(N)
    payload["edges"] = [
        e for e in payload["edges"]
        if not (e["child_cert_id"] == f"C{N}" and e["parent_cert_id"] == "CG")
    ]
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text[:500]

    rep = _report(client)
    assert rep["resolution"] == "broken"
    assert rep["statistics"]["paths_enumerated"] == 1
    assert rep["statistics"]["paths_truncated"] is False
    dead = [c for c in rep["conflicts"] if c["code"] == "DEAD_END"]
    assert len(dead) == 1
    assert dead[0]["node"] == f"C{N}"
    assert len(rep["broken_chains"]) == 1
    chain_nodes = [n["certificate_id"] for n in rep["broken_chains"][0]["nodes"]]
    assert chain_nodes == ["CW"] + [f"C{i}" for i in range(1, N + 1)]


def test_deep_chain_inactive_parent_termination(client: TestClient):
    # 根证书在查询时点失效 -> 深链末端以 PARENT_CERT_NOT_ACTIVE 终止
    _submit_chain(
        client,
        root_valid=("2020-01-01T00:00:00", "2021-01-01T00:00:00"),
    )
    rep = _report(client)
    assert rep["resolution"] == "broken"
    assert rep["statistics"]["paths_enumerated"] == 1
    codes = {c["code"] for c in rep["conflicts"]}
    assert "PARENT_CERT_NOT_ACTIVE" in codes
    not_active = next(
        c for c in rep["conflicts"] if c["code"] == "PARENT_CERT_NOT_ACTIVE"
    )
    assert not_active["node"] == "CG"
    assert len(rep["broken_chains"]) == 1
    chain_nodes = [n["certificate_id"] for n in rep["broken_chains"][0]["nodes"]]
    assert chain_nodes[-1] == "CG"
    assert chain_nodes[0] == "CW"


def test_deep_chain_cycle_termination(client: TestClient):
    # 在深链末端附近追加一条回指早先证书的有效边 -> 成环终止记录，
    # 同时经原边到根的链仍成立（环作为第二条候选终止）。
    payload = _chain_payload(N)
    payload["edges"].append(
        {"child_cert_id": f"C{N}", "parent_cert_id": f"C{N - 5}"}
    )
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text[:500]

    rep = _report(client)
    # 一条到基准的合格链：唯一有效主链仍成立
    assert rep["resolution"] == "unique"
    chain = rep["active_chain"]
    assert chain is not None
    assert chain["reaches_national_standard"] is True
    assert len(chain["nodes"]) == N + 2
    assert [n["certificate_id"] for n in chain["nodes"]] == (
        ["CW"] + [f"C{i}" for i in range(1, N + 1)] + ["CG"]
    )
    # 成环分支按现有规则完整记录（CYCLE_DETECTED；该回指边的父子
    # 不确定度倒挂还会在环分支上追加 UNCERTAINTY_REGRESSION），故整份
    # 报告 valid=False，但唯一有效主链的节点序列不受影响。
    cycles = [c for c in rep["conflicts"] if c["code"] == "CYCLE_DETECTED"]
    assert len(cycles) == 1
    assert cycles[0]["node"] == f"C{N - 5}"
    cycle_chains = [c for c in rep["broken_chains"] if c["cycle"]]
    assert len(cycle_chains) == 1
    cycle_nodes = [n["certificate_id"] for n in cycle_chains[0]["nodes"]]
    assert cycle_nodes[-1] == f"C{N - 5}"
    assert cycle_nodes[-2] == f"C{N}"


# ---------------------------------------- 深主干末端分叉的 500 条截断语义
#
# 这两个用例直接在引擎层用内存 Catalog 构造数据：截断时报告要保留 500 条
# 每条近千节点的样本链，经 HTTP 序列化响应体积过大（与枚举实现无关，
# 浅链截断语义已由 tests/test_undetermined.py 在接口层覆盖）。这里用
# Row 兼容的轻量字典直接驱动 _build_report，专注验证“深主干 + 末端分叉”
# 不会递归崩溃，且截断/穷尽判定与浅链完全一致。

class _Row(dict):
    """同时支持 ``row["k"]`` 与 ``row.k`` 访问的 sqlite3.Row 兼容字典。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


# 深主干深度仍超过 Python 默认递归深度（节点数 = TRUNK_N + 2）
TRUNK_N = sys.getrecursionlimit() + 40


def _inst_row(code, root=False):
    return _Row(code=code, name=code, is_national_standard=1 if root else 0)


def _cert_row(cid, inst, u, root=False, valid_from=FROM, valid_until=UNTIL):
    payload = (
        '{"cert_number":"%s","id":"%s","instrument_code":"%s","issued_at":"%s",'
        '"uncertainty":%s,"unit":"mm","valid_from":"%s","valid_until":"%s"}'
        % (cid, cid, inst, ISSUED, u, valid_from, valid_until)
    )
    from app.utils import compute_digest
    return _Row(
        id=cid, instrument_code=inst, cert_number=cid,
        issued_at=ISSUED, valid_from=valid_from, valid_until=valid_until,
        uncertainty=0.0001 if root else u, unit="mm",
        digest_alg="sha256", signed_payload=payload,
        signature_digest=compute_digest(payload),
    )


def _deep_fan_catalog(fan, *, with_root_edge):
    """CW -> C1..Cn 深主干 -> 枢纽 Cn；枢纽挂 fan 条死路父边（CB0..），
    with_root_edge 时另挂一条到国家基准 CG 的有效边。

    父证书 id 字典序 CB* < CG，死路分支先于到根分支被枚举。
    """
    n = TRUNK_N
    instruments = {"G": _inst_row("G", True), "W": _inst_row("W")}
    certs = {"CG": _cert_row("CG", "G", 0.0001, root=True),
             "CW": _cert_row("CW", "W", 0.01)}
    edges = []
    eid = 0

    def add_edge(child, parent):
        nonlocal eid
        child_c = certs[child]
        edges.append(_Row(
            id=eid, child_cert_id=child, parent_cert_id=parent,
            valid_from=child_c["valid_from"], valid_until=child_c["valid_until"],
            note="",
        ))
        eid += 1

    for i in range(1, n + 1):
        instruments[f"I{i}"] = _inst_row(f"I{i}")
        certs[f"C{i}"] = _cert_row(
            f"C{i}", f"I{i}", 0.01 - i * (0.01 - 0.0001) / (n + 1)
        )
    add_edge("CW", "C1")
    for i in range(1, n):
        add_edge(f"C{i}", f"C{i + 1}")
    for i in range(fan):
        instruments[f"B{i}"] = _inst_row(f"B{i}")
        certs[f"CB{i}"] = _cert_row(f"CB{i}", f"B{i}", 0.0009)
        add_edge(f"C{n}", f"CB{i}")
    if with_root_edge:
        add_edge(f"C{n}", "CG")
    return Catalog(instruments=instruments, certs=certs, edges=edges)


def test_deep_trunk_fan_beyond_cap_is_undetermined_not_unique():
    # 深主干已超过递归深度；末端 506 条死路先填满 500 上限，合格链 CG
    # 完全未被枚举 -> undetermined，既不崩溃也不误判为唯一或断裂。
    cat = _deep_fan_catalog(MAX_PATHS + 6, with_root_edge=True)
    rep = _build_report(cat, "W", AT_Z)
    assert rep["resolution"] == "undetermined"
    assert rep["valid"] is False
    assert rep["active_chain"] is None
    assert rep["statistics"]["paths_truncated"] is True
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    assert len(rep["broken_chains"]) == MAX_PATHS
    codes = {c["code"] for c in rep["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "TOO_MANY_CANDIDATES" in codes
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    # 每条样本都是“深主干 + 一个死路父证书”的确定性序列
    trunk = ["CW"] + [f"C{i}" for i in range(1, TRUNK_N + 1)]
    for chain in rep["broken_chains"]:
        nodes = [n["certificate_id"] for n in chain["nodes"]]
        assert nodes[: TRUNK_N + 1] == trunk
        assert nodes[-1].startswith("CB")


def test_deep_trunk_fan_exactly_cap_exhausted_is_broken():
    # 恰好 500 条死路、无到根边：枚举在第 500 条处穷尽（上限外无未展开
    # 分支），paths_truncated=false，正常判断裂。
    cat = _deep_fan_catalog(MAX_PATHS, with_root_edge=False)
    rep = _build_report(cat, "W", AT_Z)
    assert rep["resolution"] == "broken"
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    assert rep["statistics"]["paths_truncated"] is False
    codes = {c["code"] for c in rep["conflicts"]}
    assert "CHAIN_BROKEN" in codes
    assert "RESULT_UNDETERMINED" not in codes
    assert "TOO_MANY_CANDIDATES" not in codes
