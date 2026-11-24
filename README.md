# escrowclock

`escrowclock` 是一个仅使用 Python 3.12 标准库和本地 SQLite 的非生产档案领取模拟。它覆盖：

- 申请后由多个**岗位不同、背后自然人也不同**的保管人依次签发；
- 签发和放行领取都必须落在岗位有效时间区间 `[valid_from, valid_to)` 内；
- 同一票批准可列多个对象，并用 `max_claims` 限制**总领取次数**；
- 用户取消、合规回收可与领取并发，SQLite 写事务给出唯一串行顺序；
- 新政策从其批准事件序列（sequence）起生效，不能反向取消已经生效的回收；
- 所有成功/失败请求的幂等键都持久化，失败结果也可重放；
- 规范化 JSON + SHA-256 哈希链回执，可从创世事件重放校验；
- 支持按业务事件时点 `--at` 和已知快照 `--snapshot` 审计；
- 内置纯内存小状态串行参考实现，并用随机交错 + 确定崩溃测试验证 SQLite 恢复后的状态等价；
- 所有身份均为合成 ID，不含真实人员、密钥或外部服务。

## 1. 运行环境与测试

Windows 10/11 + Python 3.12，不需要安装第三方包。

在仓库根目录运行全部 unittest：

```bash
python run_tests.py
```

或直接使用 unittest：

```bash
python -m unittest discover -s tests -v
```

单独运行随机故障恢复等价测试（默认 40 个固定随机种子）：

```bash
RANDOM_SEEDS=100 python -m unittest tests.test_recovery_random -v
```

Windows `cmd.exe` 设置种子数：

```cmd
set RANDOM_SEEDS=100
python -m unittest tests.test_recovery_random -v
```

测试包含真实子进程：

- 两个进程竞争最后一个领取名额，断言只有一笔 `ClaimTaken`；
- 两个进程真实并发执行 take/cancel、take/reclaim；
- 子进程以退出码 `86` 注入硬崩溃，再重放和幂等重试。

## 2. CLI 总览

无需安装，在仓库根目录使用：

```bash
python -m escrowclock init ...
python -m escrowclock issue ...
python -m escrowclock take ...
python -m escrowclock audit ...
```

也提供根目录便捷包装：

```bash
python issue.py ...
python take.py ...
python audit.py ...
```

可选安装后会生成 `escrowclock`、`issue`、`take`、`audit` 以及带前缀的 `escrowclock-issue`、`escrowclock-take`、`escrowclock-audit` 命令：

```bash
pip install -e .
```

所有正常 CLI 结果均向 stdout 输出 UTF-8 JSON。退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 成功，或返回已持久化的成功重放回执 |
| `2` | 业务拒绝；JSON 中含稳定错误码，失败幂等键也已保存 |
| `70` | 内部错误或重放完整性错误 |
| `86` | 按参数注入的确定性崩溃 |

时间参数 `--at`、`--valid-from`、`--valid-to` 是确定性整数逻辑时钟；岗位和授权窗口均为左闭右开区间。系统提交顺序使用 SQLite 事件 `seq`，与业务事件时点 `at` 分开。

## 3. 三分钟端到端示例

初始化 SQLite 并载入合成夹具：

```bash
python -m escrowclock init --db .runtime-tmp/demo.db --fixture fixtures/synthetic.json
```

夹具中的 `app_demo` 有三个有序岗位：`post_intake`、`post_review`、`post_release`。依次签发：

```bash
python -m escrowclock issue --db .runtime-tmp/demo.db --request-id demo-issue-1 --application-id app_demo --post-id post_intake --agent-id ag_ada_laptop --at 20

python -m escrowclock issue --db .runtime-tmp/demo.db --request-id demo-issue-2 --application-id app_demo --post-id post_review --agent-id ag_bob_desk --at 21

python -m escrowclock issue --db .runtime-tmp/demo.db --request-id demo-issue-3 --application-id app_demo --post-id post_release --agent-id ag_cara_hsm --at 22
```

最后一张签发票据生成 `apv_demo`，授权 `obj_profile_7001`、`obj_profile_7002`，总额度为 2。领取第一个对象：

