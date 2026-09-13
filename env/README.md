# ShortPass · 短时通行票与多门点并发核销

给访客发短时通行票、多个门点同时核销的 Web 系统，支持**分区通行策略**与**紧急封锁**。

- **FastAPI + SQLite(WAL)**，单容器即可部署；数据落盘在 `/data` 卷，重启不丢状态。
- 无外部前端依赖（原生 HTML/JS），自带管理台与门点核销台。
- 访客预约批次模块：管理员建批（日期/时段/分区/人数上限）→ 访客凭一次性链接申请
  （姓名/联系方式/同行人数及同行人名单，请求幂等）→ 容量内待审核、满额按提交时间进候补队列 →
  管理员审核/取消/拒绝/补录，取消已审核即作发票并按 FIFO 释放名额给候补 →
  审核通过在同一事务签发与批次分区一致的通行票；访客可凭管理令牌申请变更，管理员审核后
  原子换发新票（旧票作废并指向新票），申请带单调变更版本；门点核销可见批次、变更版本、
  同行总人数与同行人名单。
- **访客在场清册 + 应急清点**：核销成功即在同一事务登记到场（申请人 + 同行人数/名单
  当场冻结），门点确认离场；到场、离场与管理员人工更正共用同一写锁排成唯一顺序，
  已离场的人不会因重放/重扫/并发回到在场名单；漏扫/误扫可由管理员带原因人工更正，
  原始轨迹与操作者保留；应急清点生成**不可变快照**（在场人员、同行名单、批次分区、
  最后门点记录全部冻结，之后变化不改旧快照）；四类事件进同一版本流，门点离线重连
  按版本补齐。
- `tests/` 含 57 个端到端测试（真实 HTTP + 进程重启），覆盖下列全部不变量。

## 需求对应的关键保证

| 需求 | 实现方式 |
|---|---|
| 同一人可持有多张时间重叠的票，按票面编号核销，互不顶替 | 每张票是独立行（`tickets.code` 主键）；核销只按 `code` 定位，不做“同人唯一有效票”逻辑 |
| 两个门点同时扫同一张，恰有一个成功，另一个明确知道已使用 | 核销在单个 `BEGIN IMMEDIATE` 事务中完成，条件更新 `WHERE code=? AND status='ACTIVE'`；失败方返回 HTTP 409 + `reason=already_redeemed`，并告知 `redeemed_gate` / `redeemed_at` |
| 每张票只能成功核销一次 | 状态机单向迁移 `ACTIVE → REDEEMED/REVOKED/EXPIRED`，终态无任何回退路径；核销事件 append-only |
| 作废一张，其他有效期内的票继续可用 | 作废只改对应 `code` 的行，返回 410 `revoked` 给门点；同人其他票不受影响 |
| 撤销、过期、重启不能让票复活 | 所有终态落库；过期由后台清扫（默认 10s）+ 核销时惰性判定两种途径，且过期事件只记一次；无任何 API 可改回 ACTIVE；版本号用 SQLite AUTOINCREMENT，重启后继续递增不复用 |
| 门点离线重连按版本补齐撤销/核销/策略事件 | `events` 表只追加，`id` 即全局版本；`POST /api/gate/sync {"since_version": n}` 按序拉取（每页 200 条，`has_more` 翻页）；封锁规则与票据事件共用同一版本流，门点页面把游标与策略版本存 localStorage，每 5 秒补齐 |
| 扫码请求网络抖动重发不能重复核销 | 门点为**每次物理扫码**生成新的 `attempt_id`；`(gate_id, attempt_id)` 唯一约束，重放返回首次结果并带 `"replayed": true`。注意：同一张票的**第二次扫码**（新 attempt_id）会得到“已使用” |
| 管理员按人查看可用票、门点记录、状态变化原因 | `GET /api/admin/people/{id}` 返回：当前有效票、全部票（含有效区间/终态时间与原因）、append-only 事件流水（版本+原因+门点）、该人所有票的扫码记录（成功/拒绝） |
| 门点按分区通行，票指定允许分区 | `zones` 注册表 + `gates.zone_id` + `tickets.zones`（JSON 数组）；门点分区不在票据允许列表中 → 403 `zone_mismatch`（不消耗票据） |
| 未知分区、缺少策略的门点默认拒绝 | 门点未配置分区 → 403 `gate_no_policy`；分区被注销后引用它的门点 → 403 `unknown_zone`；发票时指定不存在的分区 → 400 |
| 管理台发布带版本号的封锁/解除规则 | `POST /api/admin/policy/rules`（`action=LOCK/UNLOCK`，`zone_id` 为空=全局）；发布即写 `POLICY_LOCK/POLICY_UNLOCK` 事件，版本号即 `events.id`；分区当前状态 = 版本最新的适用规则（本区或全局） |
| 重复发布同一规则不重复生效 | `rule_id` 是 `policy_rules` 主键：同内容重发返回原规则 `duplicated=true`（HTTP 200，不产生新版本/事件）；同 ID 不同内容 → 409 |
| 策略变更与并发核销有明确先后 | 规则发布与核销都走 `BEGIN IMMEDIATE` 写事务串行化：核销事件版本 < 规则版本 ⇒ 按旧策略放行，反之按新策略判定；核销响应与事件都记录判定时的 `policy_version` |
| 门点规则版本过旧要明示 | 核销请求携带 `policy_version`（门点已同步的最新策略版本）；落后于服务器 → 409 `stale_policy` 并附 `current_policy_version`；该结果**不写入**扫码记录，门点按版本补齐后可用同一 `attempt_id` 安全重试 |
| 管理员按分区查看当前规则、受影响票据、门点执行记录 | `GET /api/admin/zones/{id}` 返回：当前规则与全部历史、允许该分区的票据、本分区门点的扫码执行记录；`GET /api/admin/zones` 为总览（封锁状态/门点数/受影响票数） |

