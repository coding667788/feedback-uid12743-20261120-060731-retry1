# 本地权限回收协调器（permission reclamation coordinator）

纯 Python 3.12 标准库 + SQLite（CPU、离线、无第三方依赖）实现的本地权限
协调器：按对象代数、基线权限、有有效期的临时授予（租约）和属性引用计算
**目标权限**，生成**计划**，并在应用时重新校验规划依据，保证不会出现半份
计划、重复回执或依赖不一致的权限状态。

## 1. 运行方式

```bash
# 回放随包提供的用例集，写出 result.json
python -m solution --input cases.json --output result.json

# 全部自动化测试（65 项）
python -m unittest discover -s tests -v
```

可选参数：`--workdir DIR` 指定用例数据库目录。默认是 `result.json` 同级的
`work/` 目录；每次运行开始前都会删除该用例同名数据库（含 `-wal`/`-shm`），
因此**同一输入重复运行输出逐字节一致**。

## 2. 状态模型

| 概念 | 含义 |
| --- | --- |
| `object` | 被授权对象。有 `generation`（只在“重建”时 +1）、`object_version`（任何状态变更 +1）、`baseline`、`attributes`、`materialized`（当前实际持有的权限）|
| `lease` | 临时授予。`[start, end)` 半开区间；可带属性引用 `attributes`；`revoked` 后立即失效；`generation` 必须等于对象当前代数 |
| `dependency` | 权限依赖 `(from_object, from_permission) → (to_object, to_permission)`，即“前者需要后者作为前置权限”；整体必须是 DAG |
| `plan` | 指定时刻 `planned_at` 计算出的调节计划，附带 `basis`（规划时读到的对象/租约/依赖版本）|
| `receipt` | `(tenant, request_id)` 的幂等回执，包含成功与业务失败 |

**目标权限** = 基线 ∪ 有效租约授权，再反复剔除“前置权限不被保留”的权限，
直到不动点（最大不动点）。依赖只**约束**、不授予：`app.write` 依赖
`db.read` 只是说明没有 `db.read` 就不能保留 `app.write`。

有效租约的判定顺序（任一不满足即失效）：

1. 未撤销（`revoked = false`）；
2. 租约代数 = 对象当前代数（对象重建后旧租约全部失效）；
3. `start <= t < end`（**恰好等于 `end` 时已失效**）；
4. 所有属性引用 `attributes[k] == object.attributes[k]`。

**来源逐项解释**：`object.get` / `plan.create` / `state.export` 的
`explanation[permission].sources` 会为同一权限的每个来源各列一条：
基线一条，每个生效租约一条；已失效的租约也会列出，但带
`"active": false` 和 `inactive_reason`（`expired` / `revoked` /
`object_recreated` / `not_started` / `attribute_mismatch` /
`object_absent`）。权限未被保留时另有 `reason`
（`no_active_source` 或 `missing_prerequisite`）和
`missing_prerequisites` 列表。

## 3. 计划与排序

- 计划记录 `scope`（请求对象）与 `planned_objects`。为保证结果一致，规划会把
  `scope` 扩展到**依赖连通分量**（双向）：向下游扩展，使撤销上游权限时下游
  也同批撤掉；向上游扩展，使“新增某权限但其前置尚未落地”时前置能在同一
  计划中先加。与 `scope` 不连通的任何对象都不进入 basis，因此无关变更
  不会使计划 stale。
- `removals`：**先撤下游再撤上游**（依赖图的逆拓扑序，同层按对象/权限名排序）。
- `additions`：**先加上游再加下游**（拓扑序）。
- 应用顺序严格按计划保存的顺序执行；每次删除前还会断言“没有仍被保留的
  下游权限引用它”，否则返回 `ordering_violation` 并整体回滚。
- 单次计划最多调节 **50 个对象**，判定口径是**实际会被调节（变更）的对象数**，
  不是被读取的作用域大小：60 个对象全部无差异的计划是合法的空操作；51 个对象
  需要变更则返回 `plan_limit_exceeded`（`detail.changed_objects`）。

## 4. 应用时的重新校验（stale）

`plan.apply` 在**同一个写事务**内重新验证：

1. `basis.objects` 中每个对象：不存在→`object_missing`；代数变化→
   `object_recreated`；对象版本变化→`object_changed`。
2. `basis.leases`：版本/撤销状态变化→`lease_changed`；消失→`lease_missing`；
   新增→`lease_added`。
3. `basis.dependencies`：版本/删除状态变化→`dependency_changed`；消失/新增
   同理由。只有**依赖方在 basis 内**的边被记录，因此从无关对象指向本对象的
   依赖变化不会使计划失效。
