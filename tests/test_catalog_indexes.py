"""Catalog 快照索引重构的回归测试。

验证点：

* 装载（数据库连接或内存构造）与证书停用派生视图都会在构造时一次性
  建立三类索引：按仪器分组的证书索引、按边 ID 定位的索引、父子边索引；
* 链路枚举 / 历史区间断档 / 链级校验复用这些索引：全表扫描次数随
  **快照数量**增长，而不随候选链数量或相关仪器数量增长；
* 索引只属于当前快照：派生快照基于自身数据重建索引，不污染原快照；
  后续请求重新装载，始终读取最新数据；
* 多分支长链 + 多仪器数据下，路径顺序、500 条截断未定、冲突明细与
  全库统计与既有结果完全一致。
"""
from fastapi.testclient import TestClient  # noqa: F401  保证 client fixture 依赖已加载

from app.database import get_conn_ctx
from app.engine import (
    MAX_PATHS,
    Catalog,
    _build_report,
    _catalog_without_certificates,
)
from app.utils import compute_digest
from tests.test_trace import AT, _submit

AT_Z = AT + "Z"
FROM = "2024-01-01T00:00:00Z"
UNTIL = "2030-01-01T00:00:00Z"
ISSUED = "2024-06-01T00:00:00Z"


class _Row(dict):
    """同时支持 ``row["k"]`` 与 ``row.k`` 访问的 sqlite3.Row 兼容字典。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _ScanCountingList(list):
    """统计被完整迭代次数的 list（每次全表扫描 +1）。"""

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class _ScanCountingDict(dict):
    """统计 values() 全量扫描次数的 dict。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.value_scans = 0

    def values(self):
        self.value_scans += 1
        return super().values()


def _inst_row(code, root=False):
    return _Row(code=code, name=code, is_national_standard=1 if root else 0)


def _cert_row(cid, inst, uncertainty, *, valid_from=FROM, valid_until=UNTIL,
              issued=ISSUED, bad_digest=False):
    payload = (
        '{"cert_number":"%s","id":"%s","instrument_code":"%s","issued_at":"%s",'
        '"uncertainty":%s,"unit":"mm","valid_from":"%s","valid_until":"%s"}'
        % (cid, cid, inst, issued, uncertainty, valid_from, valid_until)
    )
    return _Row(
        id=cid, instrument_code=inst, cert_number=cid,
        issued_at=issued, valid_from=valid_from, valid_until=valid_until,
        uncertainty=uncertainty, unit="mm",
        digest_alg="sha256", signed_payload=payload,
        signature_digest=("0" * 64) if bad_digest else compute_digest(payload),
    )


