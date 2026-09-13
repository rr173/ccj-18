"""数据层：SQLite (WAL)。

核心设计
--------
1. ``tickets`` 是票的当前状态（每个票面编号一行，票之间互不顶替）。
2. ``events`` 是只追加（append-only）的状态变化流水，``id`` 由 SQLite
   AUTOINCREMENT 单调分配，作为门点离线补齐用的全局版本号。
3. 状态只允许单向迁移：ACTIVE -> REDEEMED / REVOKED / EXPIRED，
   终态不可回退，因此撤销、过期、服务器重启都不可能让票“复活”。
4. 核销走 ``BEGIN IMMEDIATE`` 串行写事务 + 条件 UPDATE
   (``WHERE status='ACTIVE'``)，并发扫同一张票时只有一个门点成功。
5. ``scan_attempts`` 对 (gate_id, attempt_id) 建唯一约束，门点网络抖动
   重发同一次扫码时按幂等处理，返回第一次的核销结果。
6. ``zones`` 是分区注册表；``gates.zone_id`` 指定门点所属分区，
   ``tickets.zones`` (JSON 数组) 指定票允许通行的分区。
7. ``policy_rules`` 是封锁/解除封锁规则表，``rule_id`` 主键去重
   （重复发布同一规则不重复生效）；每次发布同时写一条
   ``POLICY_LOCK`` / ``POLICY_UNLOCK`` 事件，版本号即 ``events.id``，
   门点离线重连后与票据事件一起按版本补齐。
8. 访客预约批次：``batches``（日期/时段/分区/容量/一次性申请令牌）、
   ``applications``（申请、审核状态与候补序号）、``batch_capacity_log``
   （容量变化 append-only）。申请、审核、候补晋级、补录、改容量都在
   IMMEDIATE 事务内串行完成，保证并发申请不超容量、取消即按候补顺序释放。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

DB_PATH = os.getenv("PASSPORT_DB", os.path.join(os.getcwd(), "passport.db"))

TICKET_STATUSES = ("ACTIVE", "REDEEMED", "REVOKED", "EXPIRED")
EVENT_TYPES = (
    "TICKET_ISSUED",
    "TICKET_REDEEMED",
    "TICKET_REVOKED",
    "TICKET_EXPIRED",
    "POLICY_LOCK",
    "POLICY_UNLOCK",
    # 访客预约批次
    "BATCH_CREATED",
    "BATCH_CLOSED",
    "BATCH_CAPACITY_CHANGED",
    "APPLICATION_SUBMITTED",
    "APPLICATION_PROMOTED",
    "APPLICATION_APPROVED",
    "APPLICATION_CANCELLED",
    "APPLICATION_REJECTED",
    "APPLICATION_EXPIRED",
    # 访客申请变更（修改姓名/联系方式/同行人数与同行人名单）
    "APPLICATION_CHANGE_SUBMITTED",
    "APPLICATION_CHANGE_APPROVED",
    "APPLICATION_CHANGE_REJECTED",
    "APPLICATION_CHANGE_CANCELLED",
    "APPLICATION_CHANGE_EXPIRED",
    # 访客在场清册：到场（门点核销成功/管理员补登记）/ 离场（门点确认）/ 人工更正
    "PRESENCE_ARRIVED",
    "PRESENCE_DEPARTED",
    "PRESENCE_CORRECTED",
    # 应急清点：管理员发起一次快照（快照本体落 rollcall* 表，事件进版本流）
    "ROLLCALL_TAKEN",
    # 访客路线：版本编排 / 暂停恢复 / 绑定 / 检查点开闭
    "ROUTE_CREATED",
    "ROUTE_VERSION_PUBLISHED",
    "ROUTE_PAUSED",
    "ROUTE_RESUMED",
    "ROUTE_BOUND",
    "ROUTE_CHECKPOINT_CLOSED",
    "ROUTE_CHECKPOINT_OPENED",
    # 访客路线：执行（首次核销开始 / 逐点推进 / 完成 / 违规 / 离线冲突）
    "ROUTE_STARTED",
    "ROUTE_CHECKPOINT",
    "ROUTE_COMPLETED",
    "ROUTE_VIOLATED",
    "ROUTE_CONFLICT",
)
RULE_ACTIONS = ("LOCK", "UNLOCK")

# 变更请求状态机：
#   PENDING（访客已提交，待管理员审核）
#   -> APPROVED（审核通过：申请资料被改写；已通过申请同事务换票）
#   / REJECTED（管理员拒绝，申请与旧票均不变）
#   / CANCELLED（访客自行撤回，或申请本身被取消/拒绝）
#   / EXPIRED（批次结束仍未审核）
# 后四个均为终态，不产生任何可用票（APPROVED 除外，且其票为原子换发）。
CHANGE_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED", "EXPIRED")

# 申请状态机：
#   PENDING（在容量内，待审核）/ WAITLISTED（满额候补，按 seq 排序）
#   -> APPROVED（已审核，自动签发票）/ CANCELLED / REJECTED / EXPIRED
# 后四个均为终态，无任何路径回退。
APPLICATION_STATUSES = (
    "PENDING",
    "WAITLISTED",
    "APPROVED",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
)
# 占名额的状态（待审核占座 + 已审核占座）
CAPACITY_HOLDING_STATUSES = ("PENDING", "APPROVED")

# ---------------- 访客路线检查与区域停留监控 ----------------
#
# 路线族（routes）：管理员按分区编排，可发多个不可变版本（route_versions），
# 每个版本是一串带顺序的检查点（route_checkpoints，一个检查点绑定一个门点，
# 带该点最长停留秒数与运行时开/闭状态）。ACTIVE 版本供“未来使用”，可暂停或
# 发新版本替换；已开始的路线执行（route_progress）把当时版本号固化，继续走旧
# 版本，编排变更不影响在途访客。
#
# 路线执行状态机（route_progress.status，终态不可逆）：
#   IN_PROGRESS（首次核销在入口检查点开始）
#     -> COMPLETED  （按顺序通过最后一个检查点）
#     -> VIOLATED   （跳过 / 进入已关闭检查点 / 停留超时；终态，任何旧事件都
#                    不能把它改回 IN_PROGRESS）
#   重复进入同一检查点不是终态违规：明确拒绝、路线不推进，但保留门点记录。
# 每次门点判定（成功/拒绝）都向 route_checks 追加一行（票、人员、路线版本、
# 检查点顺序、门点、attempt 幂等键、事件版本），并发下全部在 IMMEDIATE 事务
# 里串行，只有一个明确先后。门点离线期间攒下的检查点事件重连批量补齐，无法
# 自动判定的（缺口/迟到/终态后到达/时间戳异常）落 route_event_conflicts，
# 保留冲突记录等管理员处理，绝不静默丢弃也不擅自推进。
ROUTE_STATUSES = ("ACTIVE", "PAUSED")
ROUTE_PROGRESS_STATUSES = ("IN_PROGRESS", "COMPLETED", "VIOLATED")

# 违规类型（violation_kind）
VIOLATION_SKIPPED = "SKIPPED_CHECKPOINT"     # 跳过未到的检查点（在线明确跳点）
VIOLATION_CLOSED = "CHECKPOINT_CLOSED"       # 进入已关闭检查点
VIOLATION_DWELL = "DWELL_TIMEOUT"            # 在上一检查点停留超过最长时间
# 拒绝但非终态（route_checks.decision=REJECTED，重复进入）
REJECT_DUPLICATE = "DUPLICATE_ENTRY"         # 重复进入当前检查点

# 离线补齐冲突类型（route_event_conflicts.kind，全部需管理员处理）
CONFLICT_GAP = "GAP_PENDING"                 # 中间有检查点事件缺失
CONFLICT_LATE = "LATE_EVENT"                 # 已越过该点后旧事件才到达
CONFLICT_TERMINAL = "ALREADY_TERMINAL"       # 路线已完成/违规后旧事件到达
CONFLICT_INVALID_TS = "INVALID_TIMESTAMP"    # 事件时间戳异常（未来时间/早于路线开始）
CONFLICT_NO_ROUTE = "NO_ROUTE"               # 票未绑定路线 / 路线未开始
CONFLICT_TICKET = "TICKET_STATE"             # 票面不存在或已作废/过期

CONFLICT_KINDS = (
    CONFLICT_GAP, CONFLICT_LATE, CONFLICT_TERMINAL,
    CONFLICT_INVALID_TS, CONFLICT_NO_ROUTE, CONFLICT_TICKET,
)
# 冲突处理动作
CONFLICT_RESOLUTIONS = ("DISMISSED", "APPLIED", "MARK_VIOLATED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    zone_id    TEXT               -- NULL = 未配置分区策略，核销默认拒绝
);

CREATE TABLE IF NOT EXISTS tickets (
    code           TEXT PRIMARY KEY,
    person_id      TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_until    TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('ACTIVE','REDEEMED','REVOKED','EXPIRED')),
    redeemed_at    TEXT,
    redeemed_gate  TEXT,
    revoked_at     TEXT,
    revoked_reason TEXT,
    expired_at     TEXT,
    note           TEXT,
    zones          TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：允许通行的分区
    batch_id       TEXT,            -- 预约批次签发的票：所属批次
    application_id TEXT,            -- 预约批次签发的票：对应申请
    party_size     INTEGER,         -- 预约批次签发的票：同行总人数（含申请人）
    replaced_code  TEXT,            -- 变更换发的新票：原子替换掉的旧票号
    replaced_by_code TEXT,          -- 变更撤销的旧票：原子换发出来的新票号
    replacement_reason TEXT,        -- 作为旧票被撤销时的撤销原因（变更说明）
    route_id       TEXT,            -- 绑定的检查路线（绑定时固化）
    route_version  INTEGER          -- 绑定/开始时使用的路线版本（在途不随编排变）
);
CREATE INDEX IF NOT EXISTS idx_tickets_person ON tickets(person_id);

-- 只追加事件流水：id 即全局版本号（AUTOINCREMENT 保证单调、不复用）
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    type           TEXT NOT NULL
                     CHECK (type IN (
                       'TICKET_ISSUED','TICKET_REDEEMED','TICKET_REVOKED','TICKET_EXPIRED',
                       'POLICY_LOCK','POLICY_UNLOCK',
                       'BATCH_CREATED','BATCH_CLOSED','BATCH_CAPACITY_CHANGED',
                       'APPLICATION_SUBMITTED','APPLICATION_PROMOTED','APPLICATION_APPROVED',
                       'APPLICATION_CANCELLED','APPLICATION_REJECTED','APPLICATION_EXPIRED',
                       'APPLICATION_CHANGE_SUBMITTED','APPLICATION_CHANGE_APPROVED',
                       'APPLICATION_CHANGE_REJECTED','APPLICATION_CHANGE_CANCELLED',
                       'APPLICATION_CHANGE_EXPIRED',
                       'PRESENCE_ARRIVED','PRESENCE_DEPARTED','PRESENCE_CORRECTED',
                       'ROLLCALL_TAKEN',
                       'ROUTE_CREATED','ROUTE_VERSION_PUBLISHED','ROUTE_PAUSED',
                       'ROUTE_RESUMED','ROUTE_BOUND',
                       'ROUTE_CHECKPOINT_CLOSED','ROUTE_CHECKPOINT_OPENED',
                       'ROUTE_STARTED','ROUTE_CHECKPOINT','ROUTE_COMPLETED',
                       'ROUTE_VIOLATED','ROUTE_CONFLICT')),
    ticket_code    TEXT,
    person_id      TEXT,
    gate_id        TEXT,
    reason         TEXT,
    payload        TEXT NOT NULL,
    batch_id       TEXT,
    application_id TEXT,
    rollcall_id    TEXT,              -- ROLLCALL_TAKEN 事件：对应快照
    route_id       TEXT,              -- 路线事件：路线族 ID
    route_version  INTEGER,           -- 路线事件：当时路线版本号
    checkpoint_seq INTEGER,           -- 检查点事件：检查点顺序（1 起）
    progress_id    TEXT               -- 路线执行事件：route_progress.id
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_code);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id);
CREATE INDEX IF NOT EXISTS idx_events_route ON events(route_id);

-- 封锁/解除封锁规则：rule_id 幂等去重；version 对应该规则的事件版本
CREATE TABLE IF NOT EXISTS policy_rules (
    rule_id    TEXT PRIMARY KEY,
    action     TEXT NOT NULL CHECK (action IN ('LOCK','UNLOCK')),
    zone_id    TEXT,                -- NULL = 全局规则，作用于所有分区
    reason     TEXT,
    created_at TEXT NOT NULL,
    version    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_zone ON policy_rules(zone_id);

-- 门点每次扫码尝试（含失败），用于幂等重放与门点记录
CREATE TABLE IF NOT EXISTS scan_attempts (
    gate_id     TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    code        TEXT NOT NULL,
    at          TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    status      TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (gate_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_gate ON scan_attempts(gate_id);
CREATE INDEX IF NOT EXISTS idx_attempts_code ON scan_attempts(code);

-- 门点同步水位（客户端也自带游标，这里仅作管理侧观测）
CREATE TABLE IF NOT EXISTS gate_cursors (
    gate_id      TEXT PRIMARY KEY,
    last_version INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

-- 访客预约批次
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,           -- B-XXXXXXXX
    name        TEXT,                        -- 批次名称（可选）
    visit_date  TEXT NOT NULL,               -- YYYY-MM-DD（访问日期）
    start_at    TEXT NOT NULL,               -- 时段开始 ISO8601
    end_at      TEXT NOT NULL,               -- 时段结束 ISO8601
    zone_id     TEXT NOT NULL,               -- 所属分区
    capacity    INTEGER NOT NULL CHECK (capacity > 0),  -- 人数上限（含同行人）
    apply_token TEXT NOT NULL UNIQUE,        -- 一次性申请链接的秘密令牌
    created_at  TEXT NOT NULL,
    closed_at   TEXT,                         -- 管理员提前关闭申请；NULL=开放
    route_id    TEXT,                         -- 绑定路线（审核签票时随票固化版本）
    route_version INTEGER                     -- 绑定时的路线版本（仅审计展示）
);
CREATE INDEX IF NOT EXISTS idx_batches_date ON batches(visit_date);

-- 访客申请：同一批次 seq 即提交顺序（候补队列按它排序）
CREATE TABLE IF NOT EXISTS applications (
    id            TEXT PRIMARY KEY,          -- A-XXXXXXXX
    batch_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,          -- 批次内提交序号，事务内分配
    name          TEXT NOT NULL,
    contact       TEXT NOT NULL,
    party_size    INTEGER NOT NULL CHECK (party_size >= 1),  -- 总人数（1+同行人数）
    companions    INTEGER NOT NULL DEFAULT 0,-- 同行人数（不含申请人）
    status        TEXT NOT NULL
                  CHECK (status IN ('PENDING','WAITLISTED','APPROVED',
                                    'CANCELLED','REJECTED','EXPIRED')),
    source        TEXT NOT NULL DEFAULT 'visitor'
                  CHECK (source IN ('visitor','admin')),
    request_id    TEXT UNIQUE,               -- 访客请求幂等键（浏览器生成的 UUID）
    manage_token  TEXT UNIQUE,               -- 访客查询自身申请状态的一次性令牌
    created_at    TEXT NOT NULL,
    promoted_at   TEXT,                      -- 候补晋级（WAITLISTED->PENDING）时间
    decided_at    TEXT,                      -- 审核/取消/拒绝/过期终态时间
    decide_reason TEXT,
    ticket_code   TEXT UNIQUE,               -- 审核通过后自动签发的票
    change_version INTEGER NOT NULL DEFAULT 0,  -- 已审核通过的变更次数（当前申请资料版本）
    companion_names TEXT NOT NULL DEFAULT '[]'  -- JSON 数组：同行人姓名名单（长度=companions）
);
CREATE INDEX IF NOT EXISTS idx_apps_batch ON applications(batch_id);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_apps_ticket ON applications(ticket_code);

-- 容量变化记录（append-only）：建批与每次调整各一条
CREATE TABLE IF NOT EXISTS batch_capacity_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,
    old_capacity INTEGER,                    -- NULL = 建批
    new_capacity INTEGER NOT NULL,
    reason       TEXT,
    changed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_caplog_batch ON batch_capacity_log(batch_id);

-- 访客申请变更请求：访客凭申请的 manage_token 发起，管理员审核
CREATE TABLE IF NOT EXISTS application_changes (
    id            TEXT PRIMARY KEY,          -- C-XXXXXXXX
    application_id TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    change_seq    INTEGER NOT NULL,          -- 该申请内变更序号（事务内分配）
    request_id    TEXT NOT NULL UNIQUE,      -- 变更请求幂等键（浏览器生成的 UUID）
    status        TEXT NOT NULL
                  CHECK (status IN ('PENDING','APPROVED','REJECTED',
                                    'CANCELLED','EXPIRED')),
    old_name      TEXT NOT NULL,
    new_name      TEXT NOT NULL,
    old_contact   TEXT NOT NULL,
    new_contact   TEXT NOT NULL,
    old_party_size INTEGER NOT NULL,
    new_party_size INTEGER NOT NULL,
    new_companions INTEGER NOT NULL,
    new_companion_names TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：变更后的同行人名单
    decided_at    TEXT,
    decide_reason TEXT,
    old_ticket_code TEXT,                     -- 通过时被原子撤销的旧票
    new_ticket_code TEXT,                     -- 通过时原子签发的新票
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_app ON application_changes(application_id);
CREATE INDEX IF NOT EXISTS idx_changes_batch ON application_changes(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_changes_status ON application_changes(status);

-- 访客在场清册：每张票至多一行（核销成功即登记到场）。
--   状态机 ARRIVED -> DEPARTED（门点确认离场）
--               \-> REMOVED （管理员误扫更正：从在场名单移除）
--   DEPARTED/REMOVED 下门点自动路径不可再写；管理员可凭原因做人工更正，
--   更正只追加 presence_events 轨迹，不抹掉任何历史。
-- 同行名单/人数/分区/批次在到场瞬间冻结：之后申请变更换发的是新票（旧票
-- 已核销根本不能变更），快照与轨迹都不会被后续变化改写。
CREATE TABLE IF NOT EXISTS presence (
    ticket_code     TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL,
    application_id  TEXT,
    batch_id        TEXT,
    zone_id         TEXT,
    status          TEXT NOT NULL
                    CHECK (status IN ('ARRIVED','DEPARTED','REMOVED')),
    party_size      INTEGER NOT NULL,     -- 到场冻结：含申请人的整组人数
    companions      INTEGER NOT NULL DEFAULT 0,
    companion_names TEXT NOT NULL DEFAULT '[]',  -- 到场冻结的同行人名单 JSON
    applicant_name  TEXT,
    arrived_at      TEXT NOT NULL,
    arrived_gate    TEXT,                 -- 到场门点（人工补登记为 NULL）
    arrived_version INTEGER NOT NULL,     -- 到场事件全局版本
    arrived_event_id INTEGER,             -- 对应 presence_events.id
    departed_at     TEXT,
    departed_gate   TEXT,
    departed_version INTEGER,
    departed_event_id INTEGER,
    removed_at      TEXT,                 -- 最近一次人工更正时间
    removed_reason  TEXT,
    removed_by      TEXT,                 -- 操作者（管理员令牌身份/门点）
    corrected_seq   INTEGER NOT NULL DEFAULT 0  -- 最近一次人工更正序号
);
CREATE INDEX IF NOT EXISTS idx_presence_status ON presence(status);
CREATE INDEX IF NOT EXISTS idx_presence_batch ON presence(batch_id);
CREATE INDEX IF NOT EXISTS idx_presence_zone ON presence(zone_id);
CREATE INDEX IF NOT EXISTS idx_presence_person ON presence(person_id);
CREATE INDEX IF NOT EXISTS idx_presence_app ON presence(application_id);

-- 在场轨迹（append-only）：到场 / 离场 / 人工更正，按票内 seq 形成唯一明确顺序；
-- version 同时写入全局 events.id，门点离线重连按版本补齐。
CREATE TABLE IF NOT EXISTS presence_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_code TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL,         -- 该票内单调序号（1=到场）
    kind        TEXT NOT NULL
                CHECK (kind IN ('ARRIVED','DEPARTED',
                                'MARK_ARRIVED','MARK_DEPARTED',
                                'REMOVE','RESTORE')),
    from_status TEXT,                     -- 变更前 presence.status
    to_status   TEXT NOT NULL,            -- 变更后 presence.status
    reason      TEXT,                     -- 门点动作或人工更正原因
    operator    TEXT NOT NULL,            -- 操作者：gate:<id> / admin / admin:<id>
    gate_id     TEXT,
    version     INTEGER,                  -- 对应全局 events.id（门点补齐游标）
    attempt_id  TEXT,                     -- 门点动作的幂等键
    ts          TEXT NOT NULL,
    UNIQUE (ticket_code, seq)
);
CREATE INDEX IF NOT EXISTS idx_presence_events_ticket ON presence_events(ticket_code, seq);
CREATE INDEX IF NOT EXISTS idx_presence_events_version ON presence_events(version);
-- 门点离场扫码幂等：同一次物理扫码（gate_id, attempt_id）只离场一次。
-- 只约束门点动作（MARK_* 人工更正没有 attempt_id），到场动作在 scan_attempts
-- 唯一约束上已经幂等（核销一次即 REDEEMED），这里额外兜底。
CREATE UNIQUE INDEX IF NOT EXISTS idx_presence_gate_attempt
    ON presence_events(gate_id, attempt_id)
    WHERE attempt_id IS NOT NULL AND kind IN ('ARRIVED','DEPARTED');

-- 应急清点快照：发起瞬间固定。行只插入、永不更新/删除（无任何改写 API）。
CREATE TABLE IF NOT EXISTS rollcalls (
    id          TEXT PRIMARY KEY,         -- R-XXXXXXXX
    reason      TEXT,
    zone_id     TEXT,                     -- 可选：只清点某分区
    batch_id    TEXT,                     -- 可选：只清点某批次
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,            -- 操作者
    version     INTEGER NOT NULL,         -- ROLLCALL_TAKEN 全局版本
    headcount   INTEGER NOT NULL,         -- 在场总人数（含同行人，冻结值）
    groups      INTEGER NOT NULL,         -- 在场组数（申请人数，冻结值）
    scope_json  TEXT NOT NULL DEFAULT '{}'  -- 发起时的范围说明（分区/批次名等）
);
CREATE INDEX IF NOT EXISTS idx_rollcalls_created ON rollcalls(created_at);

-- 清点条目：发起瞬间所有在场（ARRIVED）组逐行冻结
CREATE TABLE IF NOT EXISTS rollcall_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rollcall_id     TEXT NOT NULL,
    ticket_code     TEXT NOT NULL,
    person_id       TEXT NOT NULL,
    application_id  TEXT,
    batch_id        TEXT,
    zone_id         TEXT,
    applicant_name  TEXT,
    party_size      INTEGER NOT NULL,
    companions      INTEGER NOT NULL,
    companion_names TEXT NOT NULL DEFAULT '[]',
    arrived_at      TEXT NOT NULL,
    arrived_gate    TEXT,
    last_gate       TEXT,                 -- 最后门点记录（到场门点或最近离场/更正门点）
    last_event_kind TEXT NOT NULL,        -- 最后一条在场轨迹类型
    last_event_ts   TEXT NOT NULL,
    last_event_version INTEGER NOT NULL,
    presence_seq    INTEGER NOT NULL,     -- 发起时该票轨迹长度
    UNIQUE (rollcall_id, ticket_code)
);
CREATE INDEX IF NOT EXISTS idx_rollcall_entries_rc ON rollcall_entries(rollcall_id);
CREATE INDEX IF NOT EXISTS idx_rollcall_entries_batch ON rollcall_entries(batch_id);
CREATE INDEX IF NOT EXISTS idx_rollcall_entries_zone ON rollcall_entries(zone_id);
CREATE INDEX IF NOT EXISTS idx_rollcall_entries_person ON rollcall_entries(person_id);

-- ---------------- 访客路线检查与区域停留监控 ----------------

-- 路线族：按分区编排；当前 ACTIVE 版本供未来签发/绑定使用，可暂停或替换
CREATE TABLE IF NOT EXISTS routes (
    id           TEXT PRIMARY KEY,          -- RT-XXXXXXXX
    zone_id      TEXT NOT NULL,             -- 路线所属分区（检查点门点须在本分区）
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ACTIVE'
                 CHECK (status IN ('ACTIVE','PAUSED')),
    current_version INTEGER NOT NULL DEFAULT 0,  -- 当前版本号（0=尚未发布任何版本）
    paused_at    TEXT,
    paused_reason TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_routes_zone ON routes(zone_id);

-- 路线版本：内容不可变（检查点顺序/门点/最长停留秒数发布后固定）。
-- 在途执行把版本号固化进 route_progress，发新版本/暂停都不影响旧版本执行。
CREATE TABLE IF NOT EXISTS route_versions (
    route_id     TEXT NOT NULL,
    version      INTEGER NOT NULL,         -- 路线族内单调递增，从 1 开始
    created_at   TEXT NOT NULL,
    created_by   TEXT,
    note         TEXT,
    event_version INTEGER NOT NULL,        -- ROUTE_VERSION_PUBLISHED 的全局事件版本
    PRIMARY KEY (route_id, version)
);

-- 检查点：一个版本内按 seq 排序；每个检查点绑定一个门点，max_stay_seconds
-- 为该点最长停留时间（NULL=不限）。closed 是运行时开/闭（管理员可随时开闭，
-- 产生 ROUTE_CHECKPOINT_CLOSED/OPENED 事件）；在途访客走的是版本固化的检查点
-- 行，因此对旧版本关闭检查点同样对旧版本执行生效（“进入已关闭检查点”）。
CREATE TABLE IF NOT EXISTS route_checkpoints (
    route_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    seq           INTEGER NOT NULL,        -- 顺序，从 1 开始
    gate_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    max_stay_seconds INTEGER,               -- 最长停留秒数；NULL=不限
    closed        INTEGER NOT NULL DEFAULT 0,
    closed_at     TEXT,
    closed_reason TEXT,
    PRIMARY KEY (route_id, version, seq),
    UNIQUE (route_id, version, gate_id)     -- 同一版本内一个门点只能出现一次
);
CREATE INDEX IF NOT EXISTS idx_rchk_gate ON route_checkpoints(gate_id);

-- 路线绑定审计：票或批次绑定到某条路线（绑定时固化当前版本号）。append-only。
CREATE TABLE IF NOT EXISTS route_bindings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id     TEXT NOT NULL,
    route_version INTEGER NOT NULL,
    scope        TEXT NOT NULL CHECK (scope IN ('TICKET','BATCH')),
    ticket_code  TEXT,
    batch_id     TEXT,
    bound_at     TEXT NOT NULL,
    bound_by     TEXT,
    reason       TEXT,
    event_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rbind_ticket ON route_bindings(ticket_code);
CREATE INDEX IF NOT EXISTS idx_rbind_batch ON route_bindings(batch_id);

-- 路线执行：每张绑定路线的票至多一行，首次核销（入口检查点）时创建。
-- 路线版本在此固化；状态机 IN_PROGRESS -> COMPLETED/VIOLATED（终态不可逆，
-- 完成/违规后任何迟到的旧事件都不可能改回 IN_PROGRESS）。
CREATE TABLE IF NOT EXISTS route_progress (
    id             TEXT PRIMARY KEY,        -- RP-XXXXXXXX
    ticket_code    TEXT NOT NULL UNIQUE,
    person_id      TEXT NOT NULL,
    route_id       TEXT NOT NULL,
    route_version  INTEGER NOT NULL,        -- 开始时固化的版本
    application_id TEXT,
    batch_id       TEXT,
    zone_id        TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('IN_PROGRESS','COMPLETED','VIOLATED')),
    next_seq       INTEGER NOT NULL DEFAULT 1,   -- 下一个应到的检查点顺序
    current_seq    INTEGER,                     -- 当前所在检查点（NULL=尚未进入）
    started_at     TEXT,
    started_gate   TEXT,
    started_version INTEGER,                   -- ROUTE_STARTED 全局事件版本
    entered_at     TEXT,                        -- 进入当前检查点的时间（停留计时）
    last_seq       INTEGER,                     -- 最近一次进入的检查点顺序
    last_gate      TEXT,
    last_at        TEXT,
    completed_at   TEXT,
    completed_version INTEGER,
    violated_at    TEXT,
    violated_version INTEGER,
    violation_kind TEXT,
    violation_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_rprog_status ON route_progress(status);
CREATE INDEX IF NOT EXISTS idx_rprog_route ON route_progress(route_id, status);
CREATE INDEX IF NOT EXISTS idx_rprog_batch ON route_progress(batch_id);
CREATE INDEX IF NOT EXISTS idx_rprog_zone ON route_progress(zone_id);
CREATE INDEX IF NOT EXISTS idx_rprog_person ON route_progress(person_id);

-- 门点检查记录：每次路线相关门点判定（含拒绝、离线补齐、冲突）都追加一行。
-- (gate_id, attempt_id) 唯一：同一次物理上报因网络抖动重发绝不重复推进。
CREATE TABLE IF NOT EXISTS route_checks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id        TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    ticket_code    TEXT NOT NULL,
    person_id      TEXT,
    route_id       TEXT,
    route_version  INTEGER,
    checkpoint_seq INTEGER,
    at             TEXT NOT NULL,           -- 服务器处理时间
    event_ts       TEXT,                    -- 门点事件时间（离线补齐时为当时）
    decision       TEXT NOT NULL            -- ADVANCED/REJECTED/STARTED/COMPLETED/
                   CHECK (decision IN ('STARTED','ADVANCED','COMPLETED','REJECTED',
                                       'VIOLATED','CONFLICT')),
    result         TEXT NOT NULL,           -- ok / reason（duplicate/closed/...）
    reason         TEXT,
    event_version  INTEGER,                 -- 对应全局事件版本（拒绝无事件时 NULL）
    conflict_id    INTEGER,                 -- CONFLICT 时对应 route_event_conflicts.id
    replayed       INTEGER NOT NULL DEFAULT 0,
    offline        INTEGER NOT NULL DEFAULT 0,
    http_status    INTEGER NOT NULL,
    result_json    TEXT NOT NULL,
    UNIQUE (gate_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_rchecks_code ON route_checks(ticket_code);
CREATE INDEX IF NOT EXISTS idx_rchecks_route ON route_checks(route_id);
CREATE INDEX IF NOT EXISTS idx_rchecks_gate ON route_checks(gate_id);

-- 离线补齐冲突：无法自动判定的离线检查点事件保留在此，等管理员处理。
-- 只插入/追加处理结论，从不删除（处理后 status 置 RESOLVED 并留痕）。
CREATE TABLE IF NOT EXISTS route_event_conflicts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id        TEXT NOT NULL,
    attempt_id     TEXT NOT NULL,
    ticket_code    TEXT NOT NULL,
    person_id      TEXT,
    route_id       TEXT,
    route_version  INTEGER,
    checkpoint_seq INTEGER,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('GAP_PENDING','LATE_EVENT','ALREADY_TERMINAL',
                                   'INVALID_TIMESTAMP','NO_ROUTE','TICKET_STATE')),
    event_ts       TEXT,                    -- 门点上报的事件时间
    detected_at    TEXT NOT NULL,           -- 服务器发现冲突的时间
    detail         TEXT NOT NULL,           -- JSON：冲突现场快照
    status         TEXT NOT NULL DEFAULT 'OPEN'
                   CHECK (status IN ('OPEN','RESOLVED')),
    resolution     TEXT CHECK (resolution IS NULL OR resolution IN
                   ('DISMISSED','APPLIED','MARK_VIOLATED')),
    resolved_at    TEXT,
    resolved_by    TEXT,
    resolve_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_conf_status ON route_event_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_conf_code ON route_event_conflicts(ticket_code);
CREATE INDEX IF NOT EXISTS idx_conf_route ON route_event_conflicts(route_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Any) -> datetime:
    """解析 ISO8601；朴素时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None  # 手工事务
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # WAL 模式持久化在数据库文件上；配合 BEGIN IMMEDIATE 实现单写多读
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        _drop_legacy_scaffolds(conn)
        try:
            conn.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            # 老库 events 表缺少路线模块新列时，SCHEMA 中依赖新列的索引会先
            # 失败；跳过该错误，下方 _migrate 重建 events 后再补建索引。
            if "route_id" not in str(e):
                raise
        _migrate(conn)
    finally:
        conn.close()