```bash
python -m escrowclock take --db .runtime-tmp/demo.db --request-id demo-take-1 --approval-id apv_demo --claim-id claim_demo_1 --object-id obj_profile_7001 --agent-id ag_cara_hsm --recipient-agent-id ag_owner_portal --at 30
```

再次使用同一个 `--request-id demo-take-1` 会得到 `"replayed": true` 和完全相同的 `seq/event_hash/state_hash`，不会产生第二笔领取。

查看该领取的完整依据：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --claim-id claim_demo_1
```

重放从创世哈希开始的全部事件：

```bash
python -m escrowclock replay --db .runtime-tmp/demo.db
```

## 4. 两个进程竞争最后一次领取

合成夹具已经准备好完全签发、`max_claims=1` 的 `apv_race`。先初始化一个竞争库：

```bash
python -m escrowclock init --db .runtime-tmp/race.db --fixture fixtures/synthetic.json
```

打开两个终端，几乎同时执行不同幂等键的请求：

```bash
python -m escrowclock take --db .runtime-tmp/race.db --request-id process-a --approval-id apv_race --claim-id claim-a --object-id obj_last_slot --agent-id ag_cara_hsm --at 500
```

```bash
python -m escrowclock take --db .runtime-tmp/race.db --request-id process-b --approval-id apv_race --claim-id claim-b --object-id obj_last_slot --agent-id ag_cara_hsm --at 500
```

保证结果：

- 一个进程退出码 `0`；
- 另一个进程退出码 `2`，错误码为 `QUOTA_EXHAUSTED`；
- 事件流中只有一个 `ClaimTaken`；
- `request_records` 中两个幂等键都存在：一个 `applied`，一个 `rejected`。

实现使用 `BEGIN IMMEDIATE` 在读取小状态前获取 SQLite 写锁，并设置 30 秒 `busy_timeout`。因此两个进程不是“先各自读到剩余额度 1，再互相覆盖”，而是被 SQLite 串行化：后提交者一定会看到前者的领取和新额度。

如果两个进程使用**同一个** `--request-id` 和相同请求体，则二者都返回 `0` 和同一张回执，仍然只有一笔领取。

## 5. 崩溃注入与古今回执重放

写命令支持以下崩溃点（也可用环境变量 `ESCROWCLOCK_CRASH=after_commit` 注入）：

- `before_write` / `before-write`：开启事务和任何写操作之前；
- `before_row_write`：取得写事务、完成幂等检查之后，插入事件/请求记录之前；
- `after_write` / `after-write`：事件和幂等记录已插入，但尚未 `COMMIT`；硬崩溃后 SQLite 回滚这些行；
- `after_commit` / `after-commit`：事务已持久提交，但进程在打印/返回回执前崩溃；重试同一幂等键会重放已提交回执。

示例：

```bash
python -m escrowclock issue --db .runtime-tmp/crash.db --request-id crash-1 --application-id app_demo --post-id post_intake --agent-id ag_ada_laptop --at 50 --crash after-write
```

该命令输出崩溃标记并以 `86` 退出。由于尚未提交，事件和 `crash-1` 幂等键都不会存在。执行：

```bash
python -m escrowclock replay --db .runtime-tmp/crash.db
python -m escrowclock issue --db .runtime-tmp/crash.db --request-id crash-1 --application-id app_demo --post-id post_intake --agent-id ag_ada_laptop --at 50
```

第二次是首次真正提交。

若使用 `--crash after-commit`，崩溃后事件和幂等键已经持久化；用**完全相同的命令和 request-id** 去掉 `--crash` 重试，会得到 `"replayed": true` 以及相同序号和哈希。

每张回执包含：

```json
{
  "receipt_version": 1,
  "request_id": "...",
  "seq": 42,
  "event_hash": "...",
  "prev_hash": "...",
  "state_hash": "..."
}
```

可按请求键验证：

```bash
python -m escrowclock verify-receipt --db .runtime-tmp/demo.db --request-id demo-take-1
```

也可保存 stdout 后验证文件：

```bash
python -m escrowclock take ... > .runtime-tmp/receipt.json
python -m escrowclock verify-receipt --db .runtime-tmp/demo.db --receipt-file .runtime-tmp/receipt.json
```

`replay` 和 `verify-receipt` 都会从 `prev_hash = 000...0` 的创世状态重新折叠事件并复算 `state_hash`，因此旧回执和当前回执使用同一套规范格式，任何事件篡改都会导致 `REPLAY_INTEGRITY_ERROR`。

## 6. 审计：事件时点与已知快照

审计读取模型同时支持两个维度：

1. `--at T`：只看业务事件时点 `event_time <= T` 的事实；`event_time IS NULL` 的基础数据视为远古事实；
2. `--snapshot S`：只看系统批准/提交序列 `seq <= S` 时已经知道的事件。`S` 可以是数字序号、`snp-...` 快照 ID，或某张回执中的 `state_hash`。

二者组合表示“在已知快照 S 中，站在业务时点 T 能看到什么”。

### 6.1 谁依据什么领取

夹具中 `claim_spent_1` 发生在逻辑时间 210：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --claim-id claim_spent_1
```