### 访客预约批次的关键保证

| 需求 | 实现方式 |
|---|---|
| 管理员建批：日期、时段、所属分区、人数上限 | `POST /api/admin/batches`；批次带秘密 `apply_token`，申请链接形如 `/apply?k=<token>`；容量调整在 `batch_capacity_log` append-only 留痕并产生 `BATCH_CAPACITY_CHANGED` 事件 |
| 访客一次性链接提交姓名/联系方式/同行人数 | 公开端点（无 Bearer，鉴权靠链接令牌）`POST /api/public/batches/{token}/applications`；同行人数不含本人，**整组**（1+同行人）占用名额 |
| 同一申请请求重复提交必须幂等 | 表单“一次提交动作”生成一个 `request_id`（唯一约束），双击/网络重发返回首次结果并带 `replayed=true`（200）；同 `request_id` 不同载荷返回 409 |
| 多访客同时申请不超容量 | 提交在单个 `BEGIN IMMEDIATE` 事务内分配批次内单调 `seq` 并判定 `used + party_size <= capacity`，数据库层串行化；测试以 20 线程抢 10 席验证恰好 10 个在容量内 |
| 满额进入按提交时间排序的候补队列 | 放不下即为 `WAITLISTED`，候补名次按 `seq`；后台/管理视图按名次展示。释放名额时严格 FIFO：队首整组放得下才晋级，**放得下但后面的团体更小也不跳过队首** |
| 审核 / 取消 / 拒绝 / 补录 | 管理端三个决定端点 + `POST .../backfill`（补录默认直接通过签票，同样受容量约束，超容量 409，不会留下“通过但无票”）；候补不能越级通过（409），必须先晋级 |
| 取消已审核申请按候补顺序释放名额 | 取消在同一事务内先作废通行票（票已核销=访客已到场则拒绝取消，409），再 `APPLICATION_CANCELLED`，最后 FIFO 晋级候补（可连续晋级多组）；拒绝/取消待审核申请同样触发晋级 |
| 审核通过自动签发与批次分区一致的票 | 审核与发票在**同一 IMMEDIATE 事务**：票 `zones=[批次.zone_id]`，`tickets.batch_id/application_id/party_size` 关联；票有效期默认覆盖批次时段（审核时不早于当前时刻） |
| 取消、过期、候补未晋级不产生可用票 | 这些路径完全不调发票；已审核取消则票立即 `REVOKED`；批次结束后清扫线程把未决申请置 `EXPIRED`（`APPLICATION_EXPIRED` 事件），申请终态机 `PENDING/WAITLISTED → APPROVED/CANCELLED/REJECTED/EXPIRED` 不可逆 |
| 访客申请变更（姓名/联系方式/同行人数/同行名单），管理员审核 | 访客凭申请的 `manage_token` 调 `POST /api/public/applications/{token}/changes`；变更写入 `application_changes`（状态机 `PENDING→APPROVED/REJECTED/CANCELLED/EXPIRED`），同 request_id 重放返回首次结果（`replayed=true`），同键不同载荷 409；同一申请同时只允许一个 PENDING；无差异请求返回 `noop=true` 不建单 |
| 已核销申请不得再变更 | 票 `REDEEMED`（访客已到场）后，提交变更即 409；审核与核销竞态时审批事务复查旧票状态，已核销则 409 且不动任何票 |
| 并发变更不突破容量、候补按新总人数重排 | 审批在 `BEGIN IMMEDIATE` 内串行：`used - 旧人数 + 新人数 <= capacity` 才放行；改写申请资料后按新 `party_size` 重跑 FIFO 晋级（缩小释放名额/候补自身缩小都可触发晋级，放大超容量 409） |
| 旧票原子撤销 + 新票签发 | 已通过申请的变更通过时，同一 IMMEDIATE 事务内：旧票置 `REVOKED`（记 `replaced_by_code` 与含变更说明的 `revoked_reason`）→ 新票 `ACTIVE`（记 `replaced_code`/`replacement_reason`，沿用旧票有效期）→ 申请改指新票、`change_version+1`。提交/回滚原子，任何时刻不会有两张可用票或无票；连续变更换发形成旧→新替换链 |
| 被拒绝/撤回/过期的变更不产生可用票 | 拒绝与撤回只改变更单终态，申请资料与旧票不动、零发票；批次结束清扫把未审变更置 `EXPIRED`（`APPLICATION_CHANGE_EXPIRED`）；申请本身被拒绝/取消时其待审变更联动终结（`*_CHANGE_REJECTED/CANCELLED`） |
| 核销与批次详情显示变更版本/同行名单/替换原因 | 核销响应 `appointment` 附 `change_version`、`companion_names`；新票附 `replaced_code`+`replacement_reason`，旧票返回 410 并附 `replaced_by_code`（门点引导扫新票）；`GET /api/admin/batches/{id}` 含每条申请的当前版本/名单、变更审核队列、票的新旧替换链 |
| 门点离线重连按版本补齐变更/撤销/换发事件 | `APPLICATION_CHANGE_SUBMITTED/APPROVED/REJECTED/CANCELLED/EXPIRED` 与事务内的 `TICKET_REVOKED`(旧票)、`TICKET_ISSUED`(新票) 共用同一 append-only 版本流，`/api/gate/sync` 无需特殊处理即按版本有序补齐 |
| 门点核销看到批次和同行人数 | 核销响应（含幂等重放）附 `appointment: {batch_id, batch_name, visit_date, zone_id, applicant_name, party_size, ...}`，门点台大字显示批次与“同行共 N 人”，历史记录同样展示 |
| 离线重连按版本补齐预约与票据变化 | 预约事件（`BATCH_*` / `APPLICATION_*`）与票据/策略事件共用同一 append-only 版本流，门点 `/api/gate/sync` 无需改造即可按版本补齐 |
| 管理员按批次查看申请、候补顺序、已签发票、容量变化 | `GET /api/admin/batches/{id}`：批次占用计数、全部申请（含候补名次/晋级时间）、候补队列、已签发票、`batch_capacity_log`、批次事件流水 |
| 批次提前关闭 / 容量调整 | `POST .../close` 停止接受新申请（已签票不受影响）；`PUT .../capacity` 不允许低于当前占座数，调高时同事务自动晋级候补 |

