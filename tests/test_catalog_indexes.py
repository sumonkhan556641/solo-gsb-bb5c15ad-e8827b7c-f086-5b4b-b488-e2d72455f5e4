"""Catalog 派生索引的结构与行为回归测试。

重整点：``Catalog`` 在装载（数据库或派生证书停用视图）时一次性建立
按仪器分组的证书索引（``certs_by_instrument``，按 ``(valid_from,
valid_until)`` 预排序）、按边 ID 定位的索引（``edges_by_id``）与父子边
索引（``out_edges``），并预取国家基准证书 id 列表（``root_cert_ids``）；
链路枚举、历史区间断档与链级校验全部复用这些索引，不再每条候选链重建
全边映射、每台相关仪器重复扫描全库证书。索引只属于当前快照：停用模拟
派生新 Catalog 时随构造重建，不污染原快照；每次评估重新装载，后续请求
总是读取新数据。本组验证：

* 内存 / 数据库两种装载路径的索引内容（分组、预排序、定位、基准预取）；
* 停用派生视图的索引独立：排除被停用证书与关联边，原快照索引不受污染；
* 后续装载读取新数据，旧快照索引不被新数据回写；
* 多分支长链 + 多仪器场景下报告结果与既有语义等价（路径顺序、500 条
  截断 undetermined、冲突明细与全库统计）；
* 扫描量随快照规模增长而非候选链数：报告期间零次全表证书扫描、零次
  全边列表迭代，索引每个快照仅建立一次。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_conn_ctx
from app.engine import (
    MAX_PATHS,
    Catalog,
    _build_report,
    _catalog_without_certificates,
)
from app.utils import compute_digest

AT_Z = "2026-06-15T00:00:00Z"
FROM = "2024-06-01T00:00:00Z"
UNTIL = "2029-01-01T00:00:00Z"
ISSUED = "2025-01-01T00:00:00Z"


class _Row(dict):
    """同时支持 ``row["k"]`` 与 ``row.k`` 访问的 sqlite3.Row 兼容字典。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _inst(code, root=False):
    return _Row(code=code, name=code, is_national_standard=1 if root else 0)


def _cert(cid, inst, u, valid_from=FROM, valid_until=UNTIL, issued=ISSUED):
    payload = (
        '{"cert_number":"%s","id":"%s","instrument_code":"%s","issued_at":"%s",'
        '"uncertainty":%s,"unit":"mm","valid_from":"%s","valid_until":"%s"}'
        % (cid, cid, inst, issued, u, valid_from, valid_until)
    )
    return _Row(
        id=cid, instrument_code=inst, cert_number=cid,
        issued_at=issued, valid_from=valid_from, valid_until=valid_until,
        uncertainty=u, unit="mm", digest_alg="sha256", signed_payload=payload,
        signature_digest=compute_digest(payload),
    )


def _edge(eid, child, parent, valid_from=FROM, valid_until=UNTIL):
    return _Row(
        id=eid, child_cert_id=child, parent_cert_id=parent,
        valid_from=valid_from, valid_until=valid_until, note="",
    )


def _fan_catalog(fan, trunk=3):
    """多分支长链 + 多仪器：CW -> CT1..CTn（深主干）-> 枢纽 CH（仪器 M）；
    CH 挂 fan 条死路父边（DB0..，同属仪器 M，使涉及仪器集合不随 fan 变化）
    与一条到国家基准 CG 的有效边。

    父证书 id 字典序 CG < DB*，到根分支先于死路分支被枚举。
    """
    instruments = {"G": _inst("G", True), "W": _inst("W"), "M": _inst("M")}
    certs = {
        "CG": _cert("CG", "G", 0.0001),
        "CW": _cert("CW", "W", 0.01),
        "CH": _cert("CH", "M", 0.005),
    }
    edges = []
    eid = 0

    def add(child, parent):
        nonlocal eid
        edges.append(_edge(eid, child, parent))
        eid += 1

    prev = "CW"
    for i in range(1, trunk + 1):
        instruments[f"T{i}"] = _inst(f"T{i}")
        certs[f"CT{i}"] = _cert(
            f"CT{i}", f"T{i}", 0.01 - i * (0.01 - 0.005) / (trunk + 1)
        )
        add(prev, f"CT{i}")
        prev = f"CT{i}"
    add(prev, "CH")
    for i in range(fan):
        certs[f"DB{i}"] = _cert(f"DB{i}", "M", 0.0009)
        add("CH", f"DB{i}")
    add("CH", "CG")
    return Catalog(instruments=instruments, certs=certs, edges=edges)


