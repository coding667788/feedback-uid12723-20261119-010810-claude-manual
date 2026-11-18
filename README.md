# 本地成员目录：合成邀请、离开与批量迁移

Windows 原生 Python 3.12，仅标准库、纯 CPU、离线。多个进程通过一个 SQLite 文件共享状态。

```
python -m solution --input cases.json --output result.json
python -m unittest discover -s tests -v
```

两条命令都在仓库根目录执行，无需安装任何依赖。

---

## 1. 模型

### 1.1 邮箱规范化

- 整个地址必须是可打印 ASCII（否则 `INVALID_EMAIL`）：非 ASCII 会使"域名 ASCII 小写"的规则无法无歧义地成立。
- 恰好一个 `@`；本地部分非空并**逐字节保留大小写**；域名整体转小写。
- 域名不能为空、不能有连续点或首尾点，标签首尾不能是 `-`。
- 例：`O'Brien.Tag@Example.COM` → `O'Brien.Tag@example.com`；`Müller@example.com` → `INVALID_EMAIL`。
- 因此 `alice@example.com` 与 `Alice@example.com` 是**两个不同绑定**（本地部分大小写敏感）。

### 1.2 两套独立代数

| 对象 | 代数 | 初始 | 何时 +1 |
| --- | --- | --- | --- |
| 成员 `members.member_epoch` | 每次加入/离开/迁出 | 新成员 = 1 | 离开或迁出时 +1 |
| 邀请 `invites.invite_epoch` | 每个 `(tenant, email)` 绑定各自计数 | 首张 = 1 | 每次新签发 +1（含过期后重发，不复用） |

- 成员身份 `member_id` 形如 `<local>@<tenant>.m<序号>`，序号是该 `(tenant, email)` 历史上第几次建立成员关系（从 1 开始，只增不减）。
- 邀请过期、撤销、被新版取代都**不**重置代数；被取代的邀请 `status=superseded`，旧代数永不再 pending。
- 迁移请求必须同时引用**源成员代数**与**目标邀请代数**；两者任何一项与当前值不符即拒绝。

### 1.3 邀请

- `invite.issue` 绑定 `(tenant, email, invite_epoch, expires_at)`。`expires_at = now + ttl_ms`。
- 同一绑定已有 pending 邀请时，旧邀请转 `superseded`（`superseded_previous` 回执字段）。
- 有效期判定：`now < expires_at` 可用；**`now >= expires_at` 即过期**（毫秒精度，"恰好到期"算过期）。
- 过期是时间驱动状态：任何涉及该租户的写请求都会在事务内先把到期邀请物化为 `status=expired` 并写审计 `invite.expired`（`request_id` 为 NULL）。
- 签发邀请不检查该席位当前是否有人占用：占用与否只在 `migrate.commit` 里按"整批源先移出"判断（见 1.5）。

### 1.4 离开

`member.leave`（成员主动）与 `member.revoke`（管理撤销）语义相同，仅审计动作名与默认原因不同。

- 必须给 `member_epoch`：不匹配 → `MEMBER_EPOCH_MISMATCH`；不 active → `MEMBER_NOT_ACTIVE`。
- 流转：`status=revoked`、`member_epoch += 1`、写审计 `member.left` / `member.revoked`，`reason` 取请求的 `reason`（缺省给固定文案）。

### 1.5 批量迁移 `migrate.commit`

请求形如：

```json
{"request_tenant": "t-a", "reason": "quarterly rotation",
 "moves": [
   {"source": {"tenant_id": "t-a", "member_id": "p@t-a.m1", "member_epoch": 1},
    "target": {"tenant_id": "t-b", "email": "p@example.com", "invite_epoch": 1}},
   {"source": {"tenant_id": "t-b", "member_id": "q@t-b.m1", "member_epoch": 1},
    "target": {"tenant_id": "t-c", "email": "q@example.com", "invite_epoch": 1}}
 ]}
```

语义（全部通过才生效，否则整批失败、业务表零改动）：