# 旧版本曾以不同 schema 建过一批空脚手架表（在场状态枚举为
# ON_SITE/DEPARTED/ABSENT、清点叫 muster_*、离场尝试单走 exit_attempts）。
# 它们与本模块 schema 不兼容；仅在“全部为空”时丢弃重建。任何一张非空
# （说明旧版本实际写过数据）都拒绝自动处理，避免静默丢轨迹。
_LEGACY_SCAFFOLDS = (
    "presence", "presence_corrections", "exit_attempts",
    "muster_snapshots", "muster_entries",
)
# 只有检测到旧版特征列/枚举时才认定是旧脚手架（新版 presence 有 arrived_version）
_LEGACY_MARK_SQL = {
    "presence": "SELECT 1 FROM pragma_table_info('presence') WHERE name='last_event_version'",
    "presence_corrections": "SELECT 1 FROM sqlite_master WHERE type='table' AND name='presence_corrections'",
    "exit_attempts": "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exit_attempts'",
    "muster_snapshots": "SELECT 1 FROM sqlite_master WHERE type='table' AND name='muster_snapshots'",
    "muster_entries": "SELECT 1 FROM sqlite_master WHERE type='table' AND name='muster_entries'",
}


def _drop_legacy_scaffolds(conn: sqlite3.Connection) -> None:
    existing = {}
    for table, probe in _LEGACY_MARK_SQL.items():
        if conn.execute(probe).fetchone() is not None:
            existing[table] = conn.execute(
                f"SELECT COUNT(*) c FROM {table}"
            ).fetchone()["c"]
    if not existing:
        return
    nonempty = {t: n for t, n in existing.items() if n > 0}
    if nonempty:
        raise RuntimeError(
            "检测到旧版在场/清点脚手架表中存在数据，拒绝自动迁移以免丢失轨迹: "
            f"{nonempty}；请人工核对后处理（这些表使用旧枚举 ON_SITE/ABSENT 与 "
            "muster_* 命名，与当前版本不兼容）"
        )
    # 先删依赖旧表的索引，再丢空表；IF EXISTS 保证可重复执行
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in _LEGACY_SCAFFOLDS:
            if table in existing:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    """老库就地升级：补列 + 重建 events 表以扩展事件类型 CHECK。"""
    gate_cols = {r["name"] for r in conn.execute("PRAGMA table_info(gates)")}
    if "zone_id" not in gate_cols:
        conn.execute("ALTER TABLE gates ADD COLUMN zone_id TEXT")
    ticket_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "zones" not in ticket_cols:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN zones TEXT NOT NULL DEFAULT '[]'"
        )
    if "batch_id" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN batch_id TEXT")
    if "application_id" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN application_id TEXT")
    if "party_size" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN party_size INTEGER")
    if "replaced_code" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN replaced_code TEXT")
    if "replaced_by_code" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN replaced_by_code TEXT")
    if "replacement_reason" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN replacement_reason TEXT")
    if "route_id" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN route_id TEXT")
    if "route_version" not in ticket_cols:
        conn.execute("ALTER TABLE tickets ADD COLUMN route_version INTEGER")

    batch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    if "route_id" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN route_id TEXT")
    if "route_version" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN route_version INTEGER")

    app_cols = {r["name"] for r in conn.execute("PRAGMA table_info(applications)")}
    if "change_version" not in app_cols:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN change_version INTEGER NOT NULL DEFAULT 0"
        )
    if "companion_names" not in app_cols:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN companion_names TEXT NOT NULL DEFAULT '[]'"
        )

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    # 每引入新事件类型/新列都需要重建一次（历史与版本号完整保留、继续递增）
    if row and (
        "APPLICATION_SUBMITTED" not in row["sql"]
        or "APPLICATION_CHANGE_SUBMITTED" not in row["sql"]
        or "PRESENCE_ARRIVED" not in row["sql"]
        or "ROUTE_STARTED" not in row["sql"]
        or "batch_id" not in row["sql"]
        or "rollcall_id" not in row["sql"]
        or "route_id" not in row["sql"]
    ):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """CREATE TABLE events_new (
                       id             INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts             TEXT NOT NULL,
                       type           TEXT NOT NULL
                                        CHECK (type IN (
                                          'TICKET_ISSUED','TICKET_REDEEMED',
                                          'TICKET_REVOKED','TICKET_EXPIRED',
                                          'POLICY_LOCK','POLICY_UNLOCK',
                                          'BATCH_CREATED','BATCH_CLOSED','BATCH_CAPACITY_CHANGED',
                                          'APPLICATION_SUBMITTED','APPLICATION_PROMOTED',
                                          'APPLICATION_APPROVED','APPLICATION_CANCELLED',
                                          'APPLICATION_REJECTED','APPLICATION_EXPIRED',
                                          'APPLICATION_CHANGE_SUBMITTED','APPLICATION_CHANGE_APPROVED',
                                          'APPLICATION_CHANGE_REJECTED','APPLICATION_CHANGE_CANCELLED',
                                          'APPLICATION_CHANGE_EXPIRED',
                                          'PRESENCE_ARRIVED','PRESENCE_DEPARTED',
                                          'PRESENCE_CORRECTED','ROLLCALL_TAKEN',
                                          'ROUTE_CREATED','ROUTE_VERSION_PUBLISHED',
                                          'ROUTE_PAUSED','ROUTE_RESUMED','ROUTE_BOUND',
                                          'ROUTE_CHECKPOINT_CLOSED','ROUTE_CHECKPOINT_OPENED',
                                          'ROUTE_STARTED','ROUTE_CHECKPOINT','ROUTE_COMPLETED',
                                          'ROUTE_VIOLATED','ROUTE_CONFLICT')),
                       ticket_code    TEXT,
                       person_id      TEXT,
                       gate_id        TEXT,
                       reason         TEXT,
                       payload        TEXT NOT NULL,
                       batch_id       TEXT,
                       application_id TEXT,
                       rollcall_id    TEXT,
                       route_id       TEXT,
                       route_version  INTEGER,
                       checkpoint_seq INTEGER,
                       progress_id    TEXT
                   )"""
            )
            # 老库可能还没有部分列（逐版补齐，列不存在先补 NULL）
            old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
            select_batch = "batch_id" if "batch_id" in old_cols else "NULL"
            select_app = "application_id" if "application_id" in old_cols else "NULL"
            select_rc = "rollcall_id" if "rollcall_id" in old_cols else "NULL"
            select_rid = "route_id" if "route_id" in old_cols else "NULL"
            select_rver = "route_version" if "route_version" in old_cols else "NULL"
            select_cseq = "checkpoint_seq" if "checkpoint_seq" in old_cols else "NULL"
            conn.execute(
                f"""INSERT INTO events_new
                       (id,ts,type,ticket_code,person_id,gate_id,reason,payload,
                        batch_id,application_id,rollcall_id,
                        route_id,route_version,checkpoint_seq,progress_id)
                   SELECT id,ts,type,ticket_code,person_id,gate_id,reason,payload,
                          {select_batch},{select_app},{select_rc},
                          {select_rid},{select_rver},{select_cseq},NULL
                     FROM events"""
            )
            conn.execute("DROP TABLE events")
            conn.execute("ALTER TABLE events_new RENAME TO events")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_route ON events(route_id)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # 新列对应的索引（IF NOT EXISTS，新旧库都安全；必须在补列/重建表之后执行）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tickets_batch ON tickets(batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id)"
    )
    # 路线模块的 events 索引（老库 SCHEMA 执行时可能因旧表缺列而跳过，
    # 重建 events 后在此幂等补建；列不存在则忽略并由下次启动补齐）
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_route ON events(route_id)"
        )
    except sqlite3.OperationalError:
        pass


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """立即获取写锁的事务，把并发核销/申请在数据库层串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def add_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    ts: str,
    ticket_code: Optional[str] = None,
    person_id: Optional[str] = None,
    gate_id: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
    batch_id: Optional[str] = None,
    application_id: Optional[str] = None,
    rollcall_id: Optional[str] = None,
    route_id: Optional[str] = None,
    route_version: Optional[int] = None,
    checkpoint_seq: Optional[int] = None,
    progress_id: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO events
               (ts, type, ticket_code, person_id, gate_id, reason, payload,
                batch_id, application_id, rollcall_id,
                route_id, route_version, checkpoint_seq, progress_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts,
            event_type,
            ticket_code,
            person_id,
            gate_id,
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            batch_id,
            application_id,
            rollcall_id,
            route_id,
            route_version,
            checkpoint_seq,
            progress_id,
        ),
    )
    return int(cur.lastrowid)


def event_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["version"] = data.pop("id")
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except json.JSONDecodeError:
        pass
    return data


def ticket_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in ("revoked",):
        d.pop(col, None)
    try:
        d["zones"] = json.loads(d.get("zones") or "[]")
    except json.JSONDecodeError:
        d["zones"] = []
    return d
