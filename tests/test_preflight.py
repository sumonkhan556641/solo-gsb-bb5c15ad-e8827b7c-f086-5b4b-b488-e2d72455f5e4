AT = "2026-06-15T00:00:00"


def _instruments():
    return [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "R", "name": "参考标准器"},
        {"code": "W", "name": "工作仪器"},
    ]


def _certs():
    return [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]


def _edges():
    return [
        {"child_cert_id": "CW", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]


def _preflight(client, instruments=None, certs=None, edges=None,
               strict_digest=False):
    return client.post(
        "/api/submissions/preflight",
        json={
            "instruments": _instruments() if instruments is None else instruments,
            "certificates": _certs() if certs is None else certs,
            "edges": _edges() if edges is None else edges,
            "strict_digest": strict_digest,
        },
    )


def test_preflight_valid_payload(client):
    r = _preflight(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["error_count"] == 0
    assert data["warning_count"] == 0
    assert data["would_upsert"] == {
        "instruments": 3, "certificates": 3, "edges": 2, "total": 8
    }
    # 预检不写库：仪器列表应为空
    assert client.get("/api/instruments").json() == []


def test_preflight_never_writes_even_with_errors(client):
    # 库中预置一个仪器，预检中引用不存在证书、制造自环等
    r = _preflight(client, certs=[], edges=[
        {"child_cert_id": "X", "parent_cert_id": "Y"},
        {"child_cert_id": "CW", "parent_cert_id": "CW"},
    ])
    assert r.status_code == 200
    assert r.json()["ok"] is False
    # 预检中的仪器 upsert 也必须回滚
    assert client.get("/api/instruments").json() == []


def test_preflight_reference_errors_sorted_with_stable_paths(client):
    # 仪器正常；证书引用不存在仪器；边引用不存在证书
    certs = _certs()
    certs[1]["instrument_code"] = "GHOST"
    edges = [
        {"child_cert_id": "CW", "parent_cert_id": "MISSING"},
        {"child_cert_id": "CW", "parent_cert_id": "CG"},
    ]
    r = _preflight(client, certs=certs, edges=edges)
    data = r.json()
    assert data["ok"] is False
    codes = [(e["path"], e["code"]) for e in data["errors"]]
    # 证书错误排在边错误之前；同类按提交下标
    assert codes == [
        ("certificates/1/instrument_code", "INSTRUMENT_NOT_FOUND"),
        ("edges/0/parent_cert_id", "PARENT_CERT_NOT_FOUND"),
    ]
    # 出错的证书不计入 upsert；引用缺失证书的边也不计入
    assert data["would_upsert"]["certificates"] == 2
    assert data["would_upsert"]["edges"] == 1


def test_preflight_self_loop_and_missing_child(client):
    edges = [
        {"child_cert_id": "CW", "parent_cert_id": "CW"},
        {"child_cert_id": "NOPE", "parent_cert_id": "CG"},
    ]
    r = _preflight(client, edges=edges)
    data = r.json()
    errors = data["errors"]
    codes = {e["code"] for e in errors}
    assert codes == {"EDGE_SELF_LOOP", "CHILD_CERT_NOT_FOUND"}
    loop = next(e for e in errors if e["code"] == "EDGE_SELF_LOOP")
    assert loop["path"] == "edges/0/parent_cert_id"
    missing = next(e for e in errors if e["code"] == "CHILD_CERT_NOT_FOUND")
    assert missing["path"] == "edges/1/child_cert_id"
    assert data["would_upsert"]["edges"] == 0


def test_preflight_certificate_interval_invalid(client):
    certs = _certs()
    certs[2]["valid_from"] = "2028-01-01T00:00:00"  # 晚于 valid_until
    r = _preflight(client, certs=certs, edges=[])
    errors = r.json()["errors"]
    assert [e["code"] for e in errors] == ["CERTIFICATE_INTERVAL_INVALID"]
    assert errors[0]["path"] == "certificates/2/valid_from"
    assert errors[0]["detail"]["valid_from"] == "2028-01-01T00:00:00Z"


def test_preflight_edge_interval_errors(client):
    # 边自身区间倒置 + 与子证书区间不相交
    edges = [
        {"child_cert_id": "CW", "parent_cert_id": "CR",
         "valid_from": "2026-12-01T00:00:00", "valid_until": "2026-01-01T00:00:00"},
        {"child_cert_id": "CW", "parent_cert_id": "CR",
         "valid_from": "2030-01-01T00:00:00", "valid_until": "2031-01-01T00:00:00"},
    ]
    r = _preflight(client, edges=edges)
    errors = r.json()["errors"]
    assert [e["code"] for e in errors] == [
        "EDGE_INTERVAL_INVALID",
        "EDGE_INTERVAL_DISJOINT",
    ]
    assert errors[1]["detail"]["child_valid_until"] == "2027-01-01T00:00:00Z"


def test_preflight_digest_mismatch_warning_by_default(client):
    certs = _certs()
    next(c for c in certs if c["id"] == "CW")["signature_digest"] = "0" * 64
    r = _preflight(client, certs=certs)
    data = r.json()
    assert data["ok"] is True  # 警告不阻断
    assert data["error_count"] == 0
    assert data["warning_count"] == 1
    w = data["warnings"][0]
    assert w["path"] == "certificates/2/signature_digest"
    assert w["code"] == "SIGNATURE_DIGEST_MISMATCH"
    assert w["detail"]["submitted"] == "0" * 64
    assert w["detail"]["recomputed"] != "0" * 64
    # 摘要不匹配不影响 upsert 计数
    assert data["would_upsert"]["certificates"] == 3


def test_preflight_digest_mismatch_error_in_strict_mode(client):
    certs = _certs()
    next(c for c in certs if c["id"] == "CW")["signature_digest"] = "0" * 64
    r = _preflight(client, certs=certs, strict_digest=True)
    data = r.json()
    assert data["ok"] is False
    assert data["error_count"] == 1
    assert data["warning_count"] == 0
    assert data["errors"][0]["code"] == "SIGNATURE_DIGEST_MISMATCH"
    assert data["would_upsert"]["certificates"] == 3  # 摘要问题不阻断写入模拟


def test_preflight_implicit_digest_not_checked(client):
    # 未显式提交 signature_digest：服务端重算，不可能不匹配，无任何告警
    r = _preflight(client)
    assert r.json()["warnings"] == []


def test_preflight_sorting_numeric_index_not_lexical(client):
    # 构造 certificates/2 与 certificates/10 均有错误：数字序 2 在 10 前，
    # 而字典序 "10" 会排在 "2" 前
    insts = [{"code": f"X{i}", "name": f"X{i}"} for i in range(11)]
    certs = [
        {"id": f"C{i}", "instrument_code": f"X{i}", "cert_number": f"N{i}",
         "issued_at": "2025-01-01T00:00:00",
         "valid_from": "2025-01-01T00:00:00",
         "valid_until": "2026-01-01T00:00:00",
         "uncertainty": 1.0, "unit": "mm"}
        for i in range(11)
    ]
    certs[2]["instrument_code"] = "GHOST"   # certificates/2
    certs[10]["instrument_code"] = "GHOST"  # certificates/10
    r = _preflight(client, instruments=insts, certs=certs, edges=[])
    paths = [e["path"] for e in r.json()["errors"]]
    assert paths == [
        "certificates/2/instrument_code",
        "certificates/10/instrument_code",
    ]


def test_preflight_sees_existing_db_rows_and_real_submit_still_works(client):
    # 先正式提交仪器，再只预检证书+边（引用库中既有仪器/证书）
    assert client.post("/api/submissions", json={
        "instruments": _instruments(), "certificates": [], "edges": []
    }).status_code == 201
    r = _preflight(client, instruments=[], certs=_certs(), edges=_edges())
    data = r.json()
    assert data["ok"] is True
    assert data["would_upsert"] == {
        "instruments": 0, "certificates": 3, "edges": 2, "total": 5
    }
    # 预检未替用户写入证书：仪器存在但仍无证书，链路查询保持断裂
    # （仪器存在时返回 200 的 broken 报告，404 仅用于仪器不存在）
    r = client.get(f"/api/trace/W?at={AT}")
    assert r.status_code == 200
    report = r.json()
    assert report["resolution"] == "broken"
    assert report["starts"] == []
    assert "NO_ACTIVE_CERTIFICATE" in {c["code"] for c in report["conflicts"]}
