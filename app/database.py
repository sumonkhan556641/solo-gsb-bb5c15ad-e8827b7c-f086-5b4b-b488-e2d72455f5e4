"""SQLite 连接与建表。

为便于区间比较，时间统一以 ``YYYY-MM-DDTHH:MM:SSZ``（UTC）文本保存，
该格式的字典序与时间顺序一致，因此可直接用字符串比较。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.getenv("TRACE_DB_PATH", "/data/traceability.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    manufacturer TEXT,
    model       TEXT,
    serial_no   TEXT,
    is_national_standard INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id              TEXT PRIMARY KEY,
    instrument_code TEXT NOT NULL REFERENCES instruments(code),
    cert_number     TEXT NOT NULL,
    issued_at       TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    uncertainty     REAL NOT NULL,
    unit            TEXT NOT NULL DEFAULT '',
    digest_alg      TEXT NOT NULL DEFAULT 'sha256',
    signature_digest TEXT NOT NULL,
    signed_payload  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cert_instrument ON certificates(instrument_code);
CREATE INDEX IF NOT EXISTS idx_cert_interval ON certificates(valid_from, valid_until);

-- 证书修订历史：按 ID 写入证书时在同一事务内维护的连续版本快照。
-- 首次保存记 v1；功能上线前已存在的存量证书首次更新前先补录旧值为 v1；
-- 仅业务字段实际变化才追加连续版本，同内容重交不增版。
CREATE TABLE IF NOT EXISTS certificate_revisions (
    certificate_id   TEXT NOT NULL REFERENCES certificates(id),
    version          INTEGER NOT NULL,
    instrument_code  TEXT NOT NULL,
    cert_number      TEXT NOT NULL,
    issued_at        TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    uncertainty      REAL NOT NULL,
    unit             TEXT NOT NULL DEFAULT '',
    digest_alg       TEXT NOT NULL DEFAULT 'sha256',
    signature_digest TEXT NOT NULL,
    signed_payload   TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    PRIMARY KEY (certificate_id, version)
);

CREATE TABLE IF NOT EXISTS trace_edges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    child_cert_id    TEXT NOT NULL REFERENCES certificates(id),
    parent_cert_id   TEXT NOT NULL REFERENCES certificates(id),
    valid_from       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE(child_cert_id, parent_cert_id)
);

CREATE INDEX IF NOT EXISTS idx_edge_child ON trace_edges(child_cert_id);
CREATE INDEX IF NOT EXISTS idx_edge_parent ON trace_edges(parent_cert_id);

-- 校准工位预约：全实验室仅有“单个工位”，占用一律按半开区间 [start_at, end_at)
-- 计算（相接但不重叠，如 [09,10) 与 [10,11) 不冲突）；预约只与仪器相关于
-- 溯源截止，工位占用不分仪器。certificate_valid_until 为确认时刻起点证书
-- valid_until 的快照，重查截止时以库内证书现值为准。
CREATE TABLE IF NOT EXISTS calibration_bookings (
    id               TEXT PRIMARY KEY,
    instrument_code  TEXT NOT NULL REFERENCES instruments(code),
    start_at         TEXT NOT NULL,
    end_at           TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    certificate_id   TEXT NOT NULL,
    certificate_valid_until TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_booking_instrument ON calibration_bookings(instrument_code);
CREATE INDEX IF NOT EXISTS idx_booking_interval ON calibration_bookings(start_at, end_at);

-- 校准工位候补：挂在某条已确认预约上，登记时沿用该预约的起止时段快照，
-- 按登记先后（seq 自增）排队。目标预约取消时在同一写事务内按队列顺序
-- 逐条重验（唯一有效溯源链 / 证书截止 / 与剩余预约半开冲突），首个合格
-- 申请转为已确认预约；目标预约改期时未补位候补标记失效，避免旧时段队列
-- 悬空。booking_id / instrument_code 均不设外键：目标预约取消删除、仪器
-- 档案变化后，候补记录仍须保留用于状态追溯。
-- status: waiting 候补中 | promoted 已补位 | failed 补位重验不合格 | expired 已失效
CREATE TABLE IF NOT EXISTS calibration_waitlist (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    id               TEXT NOT NULL UNIQUE,
    booking_id       TEXT NOT NULL,
    instrument_code  TEXT NOT NULL,
    start_at         TEXT NOT NULL,
    end_at           TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'waiting',
    status_reason    TEXT,
    promoted_booking_id TEXT,
    created_at       TEXT NOT NULL,
    processed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_waitlist_booking
    ON calibration_waitlist(booking_id, status, seq);
CREATE INDEX IF NOT EXISTS idx_waitlist_instrument
    ON calibration_waitlist(instrument_code);

-- 校准工位维护封锁：全实验室仅有“单个工位”，维护窗口同样按半开区间
-- [start_at, end_at) 占用工位（首尾相接允许）。登记在同一写事务内与
-- 既有预约及其他活跃维护窗口一起查重叠；空档查询把维护窗口与预约
-- 一并视作占用；预约确认、批量改期与取消候补补位均在各自写事务内
-- 重查活跃维护窗口，重叠即 409 整体回滚。撤销（revoke）为软删除：
-- 行保留用于追溯，仅把 status 置为 revoked，重查只看 active 窗口。
-- status: active 生效中 | revoked 已撤销
CREATE TABLE IF NOT EXISTS station_maintenance_windows (
    id          TEXT PRIMARY KEY,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_maintenance_interval
    ON station_maintenance_windows(start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_maintenance_status
    ON station_maintenance_windows(status, start_at);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 预约确认在同一写事务（BEGIN IMMEDIATE）内重查占用并写入，
    # 给写锁竞争留出等待窗口，避免立即 SQLITE_BUSY。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_conn_ctx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_preflight_conn() -> Iterator[sqlite3.Connection]:
    """预检专用连接：独立于请求连接，进入即开启事务，退出一律 ROLLBACK。

    预检流程可在其中照常执行 upsert 语句做高保真模拟，但任何写入都随回滚
    消失，绝不持久化到数据库。
    """
    conn = _connect()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def init_db() -> None:
    with get_conn_ctx() as conn:
        conn.executescript(SCHEMA)