def _multi_branch_dataset():
    """多分支长链 + 多仪器数据集（内存行，不经过数据库）。

    W 的溯源链：``CW -> C1..C10`` 长主干 -> 枢纽 ``CH1/CH2/CH3`` ->
    每枢纽 ``CPj1/CPj2`` 两条父边 -> 国家基准 ``CG``，共 3×2=6 条
    到达基准的长候选链（不确定度沿链严格放大，全部通过校验）。
    另有四台仪器覆盖多仪器场景：``X1`` 唯一链、``GAP`` 历史区间真实
    断档、``NEST`` 长证书覆盖下的嵌套短证书（不得报假断档）、
    ``BAD`` 签名摘要被篡改。
    """
    instruments: dict[str, _Row] = {}
    certs: dict[str, _Row] = {}
    edges: list[_Row] = []

    def add_inst(code, root=False):
        instruments[code] = _inst_row(code, root)

    def add_cert(cid, inst, u, **kw):
        certs[cid] = _cert_row(cid, inst, u, **kw)

    eid = 0

    def add_edge(child, parent):
        nonlocal eid
        edges.append(_Row(
            id=eid, child_cert_id=child, parent_cert_id=parent,
            valid_from=FROM, valid_until=UNTIL, note="",
        ))
        eid += 1

    add_inst("G", root=True)
    add_cert("CG", "G", 0.0001)
    add_inst("W")
    add_cert("CW", "W", 0.1)
    prev = "CW"
    for i in range(1, 11):
        add_inst(f"I{i}")
        add_cert(f"C{i}", f"I{i}", 0.09 - i * 0.001)
        add_edge(prev, f"C{i}")
        prev = f"C{i}"
    for j in range(1, 4):
        add_inst(f"H{j}")
        add_cert(f"CH{j}", f"H{j}", 0.07)
        add_edge("C10", f"CH{j}")
        for k in (1, 2):
            add_inst(f"P{j}{k}")
            add_cert(f"CP{j}{k}", f"P{j}{k}", 0.06)
            add_edge(f"CH{j}", f"CP{j}{k}")
            add_edge(f"CP{j}{k}", "CG")

    add_inst("X1")
    add_cert("CX1", "X1", 0.01)
    add_edge("CX1", "CG")

    add_inst("GAP")
    add_cert("CGAP1", "GAP", 0.5,
             valid_from="2024-01-01T00:00:00Z",
             valid_until="2025-01-01T00:00:00Z",
             issued="2024-01-05T00:00:00Z")
    add_cert("CGAP2", "GAP", 0.4,
             valid_from="2025-06-01T00:00:00Z",
             valid_until="2027-01-01T00:00:00Z",
             issued="2025-06-05T00:00:00Z")
    add_edge("CGAP2", "CG")

    add_inst("NEST")
    add_cert("CNEST-L", "NEST", 0.02)
    add_cert("CNEST-S", "NEST", 0.02,
             valid_from="2025-01-01T00:00:00Z",
             valid_until="2025-03-01T00:00:00Z",
             issued="2025-01-05T00:00:00Z")
    add_edge("CNEST-L", "CG")

    add_inst("BAD")
    add_cert("CBAD", "BAD", 0.01, bad_digest=True)
    add_edge("CBAD", "CG")

    return instruments, certs, edges


# ---------------------------------------------------------------- 索引建立

def test_catalog_loads_indexes_from_database(client):
    """从数据库连接装载时一次性建立三类索引。"""
    _submit(client)
    with get_conn_ctx() as conn:
        cat = Catalog(conn)

    # 按仪器分组的证书索引（保持装载顺序）
    assert {
        code: [c["id"] for c in lst]
        for code, lst in cat.certs_by_instrument.items()
    } == {"G": ["CG"], "R": ["CR"], "W": ["CW"]}
    assert cat.certs_of("W")[0]["id"] == "CW"
    assert cat.certs_of("NOPE") == []
    # 按边 ID 定位的索引
    assert set(cat.edges_by_id) == {e["id"] for e in cat.edges}
    assert all(cat.edges_by_id[e["id"]] is e for e in cat.edges)
    # 父子边索引
    assert [e["parent_cert_id"] for e in cat.out_edges["CW"]] == ["CR"]
    assert [e["parent_cert_id"] for e in cat.out_edges["CR"]] == ["CG"]

    # 派生证书停用视图同样基于自身数据重建索引，且不触碰原快照
    simulated = _catalog_without_certificates(cat, {"CR"})
    assert set(simulated.certs) == {"CG", "CW"}
    assert {
        code: [c["id"] for c in lst]
        for code, lst in simulated.certs_by_instrument.items()
    } == {"G": ["CG"], "W": ["CW"]}
    assert simulated.edges == []
    assert simulated.edges_by_id == {}
    assert simulated.out_edges == {}
    # 原快照数据与索引均未受影响
    assert set(cat.certs) == {"CG", "CR", "CW"}
    assert len(cat.edges_by_id) == 2
    assert [e["parent_cert_id"] for e in cat.out_edges["CW"]] == ["CR"]