4. 结构未变时，用**事务内时刻** `applied_at` 重新计算目标；与计划目标不一致
   →`target_changed`（若因某租约恰好到期导致，会附 `expired_leases`）。

任一项失败即返回 `stale`，`detail.applied = false`，**不写入任何对象、
不写 apply 事件、不写 plan_application 记录**；只按幂等规则持久化这次
业务失败的回执。

> “不相关对象变更允许应用”：因为 basis 只包含 `scope` + 前置闭包的对象，
> 以及依赖方在 basis 内的依赖边，`other` 对象或无关租约的变化不会进入校验。

应用成功后：写入各对象的 `materialized`、追加 `apply_events`（含 `seq` 的
跨对象先后证据）、为每个被触碰对象 `object_version + 1`（使重叠计划自然
stale），并在同一事务写 `plan_applications`。第二次应用同一计划返回
`plan_already_applied`（永不重复应用）。

## 5. 输入结构

`cases.json`：

```json
{
  "cases": [
    {
      "name": "cascade-and-multi-source",
      "db": "case1.sqlite3",
      "requests": [ { "tenant": "t", "request_id": "c1-01", "op": "object.create", "...": "..." } ],
      "expect":   [ { "ok": true } ]
    }
  ]
}
```

`expect` 与 `requests` 一一对应，`tests/test_cli.py` 会逐条核对：

- `{"ok": true}` / `{"ok": false, "code": "..."}`
- `equals`：点号路径必须精确相等，列表用下标，如
  `"detail.reasons.0.kind"`、`"events.0.object_id"`。
- `contains`：点号路径的值必须包含该元素。

请求公共字段：`tenant`、`request_id`、`op`；其余字段按下表。所有时刻为
RFC 3339 UTC，必须带 `Z`，最多 6 位小数（微秒）。

| `op` | 字段 |
| --- | --- |
| `object.create` | `object_id, at, baseline?, attributes?` |
| `object.recreate` | `object_id, at, baseline?, attributes?`（代数 +1，旧租约失效）|
| `object.update` | `object_id, at, baseline?, attributes?`（只改声明，不改当前持有）|
| `object.delete` | `object_id, at`（删除会以版本 +1 撤销全部关联依赖，返回 `retired_dependencies`）|
| `object.get` | `object_id, at` |
| `lease.grant` | `lease_id, object_id, permissions, start, end, at, attributes?` |
| `lease.revoke` | `lease_id, at` |
| `dependency.add` / `dependency.remove` | `from_object, from_permission, to_object, to_permission, at` |
| `plan.create` | `planned_at, objects?`（省略则全部对象）|
| `plan.apply` | `plan_id, applied_at` |
| `state.export` | `at` |

`baseline` / `permissions` 为权限字符串数组（不得重复）；
`attributes` 为字符串/数字/布尔标量映射。

## 6. 错误码

| 错误码 | 场景 | 是否写回执 |
| --- | --- | --- |
| `invalid_request` | 缺少/非法 `tenant`、`request_id` | 否（无可靠请求身份）|
| `unknown_op` | 未知 `op` | 是 |
| `invalid_argument` | 字段缺失、多余字段、时刻格式、重复权限、租约区间非正 | 是 |
| `not_found` | 对象/租约/依赖/计划不存在 | 是 |
| `already_exists` | 重复创建对象、重复租约、重复依赖、重复撤销 | 是 |
| `dependency_cycle` | 依赖会形成环 | 是 |
| `plan_limit_exceeded` | 一次计划超过 50 个对象 | 是 |
| `stale` | 规划依据变化 / 事务内目标变化 | 是 |
| `plan_already_applied` | 计划已应用过 | 是 |
| `ordering_violation` | 删除仍被保留下游依赖的权限（防御性断言）| 是 |
| `request_id_conflict` | 同租户同 `request_id`、不同参数 | 否（保留第一次结果）|

**业务失败 ≠ 业务状态**：失败回执的 `ok=false` + `code` 与状态区分，
`state.export.receipts` 中同时记录 `ok` 和 `code`；失败不会改变
`object_version` 或 `materialized`。

## 7. 幂等

- `request_id` 在同一 `tenant` 内唯一；不同租户互不影响。
- 同 ID 同参数：返回**已持久化的第一次结果**（包括业务失败），不重复执行。
- 同 ID 异参数：`request_id_conflict`，且不产生任何状态变更、不覆盖第一次回执。
- 参数比较基于规范化 JSON（去掉 `request_id`）的 SHA-256。