1. **结构**：批次非空；每个 move 的 source/target 字段齐全；target 邮箱可规范化。同一 `moves` 里**禁止重复源**（`DUPLICATE_SOURCE`）与**重复目标绑定**（`DUPLICATE_TARGET`）。全部元素先于任何校验检查重复，下标从 0 起。
2. **源**：成员存在、`active`、`member_epoch` 匹配；迁移必须跨租户（同租户 `SAME_TENANT`）。
3. **目标邀请**：存在且代数匹配（否则 `INVITE_NOT_FOUND`）、未过期（`INVITE_EXPIRED`）、仍 pending（`INVITE_NOT_PENDING`，例如被撤销/取代/已消费）。
4. **占用**：把整批所有源都视为"已移出"后再看目标席位；若目标 `(tenant, email)` 仍有他人 active 且它不在本批 vacated 集合里 → `TARGET_OCCUPIED`。**因此互换（A↔B）成立，即便两边目标邮箱当前都有人。**
5. **效果**（同一事务、同一保存点）：源成员 `revoked` 且代数 +1 → 目标邀请 `consumed`（记 `consumed_at_ms`）→ 在目标租户插入新成员（代数 1，`invite_id` 指向被消费的邀请）→ 写审计 `migration.source_revoked` / `invite.consumed` / `migration.target_joined` / `migration.committed`，均带批次 `reason`。

批次结果与 `moves` 顺序无关：互换、环状轮换（A→B→C→A）均可。

### 1.6 请求 ID 幂等

- 唯一键是 `(tenant_id, request_id)`。`migrate.commit` 的作用域是 `request_tenant`（缺省取第一个 move 的源租户）。
- 同 ID + **完全相同参数** → 原样返回**已持久化**的第一次回执（含原始 `at`），不重复产生副作用；成功与失败都是如此。
- 同 ID + 不同参数 → `REQUEST_ID_CONFLICT`，且**不写任何东西**（也不改原回执）。该冲突回执本身是临时应答，不落库。
- 不同租户可用同一个 request_id，彼此独立。
- 参数指纹 = `sha256({op, params})`，与执行时刻无关，所以隔一段时间重放仍命中。

---

## 2. 持久化与并发

- SQLite，`journal_mode=WAL`、`synchronous=FULL`、`busy_timeout=60000`。
- 所有业务写入都在**单个** `BEGIN IMMEDIATE` 事务内：
  1. 先读 `requests` 判断幂等（存在即按 1.6 处理）；
  2. 物化到期邀请（时间驱动，不属于请求效果）；
  3. `SAVEPOINT business` 内重读**全部**被引用的版本（成员代数、邀请代数/状态/到期、目标占用），执行效果；
  4. 失败则 `ROLLBACK TO SAVEPOINT business`，业务零改动；
  5. 无论成功失败，在同一外层事务里写 `requests` 回执与 `audits` 行，一次 `COMMIT`。
- 因此"邀请消费 + 成员变化 + 审计原因 + 请求回执"**原子**：要么全部可见，要么全部不可见。
- 事务外的那次只读预检**不参与决策**（其结论被丢弃）：这保证"预检后另一进程续发邀请、撤销成员、提交重叠批次"时，本请求在写锁内重读版本后得到正确结论，而不是依据过期预检。
- 请求在 `during_effects` 点被真实终止（`os._exit`，无栈展开、无 `finally`）时，SQLite 回滚未提交事务：业务表与回执均不出现。在 `after_commit` 点终止时，数据与回执都已落盘，重启后按回执原样返回。
- 并发批次由 `BEGIN IMMEDIATE` 串行化：后到者在锁内重读，发现源已被迁走 → 整批失败回滚，先到者完整生效。
- 业务失败回执（`ok=false, committed=false`）与业务状态严格区分：`requests.status` 为 `rejected`，失败不改变任何业务表。

### 表

`tenants` / `members` / `invites` / `requests`（回执与参数指纹，`status` 仅 `succeeded`/`rejected`）/ `audits` / `meta`。两个部分唯一索引在数据库层面兜底：
`members(tenant_id,email) WHERE status='active'` 与 `invites(tenant_id,email) WHERE status='pending'`。

---

## 3. CLI 与输入结构

### 3.1 命令

```
python -m solution --input cases.json --output result.json [--workdir DIR] [--keep-databases]
```

- 每个 case 用**独立的临时 SQLite 文件**重放，互不影响；`--workdir` 可指定位置。
- 退出码：所有 case 的断言都通过 → `0`；任一 case 有断言失败 → `1`（失败明细仍在输出文件的 `assertions` 里）。