def test_derived_snapshot_indexes_do_not_pollute_base():
    """派生快照的索引只属于派生 Catalog：原快照可继续给出相同结果。"""
    instruments, certs, edges = _multi_branch_dataset()
    cat = Catalog(instruments=instruments, certs=certs, edges=edges)
    before = _build_report(cat, "W", AT_Z)

    simulated = _catalog_without_certificates(cat, {"CH1"})
    removed_edge_ids = {
        e["id"] for e in edges
        if e["child_cert_id"] == "CH1" or e["parent_cert_id"] == "CH1"
    }
    assert removed_edge_ids  # C10->CH1 与 CH1->CP11/CP12 共 3 条
    # 派生快照：证书/边与三类索引都只剩移除后的数据
    assert "CH1" not in simulated.certs
    assert simulated.certs_of("H1") == []
    assert not (removed_edge_ids & set(simulated.edges_by_id))
    assert "CH1" not in simulated.out_edges
    assert [e["parent_cert_id"] for e in simulated.out_edges["C10"]] == [
        "CH2", "CH3",
    ]

    # 原快照：数据与索引保持完整，重新评估结果逐字段不变
    assert "CH1" in cat.certs
    assert [c["id"] for c in cat.certs_of("H1")] == ["CH1"]
    assert removed_edge_ids <= set(cat.edges_by_id)
    assert [e["parent_cert_id"] for e in cat.out_edges["C10"]] == [
        "CH1", "CH2", "CH3",
    ]
    assert _build_report(cat, "W", AT_Z) == before

    # 派生快照上的评估与“按剩余数据重新装载的快照”完全一致
    fresh = Catalog(
        instruments=instruments,
        certs={k: v for k, v in certs.items() if k != "CH1"},
        edges=[
            e for e in edges
            if e["child_cert_id"] != "CH1" and e["parent_cert_id"] != "CH1"
        ],
    )
    assert _build_report(simulated, "W", AT_Z) == _build_report(fresh, "W", AT_Z)


# ------------------------------------------------------- 多分支长链结果等价

def test_multi_branch_long_chain_report_matches_expected():
    """6 条候选长链：路径顺序、冲突明细与全库统计保持既有结果。"""
    instruments, certs, edges = _multi_branch_dataset()
    cat = Catalog(instruments=instruments, certs=certs, edges=edges)
    report = _build_report(cat, "W", AT_Z)

    assert report["resolution"] == "ambiguous"
    assert report["valid"] is False
    assert report["starts"] == ["CW"]
    trunk = ["CW"] + [f"C{i}" for i in range(1, 11)]
    # 路径顺序保持 DFS 枚举次序：父证书 id 字典序（CH1<CH2<CH3，CPj1<CPj2）
    assert [
        [n["certificate_id"] for n in c["nodes"]]
        for c in report["candidates"]
    ] == [
        trunk + ["CH1", "CP11", "CG"],
        trunk + ["CH1", "CP12", "CG"],
        trunk + ["CH2", "CP21", "CG"],
        trunk + ["CH2", "CP22", "CG"],
        trunk + ["CH3", "CP31", "CG"],
        trunk + ["CH3", "CP32", "CG"],
    ]
    assert all(c["reaches_national_standard"] for c in report["candidates"])
    assert all(c["errors"] == 0 and c["warnings"] == 0
               for c in report["candidates"])
    assert report["broken_chains"] == []

    # 冲突明细：仅 MULTIPLE_VALID_CHAINS，附全部候选链下标
    assert [
        (c["code"], c["severity"], c["path_index"])
        for c in report["conflicts"]
    ] == [("MULTIPLE_VALID_CHAINS", "ERROR", None)]
    assert report["conflicts"][0]["detail"]["path_indices"] == [0, 1, 2, 3, 4, 5]
    assert report["error_count"] == 1
    assert report["warning_count"] == 0

    # 全库统计：包含与链无关的其它仪器/证书/边
    assert report["statistics"] == {
        "instruments_total": 25,
        "certificates_total": 27,
        "edges_total": 29,
        "paths_enumerated": 6,
        "paths_truncated": False,
        "max_paths": MAX_PATHS,
        "root_reaching_chains": 6,
        "valid_root_chains": 6,
        "active_chain_depth": None,
    }


