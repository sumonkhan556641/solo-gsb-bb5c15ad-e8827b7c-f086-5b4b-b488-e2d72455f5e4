"""时间规范化与签名摘要工具。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# 与数据库文本列比较保持一致的 UTC 序列化格式
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# 服务端可重算的摘要算法（取 hashlib 保证可用的交集）
SUPPORTED_ALGS = {"sha224", "sha256", "sha384", "sha512"}
DEFAULT_ALG = "sha256"

UNCERTAINTY_EPS = 1e-12


def to_utc_naive(dt: datetime) -> datetime:
    """把输入时间统一为 naive UTC。"""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fmt(dt: datetime) -> str:
    return to_utc_naive(dt).strftime(TS_FMT)


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def canonical_payload(c: dict[str, Any]) -> str:
    """证书签名载荷的规范化 JSON（键排序、无空白）。

    客户端未显式提交 ``signed_payload`` 时，服务端按此固定字段集重算，
    保证“提交即签名”的摘要可在日后被独立复核。
    """
    fields = (
        "id",
        "instrument_code",
        "cert_number",
        "issued_at",
        "valid_from",
        "valid_until",
        "uncertainty",
        "unit",
    )
    return json.dumps(
        {k: c[k] for k in fields if k in c},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_digest(payload: str, alg: str = DEFAULT_ALG) -> str:
    if alg not in SUPPORTED_ALGS:
        raise ValueError(f"不支持的摘要算法: {alg}（支持: {sorted(SUPPORTED_ALGS)})")
    return hashlib.new(alg, payload.encode("utf-8")).hexdigest()