# ------------------------------------------------------------ 索引结构

def test_indexes_built_on_inmemory_load():
    instruments = {
        "G": _inst("G", True),
        "G2": _inst("G2", True),
        "W": _inst("W"),
    }
    # 同一仪器多张证书，装载次序与有效期次序故意不一致
    certs = {
        "CW-late": _cert("CW-late", "W", 0.01,
                         valid_from="2027-01-01T00:00:00Z",
                         valid_until="2028-01-01T00:00:00Z"),
        "CG": _cert("CG", "G", 0.0001),
        "CW-early": _cert("CW-early", "W", 0.01,
                          valid_from="2025-01-01T00:00:00Z",
                          valid_until="2026-01-01T00:00:00Z"),
        "CG2": _cert("CG2", "G2", 0.0002),
    }
    edges = [
        _edge(7, "CW-early", "CG"),
        _edge(3, "CW-late", "CG2"),
        _edge(9, "CW-late", "CG"),
    ]
    cat = Catalog(instruments=instruments, certs=certs, edges=edges)

    # 按仪器分组的证书索引：分组正确且按 (valid_from, valid_until) 预排序
    assert set(cat.certs_by_instrument) == {"G", "G2", "W"}
    assert [c["id"] for c in cat.certs_by_instrument["W"]] == [
        "CW-early",
        "CW-late",
    ]
    assert [c["id"] for c in cat.certs_of("G")] == ["CG"]
    assert cat.certs_of("NO-SUCH") == []

    # 按边 ID 定位的索引：每条边可按 id 直接取到
    assert set(cat.edges_by_id) == {3, 7, 9}
    assert cat.edges_by_id[3]["child_cert_id"] == "CW-late"
    assert cat.edges_by_id[7]["parent_cert_id"] == "CG"

    # 父子边索引：子证书 -> 全部向上溯源边（保持装载次序）
    assert set(cat.out_edges) == {"CW-early", "CW-late"}
    assert [e["id"] for e in cat.out_edges["CW-late"]] == [3, 9]

    # 基准证书预取：保持证书装载次序（CG 先于 CG2）
    assert cat.root_cert_ids == ["CG", "CG2"]


def test_indexes_built_on_db_load(client: TestClient):
    r = client.post(
        "/api/submissions",
        json={
            "instruments": [
                {"code": "G", "name": "国家基准", "is_national_standard": True},
                {"code": "W", "name": "工作仪器"},
            ],
            "certificates": [
                {"id": "CG", "instrument_code": "G", "cert_number": "N-1",
                 "issued_at": ISSUED, "valid_from": FROM, "valid_until": UNTIL,
                 "uncertainty": 0.0001, "unit": "mm"},
                {"id": "CW-2", "instrument_code": "W", "cert_number": "C-2",
                 "issued_at": ISSUED, "valid_from": "2026-01-01T00:00:00Z",
                 "valid_until": UNTIL, "uncertainty": 0.002, "unit": "mm"},
                {"id": "CW-1", "instrument_code": "W", "cert_number": "C-1",
                 "issued_at": ISSUED, "valid_from": FROM,
                 "valid_until": "2026-01-01T00:00:00Z",
                 "uncertainty": 0.002, "unit": "mm"},
            ],
            "edges": [
                {"child_cert_id": "CW-1", "parent_cert_id": "CG"},
                {"child_cert_id": "CW-2", "parent_cert_id": "CG"},
            ],
        },
    )
    assert r.status_code == 201, r.text[:500]

    with get_conn_ctx() as conn:
        cat = Catalog(conn)

    # 数据库装载与内存装载走同一套索引
    assert [c["id"] for c in cat.certs_of("W")] == ["CW-1", "CW-2"]
    assert [c["id"] for c in cat.certs_of("G")] == ["CG"]
    assert cat.root_cert_ids == ["CG"]
    assert len(cat.edges_by_id) == 2
    by_child = {cid: len(es) for cid, es in cat.out_edges.items()}
    assert by_child == {"CW-1": 1, "CW-2": 1}
    # 统计仍基于全库快照
    rep = _build_report(cat, "W", "2026-06-15T00:00:00Z")
    assert rep["statistics"]["instruments_total"] == 2
    assert rep["statistics"]["certificates_total"] == 3
    assert rep["statistics"]["edges_total"] == 2