### 3.2 cases.json

```jsonc
{
  "cases": [
    {
      "id": "03-two-way-swap-despite-both-seats-occupied",  // 唯一
      "why": "这个用例验证什么（README 承诺每条都写原因）",
      "start_at": "2026-01-01T00:00:00Z",   // 可选：虚拟时钟起点，给定后时间完全确定
      "ops": [
        {
          "id": 1,                          // 任意标识，仅用于阅读
          "op": "migrate.commit",            // 见 3.3
          "params": { "...": "..." },
          "request_id": "swap-1",            // 可选，缺省 "rid-<id>"；同租户内唯一
          "at": "2026-01-01T00:05:00Z"       // 可选，推进虚拟时钟（需 start_at）
        }
      ],
      "assertions": [
        {"path": "receipts.6.data.moves.0.target.member_id", "equals": "alice@t-b.m1"},
        {"path": "final_state.invites", "length": 2},
        {"path": "receipts.5.error.code", "in": ["TARGET_OCCUPIED"]},
        {"path": "final_state.requests", "exists": true}
      ]
    }
  ]
}
```

- 输入也接受单个 case 对象，或 case 的数组。
- 不写 `start_at` 时使用系统时钟（那时才需要相对时间，结果不可逐字节复现）。

### 3.3 操作

| op | params | 成功回执 `data` |
| --- | --- | --- |
| `tenant.create` | `tenant_id`, 可选 `name`, 可选 `reason` | `tenant_id, name, created_at` |
| `invite.issue` | `tenant_id, email, ttl_ms(>0)`, 可选 `reason` | `invite_id, tenant_id, email, invite_epoch, status, expires_at, superseded_previous` |
| `invite.revoke` | `tenant_id, email, invite_epoch`, 可选 `reason` | `invite_id, tenant_id, email, invite_epoch, status=revoked` |
| `member.add` | `tenant_id, email`, 可选 `reason` | `tenant_id, member_id, email, member_epoch=1, status=active` |
| `member.leave` | `tenant_id, member_id, member_epoch`, 可选 `reason` | `status=revoked, previous_member_epoch, member_epoch, email` |
| `member.revoke` | 同 `member.leave`（管理侧） | 同上 |
| `migrate.commit` | `request_tenant`（或首个源租户）, `moves[]`, 可选 `reason` | `reason, moves[{source{...}, target{...}}]` |

### 3.4 回执与断言路径

成功回执：

```json
{"ok": true, "committed": true, "op": "...", "tenant_id": "t-a", "request_id": "swap-1",
 "at": "2026-01-01T00:00:00Z", "data": {...}}
```

失败回执（业务状态未变）：

```json
{"ok": false, "committed": false, "op": "...", "tenant_id": "t-a", "request_id": "x",
 "at": "...", "error": {"code": "TARGET_OCCUPIED", "message": "...", "details": {...}}}
```

`result.json` 结构：`{"cases": [ {case_id, steps, receipts, final_state, assertions, passed} ], "passed": bool}`（只有一个 case 时直接是该对象）。

断言路径基于该 case 的文档：
- `receipts.N` = 第 N 条 op（从 0 起）的回执；`receipts.N.data...`、`receipts.N.error.code`。
- `final_state.members / invites / audits / requests / tenants`，数组按确定顺序排列；
  `final_state.invites[i].effective_status` 会按 `now` 渲染 `expired`，`status` 是落库值。
- `final_state.now` = 该 case 结束时的虚拟时钟。
- 操作符：`equals`、`in`、`length`、`exists`、`absent`。

---

## 4. 错误码

