"""SQLite persistence for composition tasks.

任务状态机：pending -> running -> done（结果写入 result 列）。
后台线程与请求线程各自建立连接，配合进程内互斥锁串行化写操作。

组卷模板（templates 表）：创建后不可变——只提供插入与查询，没有任何更新
接口；修改约束配置只能通过创建新版本（同一 root_id 谱系下 version 递增）。

题目曝光配额（quotas / quota_reservations / quota_leases 表）：按题号维护可用
次数；启用配额的任务在可行组卷完成时，于**单个 SQLite 事务**（BEGIN IMMEDIATE）
中串行化校验并原子扣减——任一题余额不足则整组不扣减，任务结果改写为
quota_exceeded 并附缺额明细；扣减成功时逐题写入预留记录（含扣后剩余快照），
供任务查询接口回显“本次预留及剩余次数”。

可过期预留租约（quota_leases 表）：创建启用配额的任务时在同一事务登记
lease_seconds 与 reservation_token（仅存令牌 SHA-256 哈希），任务成功扣减后
租约进入 active 并记录 expires_at；用令牌主动确认则原子转为 confirmed（配额
永久消耗），重复确认幂等。超过 expires_at 仍未确认的租约由回收接口在单个
BEGIN IMMEDIATE 事务中逐个执行“仅当仍为 active”的条件翻转并归还配额——
进程锁 + 条件 UPDATE 保证并发回收不会把同一配额释放两次；过期后确认同样
幂等，返回 expired 且不再触发释放。租约每次状态流转（confirmed/expired）都与
业务改动同事务追加审计事件。

防篡改审计链（task_audit_events 表）：任务创建（created）、开始（started）、
完成或失败（finished）、配额结算（quota_settled）及租约确认/过期回收
（quota_confirmed / quota_expired）均在**同一任务状态写入所在的既有 SQLite
事务内**追加事件，与业务改动原子提交。事件含序号 seq（同任务从 1 连续递增）、
UTC 时间、类型与最小业务载荷；事件哈希为规范化 JSON 载荷（含
task_id/seq/type/created_at/payload/prev_hash，不含 hash 本身）的 SHA-256，
首事件前驱为 64 个 0（创世哈希）。校验时逐条重算哈希、核对序号与前驱，并与
tasks/quota_reservations/quota_leases 表中的实际内容摘要交叉比对，发现首个
断点即停止，绝不伪报通过。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("EXAM_DB_PATH", "/data/exam.db")

_lock = threading.Lock()

#: 审计链创世前驱哈希：每个任务首事件的 prev_hash 固定为 64 个 0。
GENESIS_HASH = "0" * 64

#: 租约状态：pending（已登记、任务未结算）→ active（已扣减、等待确认）
#: → confirmed（已确认，永久消耗）/ expired（已过期并由回收接口释放）。
LEASE_PENDING = "pending"
LEASE_ACTIVE = "active"
LEASE_CONFIRMED = "confirmed"
LEASE_EXPIRED = "expired"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock, get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                request     TEXT NOT NULL,
                result      TEXT,
                seed        INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                config      TEXT NOT NULL,
                version     INTEGER NOT NULL,
                root_id     INTEGER,
                base_id     INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 题目曝光配额：question_id 存 JSON 编码的题号（整数 1 与字符串 "1" 互不冲突）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotas (
                question_id TEXT PRIMARY KEY,
                quota       INTEGER NOT NULL CHECK (quota >= 0),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 每次成功扣减的预留明细（remaining 为本次扣减后的剩余快照）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_reservations (
                task_id     INTEGER NOT NULL,
                question_id TEXT NOT NULL,
                reserved    INTEGER NOT NULL,
                remaining   INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, question_id)
            )
            """
        )
        # 可过期预留租约：一个启用配额的任务至多一条。reservation_token 只存
        # SHA-256（不存明文）；终态 confirmed/expired 互斥且不可逆。扣减成功时
        # pending -> active 并写入 activated_at/expires_at；确认/回收做仅在
        # 当前状态上的条件 UPDATE，配合进程锁杜绝重复释放。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_leases (
                task_id          INTEGER PRIMARY KEY,
                status           TEXT NOT NULL DEFAULT 'pending',
                lease_seconds    INTEGER NOT NULL,
                reservation_token_sha256 TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                activated_at     TEXT,
                expires_at       TEXT,
                confirmed_at     TEXT,
                expired_at       TEXT,
                CHECK (status IN ('pending', 'active', 'confirmed', 'expired'))
            )
            """
        )
        # 防篡改审计链：同一任务 seq 从 1 连续递增、唯一；prev_hash 链向前一
        # 事件哈希（首事件为 64 个 0）；hash 为事件内容（规范化 JSON）的 SHA-256。
        # payload 为规范化 JSON 文本（最小业务载荷），事件只追加、永不更新/删除。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_audit_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                seq        INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload    TEXT NOT NULL,
                prev_hash  TEXT NOT NULL,
                hash       TEXT NOT NULL,
                UNIQUE (task_id, seq)
            )
            """
        )


def create_task(
    name: Optional[str],
    request_json: str,
    seed: int,
    lease: Optional[Tuple[int, str]] = None,
) -> int:
    """创建任务；lease=(lease_seconds, reservation_token) 时同事务登记 pending 租约。

    任务行、created 审计事件与（启用配额任务的）租约登记在同一事务原子提交：
    不存在“任务已建但租约参数丢失”的中间态。租约参数（有效期与令牌哈希）
    同时进入 created 事件载荷，事后篡改 quota_leases 或 tasks.request 都会在
    审计交叉校验中暴露。
    """
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, request, seed) VALUES (?, 'pending', ?, ?)",
            (name, request_json, seed),
        )
        task_id = int(cur.lastrowid)
        # created 事件与任务行在同一事务原子提交；载荷内冻结请求摘要，
        # 事后任何对 tasks.request 的篡改都会在交叉校验中暴露
        created_payload: Dict[str, Any] = {
            "name": name,
            "seed": seed,
            "request_sha256": _sha256_text(request_json),
        }
        if lease is not None:
            lease_seconds, token = lease
            conn.execute(
                "INSERT INTO quota_leases"
                " (task_id, status, lease_seconds, reservation_token_sha256, created_at)"
                " VALUES (?, 'pending', ?, ?, ?)",
                (
                    task_id,
                    int(lease_seconds),
                    _token_hash(token),
                    _now_iso_us(),
                ),
            )
            created_payload["lease_seconds"] = int(lease_seconds)
            created_payload["reservation_token_sha256"] = _token_hash(token)
        append_audit_event(conn, task_id, "created", created_payload)
        return task_id