# ------------------------------------------------- 派生视图与快照隔离

def test_deactivation_view_indexes_independent():
    cat = _fan_catalog(fan=4)
    base_groups = {k: list(v) for k, v in cat.certs_by_instrument.items()}
    base_edges_by_id = dict(cat.edges_by_id)
    base_out = {k: list(v) for k, v in cat.out_edges.items()}
    base_roots = list(cat.root_cert_ids)

    simulated = _catalog_without_certificates(cat, {"CH", "DB0"})

    # 派生视图索引只含留存内容：CH/DB0 从分组、边索引与父子边索引中消失
    assert "CH" not in simulated.certs
    assert [c["id"] for c in simulated.certs_of("M")] == [
        f"DB{i}" for i in range(1, 4)
    ]
    kept_edge_ids = {
        e["id"] for e in cat.edges
        if e["child_cert_id"] not in {"CH", "DB0"}
        and e["parent_cert_id"] not in {"CH", "DB0"}
    }
    assert set(simulated.edges_by_id) == kept_edge_ids
    assert "CH" not in simulated.out_edges
    assert all(
        e["parent_cert_id"] not in {"CH", "DB0"}
        for es in simulated.out_edges.values()
        for e in es
    )
    assert simulated.root_cert_ids == ["CG"]

    # 原快照索引不受模拟污染
    assert {k: [c["id"] for c in v] for k, v in cat.certs_by_instrument.items()} == {
        k: [c["id"] for c in v] for k, v in base_groups.items()
    }
    assert cat.edges_by_id == base_edges_by_id
    assert {k: [e["id"] for e in v] for k, v in cat.out_edges.items()} == {
        k: [e["id"] for e in v] for k, v in base_out.items()
    }
    assert cat.root_cert_ids == base_roots
    assert "CH" in cat.certs


def test_fresh_catalog_reads_new_data(client: TestClient):
    def submit(cert_id, edge=False):
        payload = {
            "instruments": [],
            "certificates": [
                {"id": cert_id, "instrument_code": "W", "cert_number": cert_id,
                 "issued_at": ISSUED, "valid_from": FROM, "valid_until": UNTIL,
                 "uncertainty": 0.002, "unit": "mm"}
            ],
            "edges": (
                [{"child_cert_id": cert_id, "parent_cert_id": "CG"}]
                if edge else []
            ),
        }
        r = client.post("/api/submissions", json=payload)
        assert r.status_code == 201, r.text[:500]

    r = client.post(
        "/api/submissions",
        json={
            "instruments": [
                {"code": "G", "name": "国家基准", "is_national_standard": True},
                {"code": "W", "name": "工作仪器"},
            ],
            "certificates": [
                {"id": "CG", "instrument_code": "G", "cert_number": "N-1",
                 "issued_at": ISSUED, "valid_from": FROM, "valid_until": UNTIL,
                 "uncertainty": 0.0001, "unit": "mm"}
            ],
            "edges": [],
        },
    )
    assert r.status_code == 201, r.text[:500]

    with get_conn_ctx() as conn:
        first = Catalog(conn)
    submit("CW-1", edge=True)

    with get_conn_ctx() as conn:
        second = Catalog(conn)

    # 后续装载读取新数据：新证书与边进入索引
    assert [c["id"] for c in second.certs_of("W")] == ["CW-1"]
    assert len(second.edges_by_id) == 1
    assert "CW-1" in second.out_edges
    # 旧快照索引不被回写：仍反映其装载时刻的内容
    assert first.certs_of("W") == []
    assert first.edges_by_id == {}
    assert first.out_edges == {}