| code | 含义 | 作用域 |
| --- | --- | --- |
| `INVALID_EMAIL` | 邮箱非 ASCII / 结构不合法（无 `@`、多 `@`、空本地/域名、空标签、`-` 首尾） | 任意请求 |
| `INVALID_REQUEST` | 参数缺失/类型错、`moves` 为空、`ttl_ms<=0`、无法确定请求作用域 | 任意请求 |
| `TENANT_EXISTS` | `tenant.create` 遇到已存在租户 | 租户内 |
| `TENANT_NOT_FOUND` | 租户不存在 | 租户内 |
| `MEMBER_EXISTS` | 该租户已有同邮箱 active 成员 | 租户内 |
| `MEMBER_NOT_FOUND` | 成员不存在 | 租户内 |
| `MEMBER_NOT_ACTIVE` | 成员已 revoked（离开/迁出后） | 租户内 |
| `MEMBER_EPOCH_MISMATCH` | 成员代数过期 | 租户内 |
| `INVITE_NOT_FOUND` | 该 `(tenant, email, invite_epoch)` 不存在 | 目标租户 |
| `INVITE_NOT_PENDING` | 邀请已被消费/撤销/取代 | 目标租户 |
| `INVITE_EXPIRED` | 邀请已到期（`now >= expires_at`）；`invite.revoke` 与 `migrate.commit` 都可能返回 | 目标租户 |
| `DUPLICATE_SOURCE` | 批次内重复源成员（`first_index`/`again_index`） | 批次 |
| `DUPLICATE_TARGET` | 批次内重复目标绑定 | 批次 |
| `SAME_TENANT` | 迁移源与目标同租户 | 批次 |
| `TARGET_OCCUPIED` | 源全部移出后目标席位仍被他人占用（`occupant_member_id`） | 批次 |
| `REQUEST_ID_CONFLICT` | 同 ID 不同参数；同 ID 同参数会返回原回执而不是这个 | 租户内 |

批次拒绝时 `details.move_index` 指出第一个出错的 move。

审计动作：`tenant.created`、`member.added`、`member.left`、`member.revoked`、`invite.issued`、`invite.revoked`、`invite.expired`、`migration.source_revoked`、`invite.consumed`、`migration.target_joined`、`migration.committed`，以及任何被拒请求的 `<op>.rejected`（含 code 与被拒原因）。

---

## 5. 边界示例（均可在 cases.json 找到对应断言）

1. **恰好到期**（case 02）：同一时刻签发的两张邀请，`ttl_ms=60000`；`59.999s` 时 `invite.revoke` 成功；`60.000s` 时对另一张的同样请求得 `INVITE_EXPIRED`，且它落库为 `expired`。
2. **互换**（case 03）：同一地址在 t-a 与 t-b 各有一名 active 成员；批次让两人对调，于是**每个目标席位在批次开始时都被另一位源成员占用**。把占用判断放在"整批源移出后"批次成功（按单条 move 判断会误拒）；结束后两条旧成员 `revoked`（代数 2），两条新成员 active，两张邀请 `consumed`。
3. **目标被占且无源离开**（case 04）：`TARGET_OCCUPIED`，业务表零改动；邀请保持 `pending`，待占用者离开后同一张邀请在重试批次中成功（`carol@t-b.m2`）。
4. **重复源 / 重复目标**（case 05）：分别返回 `DUPLICATE_SOURCE` / `DUPLICATE_TARGET`，两张邀请都仍是 `pending`。
5. **失败请求可被后续批次复用**（case 07）：第一批因 `MEMBER_EPOCH_MISMATCH` 整体失败，`eve@example.com` 的邀请未被消费；第二批用它成功。
6. **同 ID 同参数重放**（case 06、tests/test_domain.py）：即使世界已经变了（例如成员已被迁走），重放仍返回**第一次**的 `ok=true` 回执与原始 `at`。

---

## 6. 测试

### 6.1 `python -m unittest discover -s tests -v`（44 个测试，约 7 秒）

- `tests/test_domain.py` —— 邀请代数/规范化/到期边界（`59.999s` 可用、`60.000s` 过期）；离开与代数；迁移成功、真互换、两类独立 move、各类拒绝、全无回滚；幂等重放（成功与失败）、跨租户同 ID、冲突、未知 op、无作用域请求。
- `tests/test_concurrency.py` —— 真实多进程，用同步点固定先后：
  - 两个进程提交**同一**批次（同 request_id）：回执一致，只产生一次 `migration.committed`，只消费一次，最终状态就是一次迁移的结果。
  - 注意：这里的"互换"是两条彼此独立的 move（分别从 t-a、t-b 出发到另一个租户的空闲席位）——**真正的同席位争用互换**在 `tests/test_domain.py::test_two_way_swap_is_accepted_even_though_each_target_is_occupied` 里，那里两边目标席位在批次开始时都被占用。
  - 进程 A 停在 `after_preflight` 后，进程 B 续发邀请 → A 在写锁内重读，得 `INVITE_NOT_PENDING`（若信任预检就会错误提交）。
  - 进程 A 停在 `after_preflight` 后，进程 B 撤销源成员 → A 得 `MEMBER_NOT_ACTIVE`。
  - 两个**重叠批次**：B 先提交（A 已预检但尚未拿写锁），A 后到在锁内重读，整批回滚，B 的结果完整保留。
  - 两个进程用同一 request_id 并发：同参数 → 相同回执且只建一次成员；异参数 → 一个成功、另一个 `REQUEST_ID_CONFLICT`，只建一次成员。