def test_multi_instrument_reports_preserve_conflict_details():
    """多仪器数据：断档/嵌套区间/摘要篡改等冲突明细保持既有结果。"""
    instruments, certs, edges = _multi_branch_dataset()
    cat = Catalog(instruments=instruments, certs=certs, edges=edges)

    gap = _build_report(cat, "GAP", AT_Z)
    assert gap["resolution"] == "unique"
    assert gap["valid"] is False  # 链唯一但存在历史断档 ERROR
    gaps = [c for c in gap["conflicts"] if c["code"] == "INTERVAL_GAP"]
    assert len(gaps) == 1
    assert gaps[0]["node"] == "CGAP1"
    assert gaps[0]["instrument"] == "GAP"
    assert gaps[0]["detail"] == {
        "prev_cert_id": "CGAP1",
        "next_cert_id": "CGAP2",
        "gap_from": "2025-01-01T00:00:00Z",
        "gap_to": "2025-06-01T00:00:00Z",
    }

    nest = _build_report(cat, "NEST", AT_Z)
    assert nest["resolution"] == "unique"
    assert nest["valid"] is True
    assert nest["error_count"] == 0
    assert not any(c["code"] == "INTERVAL_GAP" for c in nest["conflicts"])

    bad = _build_report(cat, "BAD", AT_Z)
    assert bad["resolution"] == "broken"
    mism = [
        c for c in bad["conflicts"] if c["code"] == "SIGNATURE_DIGEST_MISMATCH"
    ]
    assert len(mism) == 1
    assert mism[0]["node"] == "CBAD"
    assert mism[0]["detail"]["digest_alg"] == "sha256"
    assert any(c["code"] == "ALL_CHAINS_FAILED_CHECKS" for c in bad["conflicts"])

    x1 = _build_report(cat, "X1", AT_Z)
    assert x1["resolution"] == "unique"
    assert x1["valid"] is True
    assert [n["certificate_id"] for n in x1["active_chain"]["nodes"]] == [
        "CX1", "CG",
    ]


# ------------------------------------------- 扫描次数随快照而非候选链增长

def test_full_scans_grow_with_snapshots_not_candidate_chains():
    """全表扫描只发生在快照装载/派生时，与候选链数、相关仪器数无关。"""
    instruments, certs, edges = _multi_branch_dataset()
    counted_certs = _ScanCountingDict(certs)
    counted_edges = _ScanCountingList(edges)
    cat = Catalog(
        instruments=instruments, certs=counted_certs, edges=counted_edges
    )
    # 装载即建索引：各恰好一次全量扫描
    assert counted_certs.value_scans == 1
    assert counted_edges.iterations == 1

    report = _build_report(cat, "W", AT_Z)
    # 多分支长链：6 条候选链，链上共 18 台仪器参与历史断档检查
    assert report["statistics"]["paths_enumerated"] == 6
    assert report["statistics"]["root_reaching_chains"] == 6
    # 链级校验/链序列化不再按候选链重建全边映射，
    # 起点筛选/历史断档检查不再按仪器重扫全库证书
    assert counted_edges.iterations == 1
    assert counted_certs.value_scans == 1

    # 同一快照上再评估多台仪器，扫描次数仍不增长
    for code in ("X1", "GAP", "NEST", "BAD"):
        _build_report(cat, code, AT_Z)
    assert counted_edges.iterations == 1
    assert counted_certs.value_scans == 1

    # 派生一个停用模拟快照：对原快照的边列表多一次过滤扫描
    # （扫描随快照数增长）；派生快照上的评估不再触碰原快照数据
    simulated = _catalog_without_certificates(cat, {"CH1"})
    assert counted_edges.iterations == 2
    after = _build_report(simulated, "W", AT_Z)
    assert after["statistics"]["root_reaching_chains"] == 4
    assert counted_edges.iterations == 2
    assert counted_certs.value_scans == 1