# --------------------------------------- 多分支长链 + 多仪器的结果等价

def test_multi_branch_long_chain_report_semantics():
    cat = _fan_catalog(fan=4, trunk=30)
    rep = _build_report(cat, "W", AT_Z)

    assert rep["resolution"] == "unique"
    trunk = ["CW"] + [f"CT{i}" for i in range(1, 31)] + ["CH"]
    # 唯一有效主链：叶子 -> 深主干 -> 枢纽 -> 国家基准，路径顺序保持
    assert [n["certificate_id"] for n in rep["active_chain"]["nodes"]] == (
        trunk + ["CG"]
    )
    # 死路分支按父证书 id 字典序（DB0..DB3）依次记录
    assert len(rep["broken_chains"]) == 4
    for i, chain in enumerate(rep["broken_chains"]):
        nodes = [n["certificate_id"] for n in chain["nodes"]]
        assert nodes == trunk + [f"DB{i}"]
    dead = [c for c in rep["conflicts"] if c["code"] == "DEAD_END"]
    assert [c["node"] for c in dead] == [f"DB{i}" for i in range(4)]
    # 全库统计基于完整快照而非候选链
    assert rep["statistics"]["certificates_total"] == len(cat.certs)
    assert rep["statistics"]["edges_total"] == len(cat.edges)
    assert rep["statistics"]["instruments_total"] == len(cat.instruments)
    assert rep["statistics"]["paths_enumerated"] == 5
    assert rep["statistics"]["paths_truncated"] is False
    assert rep["statistics"]["root_reaching_chains"] == 1
    assert rep["statistics"]["valid_root_chains"] == 1


def test_multi_instrument_truncation_semantics_preserved():
    # 601 条候选（1 条到基准 + 600 条死路）超过 500 上限：已见合格链
    # 不足 2 条 -> undetermined，样本与截断说明保留，路径顺序确定。
    cat = _fan_catalog(fan=600, trunk=5)
    rep = _build_report(cat, "W", AT_Z)

    assert rep["resolution"] == "undetermined"
    assert rep["active_chain"] is None
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    assert rep["statistics"]["paths_truncated"] is True
    codes = {c["code"] for c in rep["conflicts"]}
    assert "RESULT_UNDETERMINED" in codes
    assert "TOO_MANY_CANDIDATES" in codes
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    # 到根分支（父 id 字典序最小）最先枚举，成为唯一样本候选
    assert len(rep["candidates"]) == 1
    trunk = ["CW"] + [f"CT{i}" for i in range(1, 6)] + ["CH"]
    assert [n["certificate_id"] for n in rep["candidates"][0]["nodes"]] == (
        trunk + ["CG"]
    )
    # 其余 499 条样本为死路分支，按父证书 id 字典序（DB0, DB1, DB10, ...）
    expected_dead = sorted(f"DB{i}" for i in range(600))[: MAX_PATHS - 1]
    assert len(rep["broken_chains"]) == MAX_PATHS - 1
    for chain, dead_id in zip(rep["broken_chains"], expected_dead):
        assert [n["certificate_id"] for n in chain["nodes"]] == (
            trunk + [dead_id]
        )