## 8. 并发与中断

- 每个写请求使用 `BEGIN IMMEDIATE`，跨进程串行化；WAL + `busy_timeout`。
  打开数据库不再取写锁，因此两个进程可同时启动。
- 重校验、写入、回执、`plan_applications` 全在**同一事务**中：进程在事务中
  被真实终止（`terminate()`）后，重启看到的数据库既没有半份计划，也没有
  回执；未被中断的请求则回执与状态同时存在。
- 测试用同步点固定先后，而非 sleep：
  `plan_apply.revalidated`（事务内重校验后）、`plan_apply.written`（全部写后、
  提交前）、`before:<op>`（进入事务前）。崩溃点用 `SOLUTION_CRASH_AT`：
  `request_before_commit`、`request_after_commit`、`apply_after_revalidate`、
  `apply_after_writes`。
- 仅终止测试自己 `subprocess.Popen` 启动的子进程。

## 9. 边界示例

**恰好到期**（`exact-expiry-and-overlap`）：租约 `[00:00:00, 01:00:00)`。

- `00:59:59.999999` → 生效；`00:59:59` → 生效；`01:00:00` → 失效。
- 权限同时由基线与该租约提供时，租约到期后权限**仍保留**（并集），
  解释中该租约显示 `active: false, inactive_reason: "expired"`。

**过期导致 stale**（`stale-on-expiry`）：在租约到期前规划“新增该权限”，
在恰好到期时刻应用 → `stale`，原因 `target_changed` 且带
`expired_leases: [{"lease_id": "L", "end": "..."}]`，`materialized` 不变。

**对象重建**（`stale-on-recreate`）：规划后 `object.recreate` → 应用返回
`stale`，原因 `object_recreated`。

**级联撤销**（`cascade-and-multi-source`）：撤销 `db.read` 后，`app.write`
因缺前置也必须撤销，计划顺序为 `app` 先、`db` 后。

**计划上限**（`plan-limit`）：51 个对象各有权限被移除 → `plan_limit_exceeded`
（`changed_objects = 51, limit = 50`）。

## 10. 实测结果（本机 Windows 11 / Python 3.12.8 / SQLite 3.45.3）

```
$ python -m unittest discover -s tests -v
Ran 65 tests in 9.032s
OK
```

- 完整测试运行墙钟：8.2 s / 8.4 s / 9.0 s（中位数约 8.4 s，含全部进程与
  终止测试）。
- `python -m solution --input cases.json --output result.json`：约 0.50 s
  （8 个用例、144 个请求）。
- 连续两次运行到不同输出文件，`cmp` 逐字节相同（REPLAY-IDENTICAL）。
- 并发/终止测试本机实测约 2 s，无睡眠等待。

测试覆盖（65 项）：

| 文件 | 内容 |
| --- | --- |
| `tests/test_semantics.py` | 并集、DAG 剪枝、传递失效、环拒绝、多来源逐项解释、属性引用、重建失效、半开区间、撤销 |
| `tests/test_planning.py` | stale 各原因（对象变更/重建/缺失、依赖改写、租约新增、恰好到期）、无关变更可应用、重复应用、回执、50 对象上限（按变更对象计数）、租户隔离、上游前置同批加入、删除对象撤销依赖 |
| `tests/test_reference_model.py` | 与独立参考模型（`tests/reference.py`，递归前置判定、不同算法）做 40 组随机图 × 6 时刻的差分，并直接断言依赖一致性 |
| `tests/test_idempotency.py` | 同 ID 同参/异参、失败回执重放、跨重启、校验与错误码 |
| `tests/test_concurrency.py` | 两进程固定交错、真实终止（事务前/写后提交前/提交后）、未提交中断按重启数据库判定、同请求 ID 竞争 |
| `tests/test_cli.py` | cases.json 可重放、逐条期望核对、错误输入退出码 |

**期望值来源**：语义与计划测试的期望值来自显式断言（人工按定义推导）；
随机图测试的期望值来自 `tests/reference.py` 中独立实现的递归不动点算法，
不调用被测实现；`cases.json` 的 `expect` 字段在 `tools/make_cases.py` 中显式书写，
并经 CLI 复核。测试从不从被测实现反推期望值。

## 11. 目录

```
solution/        # 被测实现（core, store, clock, cli, syncpoint, gates, crashpoint）
tests/           # 65 项自动化测试 + 独立参考模型 + 子进程 worker
tools/make_cases.py  # 生成 cases.json（期望值出处可审阅）
cases.json       # 可重放用例集（含断言）
```