- `tests/test_crash.py` —— 用 `os._exit(90)` 真实终止**本题启动的子进程**：
  - `during_effects`（旧成员已移除但未提交）：重启后逐行读取 SQLite 与基线完全一致（`raw_rows` 直接读库，不经过被测代码），`receipt.json` 不存在，`mig-crash` 无回执行。
  - `after_commit`（已提交但未返回）：重启后状态为完整迁移；`response_json` 可直接取回，重发同请求返回**完全相同**的回执。同一测试还把 `members`、`invites` 逐行与手工写出的元组比对，并按顺序核对四条迁移相关审计。
  - `after_preflight`：基线不变。
  - 崩溃后换新请求重做同一迁移：成功。
- `tests/test_cases.py` —— cases.json 全部 11 个 case 的断言通过、case id 唯一、CLI 端到端产出、两次独立重放文档逐字节相同。
- `tests/support.py` —— 临时目录、同步点 `SyncGate`、`raw_rows` 基线读取、子进程启动/收尾（只 kill 自己启动的进程）。

所有期望值均由人工按规则写出：要么在 `assert` 里写常量（测试），要么在 cases.json 的 `assertions` 里写 `equals`。没有从实现输出反填期望值的逻辑。

### 6.2 实测结果（Windows 11, Python 3.12.8, 本机）

```
> python -m unittest discover -s tests -v
Ran 44 tests in 6.7s

OK

> python -m solution --input cases.json --output result.json
11 case(s) replayed; all assertions passed
```

另外做过一次 12 进程同时写同一库的手工压测（12 个 `member.add`，各自 request_id）：12 个进程全部退出码 0，12 条成员、12 条回执，无 SQLITE_BUSY 失败。

并发/崩溃测试共启动 14 个子进程（6 个并发场景 10 个 + 4 个崩溃场景），每个都由测试自己创建并在结束时确保回收；正常退出码 0，两个崩溃场景的退出码 90 是刻意的 `os._exit`。测试从不查找或终止任何非本题启动的进程。

---

## 7. 文件

```
solution/__init__.py     包入口
solution/__main__.py     CLI：case 重放、断言求值、结果文件
solution/directory.py    Directory（所有业务规则）+ Clock/Hooks（虚拟时钟、同步点、崩溃点）
solution/db.py           SQLite 模式、连接参数、写事务
solution/util.py         时间、邮箱规范化、稳定哈希、错误类型
solution/worker.py       子进程入口（崩溃/并发测试用）
cases.json               11 个可重放 case，含逐条说明与断言
tests/                   44 个自动化测试 + 共享支撑
```

## 8. 设计取舍

- **事务外预检不参与决策**：宁可多读一遍，也不让过期结论决定结果。预检只为固定交错与崩溃点服务。
- **保存点隔离业务与回执**：业务失败要回滚业务改动，但仍必须写下"这次请求失败过"的回执与审计（这是幂等契约的一部分）。到期物化刻意放在保存点之外，因为它是时间的结果而非请求的效果（被拒的请求不能"复活"已到期的邀请）。
- **互换的真实含义**：目标占用是"整批源移出之后"的谓词，所以一个批次里两条 move 可以互相为对方腾出席位；实现按 vacated 集合判定，而不是逐条 move 当场判定。
- **确定性 member_id**：让 cases.json 能在一个 case 内引用新成员（`alice@t-b.m1`），断言无需知道实现细节。
- **非 ASCII 直接拒绝**：比"猜一个 IDNA 规则"更符合"域名 ASCII 小写"的硬性表述。