返回的 `claims[0]` 包含：

- `who.release_agent_id` / `release_natural_person_id`：放行保管代理及背后自然人；
- `who.recipient_agent_id` / `recipient_natural_person_id`：领取对象；
- `custodian_chain.signatures`：每个前驱岗位、代理、自然人、时间、事件哈希；
- `custodian_chain.distinct_natural_person_ids`：不同自然人列表；
- `authorization`：批准对象、第几次领取、总额度；
- `policy_basis`：该笔领取时生效的政策及其批准序列。

时点对比：

```bash
# claim_count = 0
python -m escrowclock audit --db .runtime-tmp/demo.db --at 209 --claim-id claim_spent_1

# claim_count = 1
python -m escrowclock audit --db .runtime-tmp/demo.db --at 210 --claim-id claim_spent_1
```

快照对比：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --snapshot 1
python -m escrowclock snapshot --db .runtime-tmp/demo.db --snapshot-id snp-demo
python -m escrowclock audit --db .runtime-tmp/demo.db --snapshot snp-demo
```

常用过滤参数：`--claim-id`、`--approval-id`、`--application-id`、`--ticket-id`、`--object-id`、`--agent-id`、`--person-id`。加 `--include-events` 可同时输出事件流。

### 6.2 四类可区分的失败原因

审计解释器只做只读预演，不写幂等键：

**自然人重合：** `ag_ada_laptop` 与 `ag_ada_cli` 是不同代理，但背后都是 `np_ada_7002`：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --explain issue --application-id app_overlap --post-id post_overlap_demo --agent-id ag_ada_cli --at 402
```

主错误码：`NATURAL_PERSON_OVERLAP`，详情给出重合的前一签名。

**缺少前驱：** `app_demo` 的第一岗尚未签发就请求第三岗：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --explain issue --application-id app_demo --post-id post_release --agent-id ag_cara_hsm --at 15
```

错误码：`MISSING_PREDECESSOR`，详情给出当前 stage 和 `expected_post_id`。

**额度耗尽：**

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --explain take --approval-id apv_spent --claim-id explain-only --object-id obj_spent_profile --agent-id ag_cara_hsm --at 220
```

错误码：`QUOTA_EXHAUSTED`，并给出 `used_claims/max_claims`。

**授权过时：** 可使用已取消的 `apv_canceled` 或超过 `valid_to=600` 的 `apv_expired`：

```bash
python -m escrowclock audit --db .runtime-tmp/demo.db --explain take --approval-id apv_canceled --claim-id explain-only --object-id obj_canceled --agent-id ag_cara_hsm --at 320

python -m escrowclock audit --db .runtime-tmp/demo.db --explain take --approval-id apv_expired --claim-id explain-only --object-id obj_old_profile --agent-id ag_cara_hsm --at 700
```

错误码：`AUTHORIZATION_STALE`，`details.stale_reason` 进一步区分：

- `user_canceled`
- `compliance_reclaimed`
- `outside_validity_window`
- `policy_freeze`

岗位窗口或岗位代理人不正确时另有 `POST_WINDOW_INACTIVE`、`AGENT_NOT_POST_HOLDER`。

## 7. 取消、回收与政策序列

取消命令：

```bash
python -m escrowclock cancel --db .runtime-tmp/demo.db --request-id cancel-1 --approval-id apv_demo --person-id np_owner_7001 --at 1000
```