### 访客在场清册与应急清点的关键保证

| 需求 | 实现方式 |
|---|---|
| 核销成功登记到场（申请人 + 同行人数） | 核销条件 UPDATE 成功后在**同一 IMMEDIATE 事务**写 `presence`（一 票一行）+ `PRESENCE_ARRIVED` 事件；整组人数、同行人名单、分区、批次、到场门点当场冻结 |
| 离场由门点确认 | `POST /api/gate/departure`（同样以 `(gate_id, attempt_id)` 幂等，离线重发只离场一次），只有当前 `ARRIVED` 可离场；未到场 409 `no_presence`、已离场/移除 409 `not_present`，票面不存在 404 |
| 到场/离场/人工更正并发只能有一个明确顺序 | 三者全部在 `BEGIN IMMEDIATE` 写事务内串行；`presence_events` 按票单调 `seq`，状态机 `ARRIVED→DEPARTED` / `→REMOVED`；并发落败方拿到 409，不会产生半截状态 |
| 已离场的人不能又出现在当前在场名单 | 当前在场名单固定为 `presence.status='ARRIVED'`；门点自动路径（核销/离场）在 `DEPARTED/REMOVED` 下不写入，核销重放仍只返回 `already_redeemed`。只有管理员**显式带原因**的 RESTORE/补登记才能纠正回场 |
| 漏扫/误扫人工更正，带原因且保留原始轨迹与操作者 | `POST /api/admin/presence/corrections`：`MARK_ARRIVED`（漏扫到场；票仍 ACTIVE 时同事务核销）、`MARK_DEPARTED`（漏扫离场）、`REMOVE`（误扫移除）、`RESTORE`（纠正回场）；原因必填，只向 `presence_events` 与 `PRESENCE_CORRECTED` 事件**追加**，从不删除/改写既有的到场、离场记录 |
| 应急清点快照固定当时在场人员、同行名单、批次分区与最后门点 | `POST /api/admin/rollcalls` 在单个事务内把当时全部在场组（可按分区/批次过滤）逐行复制到 `rollcall_entries`；快照表只插入、无更新/删除接口；之后到场/离场/更正只追加事件、产生新快照，旧快照字节级不变（测试逐条比对） |
| 快照之后的变化不能改写旧快照 | 快照存的是冻结副本（人数/名单/`last_gate`/`last_event_version`/`presence_seq` 都是值拷贝），不与 `presence` 做任何 JOIN |
| 门点离线重连补齐到离场、更正与清点快照事件 | `PRESENCE_ARRIVED` / `PRESENCE_DEPARTED` / `PRESENCE_CORRECTED` / `ROLLCALL_TAKEN` 与票据、策略事件共用同一 append-only 版本流；`/api/gate/sync` 无需特殊处理即按版本有序补齐 |
| 按分区、批次、人员查询当前在场 / 未确认离场 / 任一次清点结果 | `GET /api/admin/presence?view=onsite\|unconfirmed\|departed\|removed\|all&zone_id=&batch_id=&person_id=&q=`；`GET /api/admin/rollcalls?zone_id=&batch_id=&person_id=`（按人命中其当时在场的快照）；`GET /api/admin/rollcalls/{id}` 取冻结明细；另有 `GET /api/admin/presence/{code}` 单票完整轨迹 |