# --------------------------------------- 扫描量随快照而非候选链数增长

class _CountingList(list):
    """统计 ``__iter__`` 调用次数的列表（全表迭代探测器）。"""

    def __init__(self, *args):
        super().__init__(*args)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class _CountingDict(dict):
    """统计 ``values()`` / ``items()`` 全表扫描次数的字典。"""

    def __init__(self, *args):
        super().__init__(*args)
        self.values_calls = 0
        self.items_calls = 0

    def values(self):
        self.values_calls += 1
        return super().values()

    def items(self):
        self.items_calls += 1
        return super().items()


def test_report_does_not_iterate_full_edge_list():
    # 500 条截断样本链：链级校验与序列化按边 ID 索引取边，
    # 全程不再迭代全边列表（旧实现每条链重建两次全边映射）。
    cat = _fan_catalog(fan=600, trunk=5)
    cat.edges = _CountingList(cat.edges)
    rep = _build_report(cat, "W", AT_Z)
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    assert cat.edges.iterations == 0


def test_report_does_not_scan_full_cert_store():
    # 起点选取、历史断档与基准巡检全部走分组/预取索引，
    # 全程不再 values()/items() 全表扫描证书（节点访问为 O(1) 按键取值）。
    cat = _fan_catalog(fan=600, trunk=5)
    cat.certs = _CountingDict(cat.certs)
    rep = _build_report(cat, "W", AT_Z)
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    assert cat.certs.values_calls == 0
    assert cat.certs.items_calls == 0


def test_indexes_built_once_per_snapshot(monkeypatch):
    builds = {"n": 0}
    original = Catalog._build_indexes

    def spy(self):
        builds["n"] += 1
        return original(self)

    monkeypatch.setattr(Catalog, "_build_indexes", spy)
    cat = _fan_catalog(fan=600, trunk=5)
    assert builds["n"] == 1
    rep = _build_report(cat, "W", AT_Z)
    assert rep["statistics"]["paths_enumerated"] == MAX_PATHS
    # 报告评估（含 500 条样本链的链级校验）不触发任何索引重建
    assert builds["n"] == 1
    # 派生停用视图在自己的构造中重建一次，原快照不再重建
    _catalog_without_certificates(cat, {"CH"})
    assert builds["n"] == 2


def test_cert_scan_volume_tracks_snapshot_not_path_count(monkeypatch):
    # 两个快照：仪器/主干完全相同，仅枢纽死路分支数不同
    # （11 条候选 vs 截断后的 500 条样本）。死路父证书同属仪器 M，
    # 两快照涉及的仪器集合一致。证书扫描量的增量应等于证书数量的
    # 增量（随快照增长），而非随候选链数放大。
    small = _fan_catalog(fan=10, trunk=3)
    large = _fan_catalog(fan=600, trunk=3)

    scans = {"rows": 0}
    original = Catalog.certs_of

    def spy(self, instrument_code):
        rows = original(self, instrument_code)
        scans["rows"] += len(rows)
        return rows

    monkeypatch.setattr(Catalog, "certs_of", spy)

    rep_small = _build_report(small, "W", AT_Z)
    rows_small = scans["rows"]
    scans["rows"] = 0
    rep_large = _build_report(large, "W", AT_Z)
    rows_large = scans["rows"]

    assert rep_small["statistics"]["paths_enumerated"] == 11
    assert rep_large["statistics"]["paths_enumerated"] == MAX_PATHS
    # 扫描量上界为快照规模的常数倍（每台涉及仪器仅扫自己的分组），
    # 与候选链数无关；旧实现为“涉及仪器数 × 全库证书数”。
    assert rows_small <= 2 * len(small.certs)
    assert rows_large <= 2 * len(large.certs)
    # 扫描量增量 == 快照证书增量（490 张死路父证书），
    # 而候选链数从 11 涨到 500 并不额外放大扫描量。
    assert rows_large - rows_small == len(large.certs) - len(small.certs)