def test_truncated_undetermined_keeps_semantics_and_scan_counts():
    """500 条截断未定语义不变，且 500 条样本链同样不触发额外表扫描。"""
    instruments = {"W": _inst_row("W"), "G": _inst_row("G", root=True)}
    certs = {
        "CW": _cert_row("CW", "W", 0.01),
        "CG": _cert_row("CG", "G", 0.0001),
    }
    edges: list[_Row] = []
    eid = 0

    def add_edge(child, parent):
        nonlocal eid
        edges.append(_Row(
            id=eid, child_cert_id=child, parent_cert_id=parent,
            valid_from=FROM, valid_until=UNTIL, note="",
        ))
        eid += 1

    # 506 条死路父边（CB* 字典序先于 CG）先填满 500 上限，合格链不被枚举
    for i in range(MAX_PATHS + 6):
        instruments[f"B{i}"] = _inst_row(f"B{i}")
        certs[f"CB{i}"] = _cert_row(f"CB{i}", f"B{i}", 0.001)
        add_edge("CW", f"CB{i}")
    add_edge("CW", "CG")

    counted_certs = _ScanCountingDict(certs)
    counted_edges = _ScanCountingList(edges)
    cat = Catalog(
        instruments=instruments, certs=counted_certs, edges=counted_edges
    )
    assert counted_certs.value_scans == 1
    assert counted_edges.iterations == 1

    report = _build_report(cat, "W", AT_Z)
    assert report["resolution"] == "undetermined"
    assert report["valid"] is False
    assert report["active_chain"] is None
    assert report["statistics"]["paths_enumerated"] == MAX_PATHS
    assert report["statistics"]["paths_truncated"] is True
    assert len(report["broken_chains"]) == MAX_PATHS
    codes = {c["code"] for c in report["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "TOO_MANY_CANDIDATES" in codes
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    # 500 条样本链的链级校验与序列化复用快照索引：无任何新增全表扫描
    assert counted_edges.iterations == 1
    assert counted_certs.value_scans == 1


# ------------------------------------------------------- 后续请求读取新数据

def test_subsequent_requests_read_fresh_data(client):
    """每次请求重新装载快照：新提交立即可见，停用模拟不落库。"""
    _submit(client)
    first = client.get(f"/api/trace/W?at={AT}").json()
    assert first["resolution"] == "unique"

    # 新提交第二参考通道后，后续请求必须读到新数据（唯一 -> 多候选）
    r = client.post(
        "/api/submissions",
        json={
            "instruments": [{"code": "R2", "name": "第二参考标准器"}],
            "certificates": [
                {"id": "CR2", "instrument_code": "R2", "cert_number": "R2-1",
                 "issued_at": "2025-07-01T00:00:00",
                 "valid_from": "2025-07-01T00:00:00",
                 "valid_until": "2027-07-01T00:00:00",
                 "uncertainty": 0.002, "unit": "mm"},
            ],
            "edges": [
                {"child_cert_id": "CW", "parent_cert_id": "CR2"},
                {"child_cert_id": "CR2", "parent_cert_id": "CG"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    second = client.get(f"/api/trace/W?at={AT}").json()
    assert second["resolution"] == "ambiguous"
    assert len(second["candidates"]) == 2

    # 证书停用模拟（dry-run）不得污染后续请求读取的数据
    d = client.post(
        "/api/assessments/certificate-deactivation",
        json={"at": AT, "certificate_ids": ["CR", "CR2"]},
    ).json()
    assert d["dry_run"] is True
    assert d["statistics"]["certificates_after"] == 2  # 仅模拟视图内移除
    third = client.get(f"/api/trace/W?at={AT}").json()
    assert third["resolution"] == "ambiguous"
    assert len(third["candidates"]) == 2
    assert third["statistics"]["certificates_total"] == 4