### 核销响应约定（门点端可直接据此亮灯/播报）

| 场景 | HTTP | `ok` | `reason` |
|---|---|---|---|
| 核销成功 | 200 | true | — |
| 已被核销（含并发落败方） | 409 | false | `already_redeemed` |
| 门点规则版本过旧 | 409 | false | `stale_policy`（附 `current_policy_version`，**未落库**，同步后同 `attempt_id` 重试） |
| 未到生效时间 | 403 | false | `not_yet_valid` |
| 分区不匹配 | 403 | false | `zone_mismatch`（附门点 `zone_id` 与票据 `ticket_zones`，不消耗票据） |
| 门点未配置分区策略 | 403 | false | `gate_no_policy` |
| 门点分区未知（已注销） | 403 | false | `unknown_zone` |
| 分区紧急封锁中 | 423 | false | `locked`（附 `lock_rule` 规则详情，不消耗票据） |
| 已作废 | 410 | false | `revoked`（含 `revoked_reason`） |
| 已过期 | 410 | false | `expired` |
| 票面不存在 | 404 | false | `not_found` |

4xx 是**明确的业务终态结论**，门点端不应把这些请求重试成“待核销”；只有网络错误/5xx 才进离线队列重发（用同一个 `attempt_id`，天然幂等）。`stale_policy` 是唯一的例外：它不是业务结论，门点补齐策略事件后用同一 `attempt_id` 重试即可。