def update_status(task_id: int, status: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        # running 是任务生命周期唯一的状态流转点（pending -> running）
        append_audit_event(conn, task_id, "started", {"status": status})


def finish_task(task_id: int, result_json: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (result_json, task_id),
        )
        # 启用配额但未走扣减结算（infeasible/error）的任务：租约从未激活、
        # 未持有任何配额，删除其 pending 登记，避免被回收接口误识别
        conn.execute(
            "DELETE FROM quota_leases WHERE task_id = ? AND status = 'pending'",
            (task_id,),
        )
        # feasible / infeasible / error 统一记为 finished，成败以 payload.status 区分；
        # 事件与任务结果落库原子提交，载荷冻结结果摘要
        append_audit_event(
            conn,
            task_id,
            "finished",
            {
                "result_status": json.loads(result_json).get("status"),
                "result_sha256": _sha256_text(result_json),
            },
        )


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


# --------------------------------------------------------------------------- #
# 防篡改审计链：规范化 JSON + SHA-256 哈希链，事件随业务写入在同事务原子追加
# --------------------------------------------------------------------------- #
def _canonical_json(value: Any) -> str:
    """审计规范化 JSON：UTF-8、键升序、无空白、不转义非 ASCII。

    追加事件与校验重算必须走同一序列化口径，任何字符级差异都会使哈希不一致。
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_text(text: str) -> str:
    """对文本的 UTF-8 字节计算 SHA-256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """事件时间：UTC ISO 8601（秒级，Z 结尾）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _now_iso_us() -> str:
    """租约时间：UTC ISO 8601（微秒级，Z 结尾），用于到期判定与状态流转时刻。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_iso_us(value: Optional[str]) -> Optional[datetime]:
    """解析 _now_iso_us / _now_iso 产生的 UTC 时间（None 原样返回）。"""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def generate_reservation_token() -> str:
    """生成随机确认令牌（URL-safe，约 32 字符，192 bit 熵）。"""
    return secrets.token_urlsafe(24)


def _token_hash(token: str) -> str:
    """确认令牌的存储/比对哈希：UTF-8 字节的 SHA-256 十六进制摘要。"""
    return _sha256_text(token)


def append_audit_event(
    conn: sqlite3.Connection, task_id: int, event_type: str, payload: Dict[str, Any]
) -> None:
    """在**调用方已开启的事务/连接**中追加一个审计事件。

    不自建连接、不自开事务：必须与触发它的业务改动（任务创建、状态流转、
    结果落库、配额扣减）在同一个 SQLite 事务中提交或回滚。同任务的 seq 取
    当前最大值 +1（从 1 起），prev_hash 取上一事件哈希（首事件为创世哈希）。
    """
    row = conn.execute(
        "SELECT seq, hash FROM task_audit_events WHERE task_id = ?"
        " ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        seq, prev_hash = 1, GENESIS_HASH
    else:
        seq, prev_hash = int(row["seq"]) + 1, row["hash"]

    created_at = _now_iso()
    payload_json = _canonical_json(payload)
    # 哈希体不含 hash 自身；prev_hash 在体内，形成逐事件咬合的哈希链
    body = _canonical_json(
        {
            "task_id": task_id,
            "seq": seq,
            "type": event_type,
            "created_at": created_at,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    conn.execute(
        "INSERT INTO task_audit_events"
        " (task_id, seq, event_type, created_at, payload, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, seq, event_type, created_at, payload_json, prev_hash,
         _sha256_text(body)),
    )


def get_audit_events(task_id: int) -> List[sqlite3.Row]:
    """按序号读取某任务的全部审计事件（链上原始记录，只追加不改写）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT task_id, seq, event_type, created_at, payload, prev_hash, hash"
            " FROM task_audit_events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()


def _audit_break(
    at_seq: Optional[int], kind: str, expected: Any = None, actual: Any = None
) -> Dict[str, Any]:
    """构造首个断点描述：定位序号 + 异常类别 + 期望/实际值。

    expected/actual 为 None 时不带入响应（避免把“无”误展示成具体值）。
    """
    detail: Dict[str, Any] = {"at_seq": at_seq, "kind": kind}
    if expected is not None:
        detail["expected"] = expected
    if actual is not None:
        detail["actual"] = actual
    return detail


#: 事件合法生命周期顺序（created 固定首位；settlement 必须在 finished 之后，
#: 租约确认/过期必须在结算之后，二者互斥且各自至多一次）。
_EVENT_PHASE = {
    "created": 0,
    "started": 1,
    "finished": 2,
    "quota_settled": 3,
    "quota_confirmed": 4,
    "quota_expired": 4,
}

#: 租约终态事件与租约表终态的对应（两者互斥：一个租约只能确认或过期一次）。
_LEASE_TERMINAL_EVENTS = {"quota_confirmed", "quota_expired"}


def _verify_audit_chain(
    task: sqlite3.Row,
    events: List[sqlite3.Row],
    reservations: List[sqlite3.Row],
    lease: Optional[sqlite3.Row] = None,
) -> Optional[Dict[str, Any]]:
    """在调用方事务的一致快照上重放校验审计链。

    校验顺序（命中即返回，不伪报通过）：
    1. 空链 / 首事件非 created / 序号连续性（seq 必须从 1 起逐条 +1）；
    2. 前驱哈希：首事件须为创世哈希（64 个 0），其余须等于上一事件哈希；
    3. 内容摘要：按规范化 JSON 重算事件体哈希，必须与存储 hash 一致，
       且 payload 必须是合法的规范化 JSON 对象；
    4. 生命周期：created/started/finished 各至多一次且按 created → started →
       finished → quota_settled → quota_confirmed|quota_expired 顺序出现
       （确认/过期互斥、各自至多一次），未知事件类型直接判断点；
    5. 与业务表交叉比对：created 事件的请求摘要/名称/种子（启用配额时还有
       租约有效期与令牌哈希）须与 tasks 行一致，finished 事件的结果摘要/状态
       须与 tasks.result 一致，quota_settled 事件须与 quota_reservations 实际
       预留（或 quota_exceeded 缺额）及 quota_leases 锚点一致，租约终态事件
       须与 quota_leases 实际状态/时刻及释放明细一致；
    6. 状态完备性：running/done 的任务必须存在 started 事件；done 任务必须
       存在 finished 事件；声明 use_quota 的任务必须存在 quota_settled 事件；
       成功扣减的租约状态（active/confirmed/expired）须与链上终态事件相符。
    """
    if not events:
        return _audit_break(None, "empty_chain", expected="created as seq 1")

    try:
        req = json.loads(task["request"])
    except (ValueError, TypeError):
        req = None
    if not isinstance(req, dict):
        req = None

    prev_hash = GENESIS_HASH
    seen_phases: Dict[str, int] = {}
    finished_seen = False
    for index, ev in enumerate(events):
        seq = int(ev["seq"])
        expected_seq = index + 1

        # 1. 序号连续
        if seq != expected_seq:
            return _audit_break(
                seq, "seq_gap", expected=expected_seq, actual=seq
            )

        # 首事件必须是 created
        if index == 0 and ev["event_type"] != "created":
            return _audit_break(
                seq,
                "unexpected_first_event",
                expected="created",
                actual=ev["event_type"],
            )

        # 2. 前驱哈希
        if ev["prev_hash"] != prev_hash:
            return _audit_break(
                seq,
                "prev_hash_mismatch",
                expected=prev_hash,
                actual=ev["prev_hash"],
            )

        # 3a. payload 必须可解析为 JSON 对象
        try:
            payload = json.loads(ev["payload"])
        except (ValueError, TypeError):
            return _audit_break(seq, "payload_not_json", actual=ev["payload"])
        if not isinstance(payload, dict):
            return _audit_break(
                seq, "payload_not_object", actual=type(payload).__name__
            )

        # 3b. 事件内容摘要（与追加时完全同口径重算）
        body = _canonical_json(
            {
                "task_id": int(ev["task_id"]),
                "seq": seq,
                "type": ev["event_type"],
                "created_at": ev["created_at"],
                "payload": payload,
                "prev_hash": ev["prev_hash"],
            }
        )
        actual_hash = _sha256_text(body)
        if actual_hash != ev["hash"]:
            return _audit_break(
                seq, "hash_mismatch", expected=ev["hash"], actual=actual_hash
            )

        # 3c. payload 文本必须就是规范化结果（杜绝“语义相同但字节被改动”）
        if _canonical_json(payload) != ev["payload"]:
            return _audit_break(
                seq, "payload_not_canonical",
                expected=_canonical_json(payload), actual=ev["payload"],
            )

        # 4. 生命周期顺序与唯一性
        etype = ev["event_type"]
        phase = _EVENT_PHASE.get(etype)
        if phase is None:
            return _audit_break(seq, "unknown_event_type", actual=etype)
        if etype == "created":
            if index != 0:
                return _audit_break(
                    seq, "duplicate_event",
                    expected="created only at seq 1", actual=etype,
                )
        else:
            if etype in seen_phases:
                return _audit_break(
                    seq, "duplicate_event",
                    expected=f"single {etype} event",
                    actual=f"also at seq {seen_phases[etype]}",
                )
            # quota_confirmed 与 quota_expired 互斥（同一租约只能有一个终态）
            if etype in _LEASE_TERMINAL_EVENTS:
                other = (_LEASE_TERMINAL_EVENTS - {etype}).pop()
                if other in seen_phases:
                    return _audit_break(
                        seq, "duplicate_event",
                        expected=f"no {etype} after {other} (lease terminal exclusive)",
                        actual=f"{other} at seq {seen_phases[other]}",
                    )
            # started/finished/settlement/lease 终态之前不得缺少更早阶段的事件
            required_predecessors = {
                "started": ["created"],
                "finished": ["created", "started"],
                "quota_settled": ["created", "started", "finished"],
                "quota_confirmed": [
                    "created", "started", "finished", "quota_settled"
                ],
                "quota_expired": [
                    "created", "started", "finished", "quota_settled"
                ],
            }[etype]
            missing = [p for p in required_predecessors if p not in seen_phases]
            if missing:
                return _audit_break(
                    seq, "event_order",
                    expected=f"{etype} after {' -> '.join(required_predecessors)}",
                    actual=f"missing {missing}",
                )
        seen_phases[etype] = seq

        # 5. 与业务表交叉比对
        if etype == "created":
            if payload.get("request_sha256") != _sha256_text(task["request"]):
                return _audit_break(
                    seq, "request_digest_mismatch",
                    expected=payload.get("request_sha256"),
                    actual=_sha256_text(task["request"]),
                )
            if payload.get("seed") != int(task["seed"]):
                return _audit_break(
                    seq, "seed_mismatch",
                    expected=payload.get("seed"), actual=int(task["seed"]),
                )
            if payload.get("name") != task["name"]:
                return _audit_break(
                    seq, "name_mismatch",
                    expected=payload.get("name"), actual=task["name"],
                )
            # 启用配额任务：created 载荷必须锚定租约参数且与冻结请求/租约表一致。
            # 旧库（功能上线前创建的任务：载荷无锚点、请求无租约字段、无租约行）放行；
            # 未成功扣减的任务（infeasible/error/quota_exceeded）pending 租约登记
            # 在结算/完成事务中故意删除，created 锚点只需与冻结请求一致。
            has_anchor = (
                "lease_seconds" in payload
                or "reservation_token_sha256" in payload
            )
            result_status = None
            if task["result"]:
                try:
                    result_status = json.loads(task["result"]).get("status")
                except ValueError:
                    result_status = None
            lease_deleted_expected = result_status in (
                "quota_exceeded", "infeasible", "error"
            )
            legacy = (
                not has_anchor
                and lease is None
                and req.get("lease_seconds") is None
                and req.get("reservation_token") is None
            )
            if req is not None and req.get("use_quota"):
                # quota_exceeded 任务从不扣减：pending 租约登记在结算事务中
                # 故意删除，created 锚点与冻结请求一致即可（无租约行可比对）
                if lease_deleted_expected:
                    if payload.get("lease_seconds") != int(
                        req.get("lease_seconds")
                    ):
                        return _audit_break(
                            seq,
                            "lease_seconds_mismatch",
                            expected=req.get("lease_seconds"),
                            actual=payload.get("lease_seconds"),
                        )
                    token = req.get("reservation_token")
                    if (
                        not isinstance(token, str)
                        or payload.get("reservation_token_sha256")
                        != _token_hash(token)
                    ):
                        return _audit_break(
                            seq,
                            "lease_token_mismatch",
                            expected="sha256 of frozen reservation_token",
                            actual=payload.get("reservation_token_sha256"),
                        )
                elif not legacy:
                    break_detail = _verify_created_lease(payload, req, lease)
                    if break_detail is not None:
                        return _audit_break(seq, **break_detail)
            elif has_anchor:
                return _audit_break(
                    seq, "unexpected_lease_anchor",
                    expected="no lease anchors without use_quota",
                    actual={
                        "lease_seconds": payload.get("lease_seconds"),
                        "reservation_token_sha256":
                            payload.get("reservation_token_sha256"),
                    },
                )
        elif etype == "started":
            if payload.get("status") != "running":
                return _audit_break(
                    seq, "invalid_payload",
                    expected={"status": "running"}, actual=payload,
                )
        elif etype == "finished":
            finished_seen = True
            result_json = task["result"]
            if not result_json:
                return _audit_break(
                    seq, "result_missing",
                    expected="tasks.result populated with finished event",
                )
            if payload.get("result_sha256") != _sha256_text(result_json):
                return _audit_break(
                    seq, "result_digest_mismatch",
                    expected=payload.get("result_sha256"),
                    actual=_sha256_text(result_json),
                )
            try:
                actual_status = json.loads(result_json).get("status")
            except ValueError:
                return _audit_break(seq, "result_not_json", actual=result_json)
            if payload.get("result_status") != actual_status:
                return _audit_break(
                    seq, "result_status_mismatch",
                    expected=payload.get("result_status"), actual=actual_status,
                )
        elif etype == "quota_settled":
            break_detail = _verify_quota_settlement(
                task, payload, reservations, lease
            )
            if break_detail is not None:
                return _audit_break(seq, **break_detail)
        elif etype in ("quota_confirmed", "quota_expired"):
            break_detail = _verify_lease_terminal(
                etype, seq, payload, lease, reservations
            )
            if break_detail is not None:
                return _audit_break(seq, **break_detail)

        prev_hash = ev["hash"]

    # 6. 状态完备性（断点定位到链尾之后，at_seq=null）
    if task["status"] in ("running", "done") and "started" not in seen_phases:
        return _audit_break(
            None, "started_event_missing",
            expected=f"started event for task status {task['status']}",
        )
    if task["status"] == "done" and not finished_seen:
        return _audit_break(
            None, "finished_event_missing",
            expected="finished event for done task",
        )
    if req is None:
        return _audit_break(None, "request_not_json", actual=task["request"])
    # 只有真正进入配额判定的任务（可行扣减 / 余额不足）才必须有结算事件；
    # infeasible/error 的启用配额任务不消耗配额、租约登记已删除，无结算事件。
    settled_outcomes = {"feasible", "quota_exceeded"}
    final_status = None
    if task["result"]:
        try:
            final_status = json.loads(task["result"]).get("status")
        except ValueError:
            final_status = None
    if (
        req.get("use_quota")
        and final_status in settled_outcomes
        and "quota_settled" not in seen_phases
    ):
        return _audit_break(
            None, "quota_settled_event_missing",
            expected=f"quota_settled event for use_quota task with {final_status}",
        )
    if "quota_settled" in seen_phases and not req.get("use_quota"):
        return _audit_break(
            None, "unexpected_quota_settled",
            expected="no quota_settled event without use_quota",
        )
    # 租约表实际终态必须与链上终态事件一致（active/pending 无终态事件）
    if lease is not None:
        lease_status = lease["status"]
        if lease_status == LEASE_CONFIRMED and "quota_confirmed" not in seen_phases:
            return _audit_break(
                None, "quota_confirmed_event_missing",
                expected="quota_confirmed event for confirmed lease",
            )
        if lease_status == LEASE_EXPIRED and "quota_expired" not in seen_phases:
            return _audit_break(
                None, "quota_expired_event_missing",
                expected="quota_expired event for expired lease",
            )
    return None


def get_audit_snapshot(
    task_id: int,
) -> Optional[Tuple[sqlite3.Row, List[sqlite3.Row], Optional[Dict[str, Any]]]]:
    """在**单一只读事务快照**中读取任务行、全部事件、预留明细并完成重放校验。

    任务不存在返回 None；否则返回 (任务行, 按 seq 升序的事件, 首个断点或 None)。
    所有读取共用同一连接的同一事务，校验所见的 tasks / task_audit_events /
    quota_reservations / quota_leases 严格处于同一时刻，杜绝跨连接瞬时不一致
    造成的误报。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            conn.commit()
            return None
        events = conn.execute(
            "SELECT task_id, seq, event_type, created_at, payload, prev_hash, hash"
            " FROM task_audit_events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        reservations = conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY question_id",
            (task_id,),
        ).fetchall()
        lease = conn.execute(
            "SELECT * FROM quota_leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        break_point = _verify_audit_chain(task, events, reservations, lease)
        conn.commit()
        return task, events, break_point
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_audit_chain(
    task_id: int, task: sqlite3.Row, events: List[sqlite3.Row]
) -> Optional[Dict[str, Any]]:
    """便捷校验入口（独立快照读取预留明细与租约）；HTTP 接口请用 get_audit_snapshot。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        reservations = conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY question_id",
            (task_id,),
        ).fetchall()
        lease = conn.execute(
            "SELECT * FROM quota_leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        result = _verify_audit_chain(task, events, reservations, lease)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _verify_created_lease(
    payload: Dict[str, Any],
    req: Dict[str, Any],
    lease: Optional[sqlite3.Row],
) -> Optional[Dict[str, Any]]:
    """交叉校验 created 事件中的租约锚点与冻结请求、quota_leases 行一致。"""
    lease_seconds = payload.get("lease_seconds")
    token_sha = payload.get("reservation_token_sha256")
    if not isinstance(lease_seconds, int) or not isinstance(token_sha, str):
        return {
            "kind": "lease_anchor_missing",
            "expected": {"lease_seconds": req.get("lease_seconds"),
                         "reservation_token_sha256": "sha256 hex digest"},
            "actual": {"lease_seconds": lease_seconds,
                       "reservation_token_sha256": token_sha},
        }
    if lease_seconds != int(req.get("lease_seconds")):
        return {
            "kind": "lease_seconds_mismatch",
            "expected": req.get("lease_seconds"),
            "actual": lease_seconds,
        }
    if lease is None:
        return {
            "kind": "lease_row_missing",
            "expected": "quota_leases row for use_quota task",
            "actual": None,
        }
    if int(lease["lease_seconds"]) != lease_seconds:
        return {
            "kind": "lease_seconds_mismatch",
            "expected": lease_seconds,
            "actual": int(lease["lease_seconds"]),
        }
    if lease["reservation_token_sha256"] != token_sha:
        return {
            "kind": "lease_token_mismatch",
            "expected": token_sha,
            "actual": lease["reservation_token_sha256"],
        }
    # 冻结请求中的令牌明文哈希必须与锚点一致（防止只改 request 或只改租约行）
    token = req.get("reservation_token")
    if not isinstance(token, str) or _token_hash(token) != token_sha:
        return {
            "kind": "lease_token_mismatch",
            "expected": token_sha,
            "actual": _token_hash(token) if isinstance(token, str) else None,
        }
    return None


def _verify_lease_terminal(
    etype: str,
    seq: int,
    payload: Dict[str, Any],
    lease: Optional[sqlite3.Row],
    rows: List[sqlite3.Row],
) -> Optional[Dict[str, Any]]:
    """交叉校验 quota_confirmed / quota_expired 事件与 quota_leases 实际状态。"""
    if lease is None:
        return {
            "kind": "lease_row_missing",
            "expected": f"quota_leases row for {etype}",
            "actual": None,
        }
    token_sha = payload.get("reservation_token_sha256")
    if not isinstance(token_sha, str) or token_sha != lease["reservation_token_sha256"]:
        return {
            "kind": "lease_token_mismatch",
            "expected": lease["reservation_token_sha256"],
            "actual": token_sha,
        }
    expires_at = payload.get("expires_at")
    if expires_at != lease["expires_at"]:
        return {
            "kind": "lease_expires_mismatch",
            "expected": lease["expires_at"],
            "actual": expires_at,
        }

    if etype == "quota_confirmed":
        if lease["status"] != LEASE_CONFIRMED:
            return {
                "kind": "lease_status_mismatch",
                "expected": LEASE_CONFIRMED,
                "actual": lease["status"],
            }
        confirmed_at = payload.get("confirmed_at")
        if confirmed_at != lease["confirmed_at"]:
            return {
                "kind": "lease_timestamp_mismatch",
                "expected": lease["confirmed_at"],
                "actual": confirmed_at,
            }
        return None

    # quota_expired：租约必须为 expired，released_at 与 expired_at 列一致，
    # 且逐题释放明细必须等于实际预留（释放即逐题归还 reserved 次）
    if lease["status"] != LEASE_EXPIRED:
        return {
            "kind": "lease_status_mismatch",
            "expected": LEASE_EXPIRED,
            "actual": lease["status"],
        }
    if payload.get("released_at") != lease["expired_at"]:
        return {
            "kind": "lease_timestamp_mismatch",
            "expected": lease["expired_at"],
            "actual": payload.get("released_at"),
        }
    actual_items = sorted(
        (
            {"question_id": json.loads(r["question_id"]),
             "released": int(r["reserved"])}
            for r in rows
        ),
        key=lambda it: _canonical_json(it["question_id"]),
    )
    expected_items = payload.get("items")
    well_formed = isinstance(expected_items, list) and all(
        isinstance(it, dict) for it in expected_items
    )
    if not well_formed or _canonical_json(
        sorted(expected_items, key=lambda it: _canonical_json(it.get("question_id")))
    ) != _canonical_json(actual_items):
        return {
            "kind": "lease_released_items_mismatch",
            "expected": expected_items,
            "actual": actual_items,
        }
    return None


def _verify_quota_settlement(
    task: sqlite3.Row,
    payload: Dict[str, Any],
    rows: List[sqlite3.Row],
    lease: Optional[sqlite3.Row] = None,
) -> Optional[Dict[str, Any]]:
    """交叉校验单个 quota_settled 事件与同快照内的实际扣减结果及租约锚点。"""
    outcome = payload.get("outcome")
    actual_items = [
        {
            "question_id": json.loads(r["question_id"]),
            "reserved": int(r["reserved"]),
            "remaining": int(r["remaining"]),
        }
        for r in rows
    ]

    result = json.loads(task["result"]) if task["result"] else {}
    if outcome == "reserved":
        # 已扣减：tasks.result 必须仍可行，且逐题预留与事件载荷完全一致
        if result.get("status") != "feasible":
            return {
                "kind": "settlement_outcome_mismatch",
                "expected": "feasible result for reserved settlement",
                "actual": result.get("status"),
            }
        expected_items = payload.get("items")
        well_formed = isinstance(expected_items, list) and all(
            isinstance(it, dict) for it in expected_items
        )
        if (
            not well_formed
            or _canonical_json(sorted(
                expected_items,
                key=lambda it: _canonical_json(it.get("question_id")),
            ))
            != _canonical_json(sorted(
                actual_items,
                key=lambda it: _canonical_json(it.get("question_id")),
            ))
        ):
            return {
                "kind": "reservations_mismatch",
                "expected": expected_items,
                "actual": actual_items,
            }
        # 租约锚点：成功扣减后租约必须已激活（active/confirmed/expired），
        # 有效期/令牌哈希/到期时间与 quota_leases 行一致。
        # 旧库（功能上线前结算、无租约锚点）允许无租约，保持向后兼容。
        lease_seconds = payload.get("lease_seconds")
        token_sha = payload.get("reservation_token_sha256")
        expires_at = payload.get("expires_at")
        has_anchor = (
            lease_seconds is not None
            or token_sha is not None
            or expires_at is not None
        )
        if has_anchor:
            if lease is None:
                return {
                    "kind": "lease_row_missing",
                    "expected": "active quota_leases row for reserved settlement",
                    "actual": None,
                }
            if lease["status"] == LEASE_PENDING:
                return {
                    "kind": "lease_status_mismatch",
                    "expected": "lease activated (active/confirmed/expired)",
                    "actual": LEASE_PENDING,
                }
            if int(lease["lease_seconds"]) != lease_seconds:
                return {
                    "kind": "lease_seconds_mismatch",
                    "expected": int(lease["lease_seconds"]),
                    "actual": lease_seconds,
                }
            if lease["reservation_token_sha256"] != token_sha:
                return {
                    "kind": "lease_token_mismatch",
                    "expected": lease["reservation_token_sha256"],
                    "actual": token_sha,
                }
            if lease["expires_at"] != expires_at or lease["activated_at"] is None:
                return {
                    "kind": "lease_expires_mismatch",
                    "expected": lease["expires_at"],
                    "actual": expires_at,
                }
            # expires_at 必须等于 activated_at + lease_seconds（微秒级 UTC）
            activated = _parse_iso_us(lease["activated_at"])
            expires = _parse_iso_us(expires_at)
            if activated is None or expires is None or expires - activated != timedelta(
                seconds=int(lease_seconds)
            ):
                return {
                    "kind": "lease_expires_mismatch",
                    "expected": f"activated_at + {int(lease['lease_seconds'])}s",
                    "actual": {"activated_at": lease["activated_at"],
                               "expires_at": expires_at},
                }
    elif outcome == "exceeded":
        # 未扣减：不得存在任何预留记录，结果须为 quota_exceeded，缺额明细须一致
        if actual_items:
            return {
                "kind": "reservations_mismatch",
                "expected": [],
                "actual": actual_items,
            }
        if result.get("status") != "quota_exceeded":
            return {
                "kind": "settlement_outcome_mismatch",
                "expected": "quota_exceeded result",
                "actual": result.get("status"),
            }
        if _canonical_json(payload.get("shortages")) != _canonical_json(
            result.get("shortages")
        ):
            return {
                "kind": "shortages_mismatch",
                "expected": payload.get("shortages"),
                "actual": result.get("shortages"),
            }
        # 未扣减则不得残留租约（pending 登记应在结算事务中删除）
        if lease is not None:
            return {
                "kind": "lease_unexpected",
                "expected": "no quota_leases row after exceeded settlement",
                "actual": {"task_id": int(lease["task_id"]),
                           "status": lease["status"]},
            }
    else:
        return {
            "kind": "invalid_payload",
            "expected": "outcome in ('reserved', 'exceeded')",
            "actual": outcome,
        }
    return None


# --------------------------------------------------------------------------- #
# 组卷模板（不可变；版本谱系以 root_id 标识，version 在谱系内单调递增）
# --------------------------------------------------------------------------- #
def create_template(
    name: Optional[str], config_json: str, base: Optional[sqlite3.Row]
) -> Tuple[int, int]:
    """插入一条模板记录，返回 (template_id, version)。

    base 为 None 时创建全新谱系（version=1，root_id 指向自身）；
    否则在 base 所在谱系内取当前最大版本号 +1，base_id 记录派生来源。
    模板记录只插不改，天然满足“创建后不可原地修改”。
    """
    with _lock, get_conn() as conn:
        if base is None:
            cur = conn.execute(
                "INSERT INTO templates (name, config, version, root_id, base_id)"
                " VALUES (?, ?, 1, -1, NULL)",
                (name, config_json),
            )
            template_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE templates SET root_id = ? WHERE id = ?",
                (template_id, template_id),
            )
            return template_id, 1

        root_id = int(base["root_id"])
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM templates WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        cur = conn.execute(
            "INSERT INTO templates (name, config, version, root_id, base_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, config_json, version, root_id, int(base["id"])),
        )
        return int(cur.lastrowid), version


def get_template(template_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()


# --------------------------------------------------------------------------- #
# 题目曝光配额（按题号的可用次数；扣减在单个事务中串行化校验、原子生效）
# --------------------------------------------------------------------------- #
def _qkey(qid: Any) -> str:
    """题号的数据库存储键：JSON 编码，区分整数 1 与字符串 "1"。"""
    return json.dumps(qid, ensure_ascii=False)


def set_quotas(items: List[Tuple[Any, int]]) -> None:
    """按题号设置可用次数（绝对值，存在即覆盖）。"""
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for qid, quota in items:
            conn.execute(
                "INSERT INTO quotas (question_id, quota, updated_at)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(question_id) DO UPDATE SET"
                "   quota = excluded.quota, updated_at = excluded.updated_at",
                (_qkey(qid), int(quota)),
            )


def list_quotas() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT question_id, quota, updated_at FROM quotas ORDER BY rowid"
        ).fetchall()


def get_quota_reservations(task_id: int) -> List[sqlite3.Row]:
    """某任务本次成功扣减的预留明细（按写入顺序，即规范化题号序）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()


def get_quota_lease(task_id: int) -> Optional[sqlite3.Row]:
    """某任务的配额预留租约（不存在返回 None）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM quota_leases WHERE task_id = ?", (task_id,)
        ).fetchone()


def lease_effective_status(row: sqlite3.Row, now: Optional[datetime] = None) -> str:
    """查询口径的租约状态：active 但已过 expires_at 报告为 expired。

    注意这只是**展示用**判定——已过到期时间的活动租约在回收接口执行前仍
    实际持有配额；确认接口据此拒绝确认，释放只可能由回收接口完成。
    """
    status = row["status"]
    if status != LEASE_ACTIVE:
        return status
    expires = _parse_iso_us(row["expires_at"])
    if expires is not None and (now or datetime.now(timezone.utc)) >= expires:
        return LEASE_EXPIRED
    return LEASE_ACTIVE


def finish_task_with_quota(
    task_id: int, result: Dict[str, Any], requirements: List[Tuple[Any, int]]
) -> Dict[str, Any]:
    """启用配额的任务落库：校验 -> 原子扣减 -> 完成任务 -> 追加审计事件。

    扣减、任务结果更新与 finished / quota_settled 两个审计事件全部在**单个
    SQLite 事务**中，提交则同时生效、回滚则同时消失：配额结算事件绝不与扣减
    结果出现半成功状态。

    requirements 为 (题号, 需扣减次数) 列表。进程内互斥锁 + BEGIN IMMEDIATE
    保证并发任务的校验-扣减严格串行：
    - 全部题余额充足：逐题扣减、写入预留记录（remaining 为扣后快照），
      任务以原可行结果完成，结算事件 outcome=reserved 并冻结逐题扣减/剩余快照；
      同时把任务创建时登记的 pending 租约激活为 active（写入 activated_at 与
      expires_at=扣减时刻+lease_seconds），租约锚点（有效期/令牌哈希/到期时间）
      一并冻结进结算事件；
    - 任一题余额不足（无配额记录按 0 计）：整组不扣减，任务结果改写为
      quota_exceeded 并附缺额明细，结算事件 outcome=exceeded 并冻结缺额明细；
      租约从未持有配额，删除其 pending 登记。
    返回最终落库的 result。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        lease_row = conn.execute(
            "SELECT lease_seconds, reservation_token_sha256 FROM quota_leases"
            " WHERE task_id = ? AND status = 'pending'",
            (task_id,),
        ).fetchone()
        keys = [(_qkey(qid), qid, int(need)) for qid, need in requirements]
        rows = (
            conn.execute(
                "SELECT question_id, quota FROM quotas WHERE question_id IN"
                f" ({','.join('?' for _ in keys)})",
                [k for k, _, _ in keys],
            ).fetchall()
            if keys
            else []
        )
        balance = {r["question_id"]: int(r["quota"]) for r in rows}

        shortages = []
        for key, qid, need in keys:
            available = balance.get(key, 0)
            if available < need:
                shortages.append(
                    {
                        "question_id": qid,
                        "required": need,
                        "available": available,
                        "shortage": need - available,
                    }
                )

        if shortages:
            final = {
                "status": "quota_exceeded",
                "message": "题目曝光配额不足，本次组卷未扣减任何配额",
                "shortages": shortages,
            }
            # 未扣减任何配额：删除 pending 租约（不存在可确认/可回收的预留）
            conn.execute(
                "DELETE FROM quota_leases WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
        else:
            settled_items = []
            for key, qid, need in keys:
                remaining = balance[key] - need
                conn.execute(
                    "UPDATE quotas SET quota = ?, updated_at = datetime('now')"
                    " WHERE question_id = ?",
                    (remaining, key),
                )
                conn.execute(
                    "INSERT INTO quota_reservations"
                    " (task_id, question_id, reserved, remaining)"
                    " VALUES (?, ?, ?, ?)",
                    (task_id, key, need, remaining),
                )
                settled_items.append(
                    {"question_id": qid, "reserved": need, "remaining": remaining}
                )
            final = result

        final_json = json.dumps(final, ensure_ascii=False)
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (final_json, task_id),
        )
        # finished：冻结任务最终结果摘要（含 quota_exceeded）
        append_audit_event(
            conn,
            task_id,
            "finished",
            {
                "result_status": final.get("status"),
                "result_sha256": _sha256_text(final_json),
            },
        )
        # quota_settled：与上面的扣减/未扣减结果同一事务原子提交
        if shortages:
            append_audit_event(
                conn,
                task_id,
                "quota_settled",
                {"outcome": "exceeded", "shortages": shortages},
            )
        else:
            # 激活租约：到期时间以实际扣减（预留生效）时刻为起点
            lease_payload: Dict[str, Any] = {}
            if lease_row is not None:
                lease_seconds = int(lease_row["lease_seconds"])
                token_sha = lease_row["reservation_token_sha256"]
                activated_at = _now_iso_us()
                expires_at = (
                    _parse_iso_us(activated_at) + timedelta(seconds=lease_seconds)
                ).isoformat(timespec="microseconds").replace("+00:00", "Z")
                conn.execute(
                    "UPDATE quota_leases SET status = 'active', activated_at = ?,"
                    " expires_at = ? WHERE task_id = ? AND status = 'pending'",
                    (activated_at, expires_at, task_id),
                )
                lease_payload = {
                    "lease_seconds": lease_seconds,
                    "reservation_token_sha256": token_sha,
                    "expires_at": expires_at,
                }
            append_audit_event(
                conn,
                task_id,
                "quota_settled",
                {
                    "outcome": "reserved",
                    "items": settled_items,
                    **lease_payload,
                },
            )
    return final


# --------------------------------------------------------------------------- #
# 预留租约：令牌主动确认（幂等）与过期回收（原子、防重复释放、并发幂等）
# --------------------------------------------------------------------------- #
#: 确认接口结果码：与 HTTP 层的状态映射一一对应。
#: ok（本次确认）/ already_confirmed（重复确认）/ expired（已过期或已回收）/
#: pending（未结算）/ bad_token（令牌不匹配）/ no_lease（无租约）。
ConfirmCode = str


def confirm_quota_lease(task_id: int, token: str) -> ConfirmCode:
    """用 reservation_token 确认预留，返回结果码（与 HTTP 状态一一对应）。

    - ``ok``            ：本次成功确认（active 且未到期）；
    - ``already_confirmed``：此前已确认（重复确认幂等，不二次扣减/释放）；
    - ``expired``       ：租约已过期（已过到期时间或已被回收）；不确认、不释放；
    - ``pending``       ：任务尚未结算、租约未激活（409）；
    - ``bad_token``     ：令牌不匹配（403）；
    - ``no_lease``      ：任务/租约不存在（404，含 quota_exceeded 后租约被删除）。

    状态翻转是带 ``status='active'`` 与 ``expires_at > now`` 双条件的单条
    UPDATE：确认与回收互斥——已被回收的租约 rowcount=0，绝不会在确认路径上
    重复处理配额。翻转、quota_confirmed 审计事件在同一事务原子提交。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM quota_leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return "no_lease"
        if not secrets.compare_digest(
            row["reservation_token_sha256"], _token_hash(token)
        ):
            conn.commit()
            return "bad_token"

        status = row["status"]
        if status == LEASE_PENDING:
            conn.commit()
            return "pending"
        if status == LEASE_CONFIRMED:
            conn.commit()
            return "already_confirmed"
        if status == LEASE_EXPIRED:
            conn.commit()
            return "expired"

        # active：以数据库内到期时刻为准（比较 ISO 文本与 _now_iso_us 同口径）
        now = _now_iso_us()
        if row["expires_at"] is not None and now >= row["expires_at"]:
            conn.commit()
            return "expired"

        cur = conn.execute(
            "UPDATE quota_leases SET status = 'confirmed', confirmed_at = ?"
            " WHERE task_id = ? AND status = 'active' AND expires_at > ?",
            (now, task_id, now),
        )
        if cur.rowcount != 1:
            # 与并发回收竞争失败：租约已被释放，按过期处理（绝不确认、不释放）
            conn.commit()
            return "expired"
        append_audit_event(
            conn,
            task_id,
            "quota_confirmed",
            {
                "reservation_token_sha256": row["reservation_token_sha256"],
                "expires_at": row["expires_at"],
                "confirmed_at": now,
            },
        )
        conn.commit()
        return "ok"


def reclaim_expired_leases() -> Dict[str, Any]:
    """原子回收全部已过期但未确认的预留租约，返回释放汇总。

    在单个 ``BEGIN IMMEDIATE`` 事务中处理，进程锁 + 条件 UPDATE
    （``status='active' AND expires_at <= now``）保证：

    - 每条租约至多被翻转一次——并发回收只有一方 rowcount=1，另一方得到 0，
      同一配额绝不会被释放两次；
    - 与确认互斥：确认路径的条件 UPDATE 要求 expires_at > now，抢在回收前
      成功的租约不满足回收条件，反之亦然；
    - 重复调用幂等：无过期租约时 reclaimed_count/released_total 均为 0。

    释放按逐题预留明细把 reserved 归还到 quotas（UPSERT，兼容配额行已被
    PUT /api/quotas 覆盖/删除的情况），并与归还、quota_expired 审计事件同事务提交。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = _now_iso_us()
        expired_rows = conn.execute(
            "SELECT l.task_id, l.reservation_token_sha256, l.expires_at,"
            " r.question_id, r.reserved"
            " FROM quota_leases l JOIN quota_reservations r ON r.task_id = l.task_id"
            " WHERE l.status = 'active' AND l.expires_at <= ?"
            " ORDER BY l.task_id, r.rowid",
            (now,),
        ).fetchall()

        reclaimed: Dict[int, sqlite3.Row] = {}
        skipped: set[int] = set()
        released_total = 0
        totals: Dict[str, int] = {}
        for r in expired_rows:
            tid = int(r["task_id"])
            key = r["question_id"]
            amount = int(r["reserved"])
            if tid not in reclaimed and tid not in skipped:
                # 条件翻转：只有仍为 active 且确实到期的租约才会生效。
                # 进程锁已串行化本进程写操作，条件 UPDATE 同时防御跨进程/
                # 离线连接并发：翻转失败则该租约的全部明细一律不释放。
                cur = conn.execute(
                    "UPDATE quota_leases SET status = 'expired', expired_at = ?"
                    " WHERE task_id = ? AND status = 'active' AND expires_at <= ?",
                    (now, tid, now),
                )
                if cur.rowcount != 1:
                    # 并发确认/其他回收抢先：标记跳过，不释放该租约任何配额
                    skipped.add(tid)
                    continue
                reclaimed[tid] = r
            if tid in skipped:
                continue
            totals[key] = totals.get(key, 0) + amount
            released_total += amount

        items: List[Dict[str, Any]] = []
        for key in sorted(totals, key=lambda k: _canonical_json(json.loads(k))):
            amount = totals[key]
            # 归还：配额行可能已被 PUT /api/quotas 改写或删除，用 UPSERT 兜底
            conn.execute(
                "INSERT INTO quotas (question_id, quota, updated_at)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(question_id) DO UPDATE SET"
                "   quota = quotas.quota + excluded.quota,"
                "   updated_at = datetime('now')",
                (key, amount),
            )
            after = conn.execute(
                "SELECT quota FROM quotas WHERE question_id = ?", (key,)
            ).fetchone()
            items.append(
                {
                    "question_id": json.loads(key),
                    "released": amount,
                    "quota_after": int(after["quota"]),
                }
            )

        # 每条成功翻转的租约同事务追加过期事件（含逐题释放明细，供交叉校验）
        for tid, r in reclaimed.items():
            release_items = [
                {
                    "question_id": json.loads(x["question_id"]),
                    "released": int(x["reserved"]),
                }
                for x in expired_rows
                if int(x["task_id"]) == tid
            ]
            append_audit_event(
                conn,
                tid,
                "quota_expired",
                {
                    "reservation_token_sha256": r["reservation_token_sha256"],
                    "expires_at": r["expires_at"],
                    "released_at": now,
                    "items": release_items,
                },
            )
        conn.commit()

    return {
        "reclaimed_count": len(reclaimed),
        "released_total": released_total,
        "items": items,
    }