合规回收必须引用已批准政策：

```bash
python -m escrowclock reclaim --db .runtime-tmp/demo.db --request-id reclaim-1 --approval-id apv_demo --policy-id pol_baseline_v1 --officer-agent-id ag_dan_compliance --reason synthetic-hold --at 1001
```

并发语义由 SQLite 提交顺序决定：

- take 先提交：领取永久保留；之后的 cancel/reclaim 仍可记录，并保存 `claims_at_cancellation` 或 `claims_at_reclamation = 1`；
- cancel/reclaim 先提交：之后的 take 得到 `AUTHORIZATION_STALE`；
- 取消/回收不会删除或改写已经提交的 claim 事件。

政策中的 `approved_seq` 是批准政策的事件序号。若新政策效果为 `freeze`，则只有该政策之后序列中的新领取会被拒绝；之前的领取和审计依据不变。回收记录带有：

```json
{
  "policy_seq": 123,
  "effective_seq": 124,
  "irreversible": true
}
```

之后即使批准一个更宽松的新政策，也不能把该回收“反向取消”。显式尝试：

```bash
python -m escrowclock reverse-reclaim --db .runtime-tmp/demo.db --request-id reverse-attempt --approval-id apv_race
```

会返回并持久化 `RECLAMATION_IRREVERSIBLE`。

## 8. 幂等与故障恢复模型

SQLite 中有三张核心表：

- `events`：只追加的规范事件和哈希链；
- `request_records`：所有请求的幂等键、请求体哈希、状态、错误码、序号和完整 JSON 响应；**业务失败也插入并提交**；
- `snapshots`：可选的已知状态快照。

同一 `request_id`：

- 请求体哈希相同：返回持久化响应的副本，并标记 `"replayed": true`；
- 请求体哈希不同：返回 `IDEMPOTENCY_KEY_CONFLICT`，不会覆盖原记录。

`escrowclock/reference.py` 是无 SQLite 的小状态串行参考。`tests/test_recovery_random.py` 对每个固定随机种子：

1. 随机交错多个申请、签发、领取、取消、回收操作；
2. 随机注入提交前/提交后崩溃；
3. 崩溃后用新的 `Store` 连接重开 SQLite；
4. 按真实提交顺序驱动 `SerialReference`；
5. 比较每个 JSON 响应、最终完整状态、全部幂等记录、状态哈希和重放结果。

这验证的是**故障恢复等价**，而不是仅比较某个计数。

## 9. 合成夹具

`fixtures/synthetic.json` 包含：

- 5 个合成自然人；
- 6 个合成代理，其中 `ag_ada_laptop` 与 `ag_ada_cli` 故意共享同一自然人；
- 4 个岗位窗口；
- 基线政策；
- 可手动演示的 `app_demo`；
- 可直接竞争最后名额的 `apv_race`；
- 已额度耗尽的 `apv_spent`；
- 已用户取消的 `apv_canceled`；
- 自然人重合失败夹具 `app_overlap`；
- 授权窗口过期夹具 `app_expired`；
- 两条标记为 `expect_error` 的夹具命令，用来验证缺少前驱和自然人重合。

没有真实档案内容、真实身份、证书、令牌或密钥；哈希只用于完整性和确定性重放。

## 10. 代码结构

```text
escrowclock/
  canonical.py    # 规范化 JSON、SHA-256、创世哈希
  errors.py       # 稳定错误码和崩溃异常
  model.py        # 纯状态、事件折叠、业务规则、审计解释
  reference.py    # 小状态串行参考 + 内存幂等记录
  store.py        # SQLite 事件日志、幂等表、事务、崩溃点、快照
  audit.py        # 事件时点/快照审计与依据投影
  fixtures.py     # 合成夹具加载
  cli.py          # UTF-8 JSON CLI
fixtures/
  synthetic.json
tests/
  support.py
  test_domain_audit.py
  test_cli_process.py
  test_recovery_random.py
issue.py / take.py / audit.py
run_tests.py
```

运行期持久状态只写入通过 `--db` 指定的本地 SQLite 文件及其 SQLite 自身 WAL/SHM 附属文件。