## Docker 部署

```bash
# 1. 设置令牌（也可直接改 docker-compose.yml 的默认值）
export PASSPORT_ADMIN_TOKEN='换成强随机串'
export PASSPORT_GATE_TOKEN='换成另一个强随机串'

# 2. 启动
docker compose up -d --build

# 3. 打开
#    管理台  http://服务器:8080/admin
#    门点台  http://服务器:8080/gate  （每个门点一个浏览器/平板，各自填门点ID）
```

数据在命名卷 `shortpass-data`（容器内 `/data/passport.db`）。备份时停容器后拷贝该卷，或直接用 `docker run --rm -v shortpass-data:/data ... tar` 打包。

不用 Docker 时：

```bash
pip install -r requirements.txt
PASSPORT_ADMIN_TOKEN=xxx PASSPORT_GATE_TOKEN=yyy \
PASSPORT_DB=./passport.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PASSPORT_ADMIN_TOKEN` | `admin-change-me` | 管理 API/页面用 Bearer 令牌 |
| `PASSPORT_GATE_TOKEN` | `gate-change-me` | 门点 API/页面共用 Bearer 令牌 |
| `PASSPORT_DB` | `./passport.db`（镜像内 `/data/passport.db`） | SQLite 路径 |
| `PASSPORT_SWEEP_INTERVAL` | `10` | 过期清扫周期秒数（核销时另有惰性过期判定） |

> 门点身份是“共享门点令牌 + 预先注册的 `gate_id`”双层：管理员先在管理台注册门点；门点被停用后核销与同步都会返回 410。

## 使用流程

1. 管理台填管理员令牌 →「分区与紧急封锁」创建分区（如 `Z-A` A 区）→「门点管理」注册各门点并在表格里为每个门点选择分区（未配置分区的门点核销默认拒绝）。
2. 「签发通行票」：填人员标识 + 有效期（分钟），勾选允许通行的分区（不勾选 = 所有门点拒绝）；票号形如 `T-XXXXXXXX`（Crockford base32，无易混字符），可做成二维码。
3. 门点平板打开 `/gate`，填门点 ID + 门点令牌；扫码枪输入票号回车即可核销，大字号显示放行/拒绝原因（含分区不符、封锁中、版本过旧等）。
4. 紧急情况下在「分区与紧急封锁」发布规则：选封锁/解除、目标分区（或全局）、填规则 ID（留空自动生成）与原因；发布即产生新版本，重复发布同一规则 ID 不会重复生效。
5. 网络断开时扫码进入本地队列，重连后自动补提交；同时按版本号补齐离线期间的作废/核销/过期/**封锁规则**事件，页面显示补齐日志与当前策略版本。若核销被拒为“规则版本过旧”，门点会自动补齐后用同一次扫码重试。
6. 管理台「按分区查看」可看到：分区当前规则与历史版本、受影响票据、本分区门点的执行记录；「按人查看」可看到：当前可用票数、每张票的终态与原因、状态流水版本、各门点扫码成功/拒绝记录。
7. 访客预约：「访客预约批次」卡片填日期/时段/分区/人数上限建批，复制生成的 `/apply?k=…` 链接发给访客；访客提交（可填每位同行人姓名）后容量内为“待审核”，满额自动进候补。右侧「预约批次」标签可按批次审核/取消/拒绝/补录、调整容量、关闭申请、查看候补顺序、已签发票、**申请变更审核队列**与容量变化记录；访客凭申请后保存的管理链接可发起/撤回变更，审核通过后系统原子换发新票，门点扫到旧票会提示改扫新票。
8. 在场清册：门点台用「入场核销/离场确认」下拉切换模式（离场扫码同样离线排队、attempt 幂等）；管理台「在场清册」标签可按当前在场/未确认离场/已离场/误扫移除与分区、批次、人员过滤，点开任一票查看完整在场轨迹并对漏扫（补登记到场/登记离场）、误扫（移除/纠正回场）做**带原因**的人工更正，更正只追加轨迹、保留操作者。
9. 应急清点：「应急清点」标签填原因（可选分区/批次范围）一键发起，快照固定当时在场人员、同行名单、批次分区与最后门点；历史快照随时回看，发起后的到场/离场/更正不会改写旧快照。

## API 摘要

管理端（`Authorization: Bearer <PASSPORT_ADMIN_TOKEN>`）

- `POST /api/admin/gates` / `GET /api/admin/gates` / `DELETE /api/admin/gates/{id}`
- `PUT  /api/admin/gates/{id}/zone` `{zone_id}`（null = 清除分区，门点回到默认拒绝）
- `POST /api/admin/zones` / `GET /api/admin/zones` / `GET /api/admin/zones/{id}` / `DELETE /api/admin/zones/{id}`
- `POST /api/admin/policy/rules` `{action: "LOCK"|"UNLOCK", zone_id?, rule_id?, reason?}` → 201 新规则 / 200 `duplicated`（重复发布不生效）/ 409（同 ID 不同内容）
- `GET  /api/admin/policy/rules?zone_id=`（规则列表 + 当前策略版本）
- `POST /api/admin/tickets`  `{person_id, ttl_seconds, zones}` 或 `{person_id, valid_from, valid_until, note, zones}`
- `POST /api/admin/tickets/revoke` `{code, reason}`
- `GET  /api/admin/tickets/{code}`
- `GET  /api/admin/people?q=` / `GET /api/admin/people/{person_id}`
- `GET  /api/admin/events?since=&code=&person_id=` （状态变化流水，版本即 `events.id`，含 `POLICY_LOCK`/`POLICY_UNLOCK`）
- `GET  /api/admin/attempts?gate_id=` （门点扫码记录）
- `GET  /api/admin/stats`

访客预约批次（管理端，Bearer 管理员令牌）

- `POST /api/admin/batches` `{visit_date, start_at, end_at, zone_id, capacity, name?}` → 批次 + `apply_token`（申请链接令牌）
- `GET  /api/admin/batches` / `GET  /api/admin/batches/{id}`（申请/候补顺序/已签发票/容量变化/事件流水）
- `PUT  /api/admin/batches/{id}/capacity` `{capacity, reason?}`（不可低于当前占座；调高自动 FIFO 晋级候补）
- `POST /api/admin/batches/{id}/close`（提前关闭申请，已签票不受影响）
- `POST /api/admin/batches/{id}/backfill` `{name, contact, companions, companion_names?, approve=true}`（管理员补录，同事务签票，受容量约束）
- `POST /api/admin/applications/{id}/approve`（同事务签发与批次分区一致的票）
- `POST /api/admin/applications/{id}/reject` / `cancel` `{reason}`（取消已审核先作发票，再按候补顺序释放名额）
- `GET  /api/admin/changes?batch_id=&application_id=&status=`（变更审核队列）
- `POST /api/admin/changes/{id}/approve` / `reject` `{reason}`（通过已通过申请的变更时同事务撤销旧票+签发新票；超容量或旧票已核销 → 409）
- `GET  /api/admin/events?batch_id=`（批次事件过滤）

访客端（公开，无 Bearer；授权靠一次性链接令牌）

- `GET  /api/public/batches/{apply_token}`（批次信息：时段/分区/剩余名额/候补组数）
- `POST /api/public/batches/{apply_token}/applications` `{request_id, name, contact, companions, companion_names?}`
  → 201 新申请（`PENDING`/`WAITLISTED`）；同 `request_id` 重放 → 200 `replayed=true`；同键不同载荷 → 409；名单数量与人数不符 → 400；批次结束/关闭 → 410
- `GET  /api/public/applications/{manage_token}`（访客查询自身申请状态、候补名次、当前票、变更版本 `change_version` 与变更历史 `changes`/`pending_change`）
- `POST /api/public/applications/{manage_token}/changes` `{request_id, name, contact, companions, companion_names?}`
  → 201 变更单（PENDING）；重放 200 `replayed=true`；无差异 200 `noop=true`；已有待审变更/票已核销/申请终态 → 409；批次已结束 → 410
- `POST /api/public/changes/{change_id}/cancel` `{manage_token}`（访客撤回自己的待审变更，不产生票）
- 申请页：`/apply?k=<apply_token>`

门点端（`Authorization: Bearer <PASSPORT_GATE_TOKEN>`）

- `POST /api/gate/redeem` `{gate_id, code, attempt_id, policy_version}`
- `POST /api/gate/departure` `{gate_id, code, attempt_id}`（门点确认离场；同样以 attempt_id 幂等）
- `POST /api/gate/sync` `{gate_id, since_version}` → `{events:[...], next_since, has_more}`（含封锁策略、到场/离场/更正/清点事件）
- `GET  /api/gate/heartbeat/{gate_id}`

在场清册与应急清点（管理端，Bearer 管理员令牌）

- `GET  /api/admin/presence?view=onsite|unconfirmed|departed|removed|all&zone_id=&batch_id=&person_id=&q=`（当前在场/未确认离场/已离场/误扫移除；含按分区、批次、人员、关键字过滤与汇总）
- `GET  /api/admin/presence/summary`（在场组数/人数，按分区、批次拆分）
- `GET  /api/admin/presence/{code}`（单票当前状态 + append-only 在场轨迹，含每次人工更正的操作者与原因）
- `POST /api/admin/presence/corrections` `{code, action: "MARK_ARRIVED"|"MARK_DEPARTED"|"REMOVE"|"RESTORE", reason, gate_id?, operator?, party_size?, companion_names?}`（漏扫/误扫人工更正，原因必填；原轨迹保留）
- `GET  /api/admin/presence-events?code=&person_id=`（在场轨迹总流水）
- `POST /api/admin/rollcalls` `{reason, zone_id?, batch_id?, operator?}` → 201 不可变快照
- `GET  /api/admin/rollcalls?zone_id=&batch_id=&person_id=` / `GET /api/admin/rollcalls/{id}`（历史清点列表 / 任一次清点的冻结明细）

## 测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
# 57 passed
```

测试启动真实 uvicorn 子进程打真实 HTTP，包含：双门点线程屏障并发核销（连跑多轮验证）、
同人三票独立核销、作废隔离、过期不复活、幂等重放、离线游标补齐、**杀进程重启后终态不复活且版本号延续**、门点停用、鉴权，
以及分区与封锁：分区不匹配不消耗票据、缺少策略/未知分区默认拒绝、封锁-版本过旧-解锁全流程、
全局封锁、规则幂等重发与冲突、策略事件离线补齐、**封锁发布与并发核销的先后序验证**、按分区的管理视图；
预约批次：批次创建与链接校验、申请幂等（重放/载荷冲突）、**20 线程抢 10 席不超售**、
候补 FIFO（不跳过放不下的队首）、审核自动签同分区票且候补不可越级、取消已审核同事务作发票并晋级、
票已核销不可取消、补录受容量约束、容量调整留痕与上下限、批次到期清扫无票、门点同步补齐预约事件、
核销可见批次与同行人数、**重启后申请/票/链接幂等全部延续**；
申请变更：变更请求幂等/单待审/无差异 noop、已通过变更原子换票（旧票 REVOKED+新票 ACTIVE 同事务、
替换链与原因可追溯、旧票门点 410 指向新票）、已核销不可变更、并发变更不超容量、
候补按新总人数重排晋级、拒绝/撤回/过期变更不发票、待审变更随申请终结、
门点核销显示变更版本与同行名单、门点同步补齐撤销/换发事件、重启后版本与替换链延续；
在场清册与应急清点：核销自动登记到场（整组人数/名单/分区/批次/门点冻结）、门点确认离场与
attempt 幂等、未到场/已离场的明确拒绝、离场不被重放/重扫复活、到场-离场-人工更正并发唯一顺序、
漏扫补登记（票同事务核销）、漏扫离场、误扫移除、纠正回场且原轨迹保留、
快照固定且随后变化不改旧快照、按分区/批次范围清点、按分区/批次/人员查询清册与任一次清点、
门点按版本补齐四类新事件、重启后在场状态/轨迹/快照延续。

> 注意：`tests/test_zones.py` 依赖在 `test_system.py` 之后运行（pytest 默认按文件名字序），
> 因为 test_system 的用例假定“尚未发布任何封锁规则”（门点策略版本为 0）。

## 升级说明（老库迁移）

启动时自动就地迁移：`gates` 加 `zone_id` 列、`tickets` 加 `zones` 列（默认 `[]`）并在引入
预约批次后再加 `batch_id` / `application_id` / `party_size` 列、
重建 `events` 表以扩展事件类型 CHECK（并加 `batch_id` / `application_id` / `rollcall_id` 列；
申请变更上线时再次重建以加入 5 个 `APPLICATION_CHANGE_*` 类型；在场清册上线时第三次重建以加入
`PRESENCE_ARRIVED/PRESENCE_DEPARTED/PRESENCE_CORRECTED/ROLLCALL_TAKEN` 类型），
新建 `batches` / `applications` / `batch_capacity_log` / `application_changes` 表，
以及在场清册的 `presence` / `presence_events` 与应急清点的 `rollcalls` / `rollcall_entries` 表
（事件与版本号完整保留、继续递增）。
若库中存在旧版空脚手架表 `presence`（旧枚举 `ON_SITE/ABSENT`）、`presence_corrections`、
`exit_attempts`、`muster_snapshots`、`muster_entries`，启动时仅在它们**全部为空**时丢弃并按
新 schema 重建；任何一张含数据都会拒绝启动迁移以免静默丢轨迹，需人工核对。
**注意**：升级前已存在的票 `zones` 为空数组，按“未授权任何分区”处理，在所有门点都会被
`zone_mismatch` 拒绝；需要的话请作废旧票并按分区重新签发。

## 部署规模与边界说明

- SQLite WAL + 单写者串行事务适合单机到部门级规模（每秒数十次核销、单库几十万票据无压力）；
  多实例水平扩展需换 Postgres（`UPDATE ... WHERE status='ACTIVE' RETURNING` 同样满足并发单赢家语义）。
- 门点令牌是共享密钥模型；若每门点需独立密钥，可在 `gates` 表加 `token_hash` 列扩展。
- 时钟以服务器 UTC 为准；门点本地不裁决时间，只负责展示与重放。
