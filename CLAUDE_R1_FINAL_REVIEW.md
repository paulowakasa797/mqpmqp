# R1 候选独立审核结论

审核对象：`CLAUDE_REVIEW__1__b720.md`（R1 复盘/修复/待审候选材料）。
审核方式：只读。无 SSH、无联网交易、无 Telegram、无部署、无对候选源码的修改。
平台未提供完整 ZIP（三币原始 K 线、逐点 `decisions.jsonl`、裁剪账本、封存 backend 114 文件均不在本工作区）。按入口说明，对 Markdown 内嵌的完整源码、diff、测试与证据做独立审核；未实际执行的检查只把那些项标为 BLOCKED。

独立复算环境：本机 Python 3、从 Markdown 抽出的 `rules.py` / `market.py`、Decimal RR 与 tick 取整、窗口计数、政策哈希。抽出字节 SHA256 与材料声明完全一致：

| 文件 | 声明 SHA256 | 抽出后核对 |
|---|---|---|
| `candidate/rules.py` | `65bc967b152dc112b39276c7284712d99c4b8a96453cf95fc9b0951e10e80644` | 一致 |
| `candidate/market.py` | `9f3836eb74103db5426a1be9fc8278a3edcf6fbd1050c8d6683a1b45c319b509` | 一致 |
| `shadow_loop.py` | `4e1eff547bc3a856abb4e8d28b3901c74efd13a706d3785bced9f4d498f0237a` | 一致 |
| `signal_preflight.py` | `150d564e2113b6a3d343243390221c5f0db4a3a3b4895595aa22d2c9bdc4173c` | 一致 |
| `risk.py` | `f67bb14ad705fbfba837cb4fc2b5739b1f7855f42bcd86f148d9008fb212e9ea` | 一致 |

**总判：** 工程向内 tick 修复在源码层面看起来已关闭所陈述的止盈外扩缺陷；趋势改动在本窗口只把结构匹配从 2 增到 4，四个结构的零成本最有利 RR 上限全部远低于 2。这不是可交易信号恢复。收益 INCONCLUSIVE。部署 NO-GO。可作为冻结研究快照保留，不可当作“已恢复严格信号”的候选继续在本窗口调参。

---

## 范围与未执行项

已独立完成：

- 阅读完整 `rules.py` / `market.py` / `shadow_loop.py` / 封存 `risk.py` 与 `signal_preflight.py` / 两份 diff / 测试源 / 回放与评估脚本 / 报告、协议、内部复核、receipt、readback、summary、price bounds、evaluation、四条结构记录。
- 抽出 `rules.py` 后复跑趋势暂停、交叉/走平、不完整 15m、连续三根快线逆向、`closed_bars` 拒未来 K 线；多空均覆盖。
- 复算 4 个候选结构的零成本最有利 RR、入场区间是否装得下最低滑点、窗口 789×3=2367、漏斗 722→205=517、政策哈希、474.364 向内/外取整恒等。

BLOCKED（材料未提供原始字节，不能冒称已跑）：

- `verify.py --unittest`（`verify.py` 未内嵌）。
- `test_existing_contract.py` / 既有 `test_rules_market.py` 夹具 / 封存 backend 其余 112 个文件。
- `replay.py replay/evaluate`（无三币 `*_5m.json`、无 `decisions.jsonl`、无 `cycles_selected.jsonl`）。
- 2,202 条账本同口径复算、792 周期线上扫描、历史盘口/资金费率/深度 receipt。
- 现场 VPS 验收。`final_readback.json` 只能当作所附采集证据。

---

## ENGINEERING

**结论：所陈述的止盈外扩缺陷在 adapter 层看起来已关闭；封存 planner / 通知 / TimesFM 边界未被这次 diff 改动。工程测试声明 PASS，本审核未实际跑通，标 BLOCKED。**

### E1. 止盈外扩 → 向内 tick + 触及拒绝 + 合约核对（P2 已在源码层关闭）

- **严重性：** 原缺陷 P2；当前修复源码审查通过，执行测试 BLOCKED。
- **位置：** `candidate/market.py` 约 58–86 行；封存 `pump_short_testnet/risk.py` 246–248、326 行。
- **复现（独立 Decimal，不依赖夹具）：** 结构目标 `474.364`、tick `0.01`：
  - 封存 planner 对 LONG 止盈 `ROUND_CEILING` → `474.37`（外扩）。
  - adapter 先 `ROUND_FLOOR` → `474.36`；planner 再对已对齐值 `ROUND_CEILING` → 仍为 `474.36`。
  - 合约 `Decimal(plan['tp_price']) != target` 在恒等失败时会拒绝。
  - SHORT 镜像：`109.336` 向内 `CEILING` → `109.34`，planner 外向 `FLOOR` 后仍为 `109.34`。
- **影响：** 在 tick 可整除的正常 PRICE_FILTER 下，取整不能再把净 RR 从结构目标之下抬过 2。报告中合成样本净 RR `1.99986 → 2.00149` 依赖未内嵌夹具，**未独立复现该两个浮点 RR 数字**；外扩方向与“先向内则 planner 不再改变目标”已独立证实。
- **最小建议：** 保持 adapter 向内对齐 + `TARGET_ROUNDING_CONTRACT_VIOLATION`；不要改封存 planner 来“顺便”提高通过率。

触及检查使用 `closed_bars(...)` 的最后已收盘 K 线，再按 `open_ms` 回查原始 high/low，不读未收盘尾行。独立验证：把未收盘尾行 close/high 改成极大值，`closed_bars` 输出不变；额外未来 K 线触发 `UNEXPECTED_UNCLOSED_OR_FUTURE_KLINE`。合成 high=`121.66` / low=`109.34` 的完整 `evaluate_snapshot` 路径未跑（缺 backend 与 snapshot 夹具）。

### E2. 政策哈希与去重（通过）

- **严重性：** 信息。
- **位置：** `rules.py` 13–47、207、253 行。
- **复现：** 独立计算
  - 候选 `RULES_HASH = 4e5204f143c6b4cf514af2bb7e10c3bd802fd5917658da33f571ea7fbafafbd2`（与四条结构记录一致）
  - 基线（无新字段）`2b36694cce1338f71e0136f105887c39d5bb160ea5b70d047b7ff5c6d9bc1e0c`（与基线结构记录一致）
- `setup_id` 锚点含 `policy=RULES_HASH`，故同几何 SOL 结构 ID 从 `44830b73…`/`0e6802d6…` 变为 `42d71ea8…`/`323d027b…`。版本字符串 `3.0.0-r1.1`。数字阈值与基线字段逐项相同。
- **影响：** 不能与旧去重账本混用，这是正确行为。
- **最小建议：** 部署前必须清空或分版本隔离 `notified_setup_ids.json`。本轮不应部署。

### E3. RULES_HASH 只哈希参数字符串，不哈希 `regime()` 源码（P3 残余）

- **严重性：** P3（流程风险，非本 diff 回归）。
- **位置：** `rules.py` 47 行 vs 106–141 行。
- **复现：** 若只改 `if long_alignment and slow_rising` 而不改 `regime_policy` / `target_rounding_policy` 字符串，哈希与 setup_id 不变。
- **影响：** 误把逻辑改动当成同一政策去重。
- **最小建议：** 冻结时同时钉 `rules_source_sha256`（shadow_loop 已记录），或要求政策字符串与逻辑同步变更。

### E4. 原风险 / 通知 / TimesFM 边界（通过，就所附 diff 而言）

- **严重性：** 信息。
- **位置：** 唯一代码 diff 为 `rules.py` 与 `market.py`。`shadow_loop.py` 字节与 receipt/readback 均为 `4e1eff54…`。
- **复现：** `market.py` 无 TimesFM/Telegram 引用。`notify_selected` 仍只对 `selected`；`notify_watches` 明确不发 Telegram；TimesFM 失败只能 `BLOCKED`/`SKIP`，不能改 `evaluate_setup`。`preflight` / `plan_order` 的 2 倍净 RR、成本、流动性门槛仍在封存 backend。
- **影响：** 这次候选没有把观察模型或 watch 提升为入场。
- **最小建议：** 维持该边界。端到端 Telegram / TimesFM 桥未在本轮验收（材料已声明）。

### E5. 止损仍由封存 planner 向外取整（已接受残余，非本轮回归）

- **严重性：** 注记。
- **位置：** `risk.py` 246–248 行；`rules.py` 231–232 行 `max_stop_atr=2.0` 约束的是确认收盘价口径。
- **复现：** LONG 止损 `ROUND_FLOOR`、SHORT `ROUND_CEILING`，风险变大、RR 变差，**不能**帮助跨过 2。最终成交估到取整止损的距离可以超过 2 ATR。
- **影响：** 口径需写明；不是这次为了恢复信号而打开的口子。
- **最小建议：** 新研究方案若要改，必须单独立项，不要绑在本窗口趋势实验上。

---

## STRATEGY_LOGIC

**结论：多空在代码上镜像；快线斜率不再是否决条件，连续逆向仍可保持趋势。这是已冻结的设计风险，不是“只允许一根回调”。本窗口新增的两个 ETH 结构正是该假设的命中样本，但 RR 上限约 0.44，不能当作策略变好。**

### S1. 趋势 vs 回撤（实现与协议一致）

- **严重性：** 设计风险（已冻结，非缺陷）。
- **位置：** `rules.py` 106–141 行。
- **复现（已跑）：** `trend_pause` 多空两侧基线 `NEUTRAL`、候选保持原方向，且 `fast_momentum_direction` 与趋势相反、`fast_slope_is_entry_gate=False`。交叉/走平仍 `NEUTRAL`。不完整 15m 不改变结果。连续三根 15m 快线逆向（`regime_slope_bars=3` = 45 分钟慢线斜率）仍保持原趋势。
- **影响：** 快线连续多根逆向只要排列与慢线斜率同向，仍 LONG/SHORT；入场时机仍交给 5m 确认。协议写明不追加保持期，测试也锁了这一点。
- **最小建议：** 对外描述必须写成“排列 + 慢线斜率”；禁止写成“只滤一根回调”。

### S2. 多空对称（代码通过；本窗口实证缺空头）

- **严重性：** 信息。
- **位置：** `regime()` 两侧条件互斥；`evaluate_setup` `sign = ±1` 方向翻转；`market.py` LONG 向内 FLOOR / SHORT 向内 CEILING。
- **复现：** 趋势单测两侧均过。本窗口 4 个结构记录全部 LONG，**零个 SHORT 真实结构**。
- **影响：** 空头路径没有本窗口真实样本，只靠合成测试。
- **最小建议：** 下一冻结窗口必须预先包含空头活跃阶段，不能再只用这三天三币。

### S3. 未来数据（规则层通过）

- **严重性：** 信息。
- **位置：** `rules.py` `closed_bars` 66–95 行；`replay.py` `context_at` / 先写 decisions 再 `evaluate`。
- **复现：** 未收盘尾行不进入规则输入；`evaluate()` 只读已封印的 `decisions.jsonl` 再贴 30/60/240 分钟收盘标签，不再调用策略。
- **影响：** 结构判定路径不读未来。标签不是成交。
- **最小建议：** 保持两步分离。

### S4. 确认/目标几何很可能系统性吃掉 RR（研究问题，非实现 bug）

- **严重性：** 策略层 P2（可交易性），不是代码错误。
- **位置：** 回撤 25%–60%、确认收盘、目标=前高−0.1 ATR、止损=回撤低−0.2 ATR、`min_net_rr=2`。
- **复现：** 见 SIGNAL_RECOVERY。本窗口全部结构匹配的零成本最有利 RR ∈ (0.23, 0.45)。其中两条入场带宽甚至装不下报价+最低滑点。线上账本另有 `PRICE_ORDER_OR_ENTRY_RANGE=103`、`NET_RR_LT_MIN=9`，与“结构到了但公开预检过不了”一致。协议称双向合成样本仍能过完整预检——该夹具未在本环境运行。
- **影响：** 去掉快线斜率会放出更多“像结构”的回撤，但不自动变成 RR≥2 的单。不能把确认时机+向内目标说成本窗口失败的根因：这四条在取整之前就已经远低于 2。
- **最小建议：** 若继续研究，冻结新窗口与新假设，单独衡量“结构目标在真实回撤确认后是否经常先天 RR<2”。禁止在本窗口继续调 `retrace_*` / buffer / min_net_rr。

---

## REPLAY_REPRODUCIBILITY

**结论：回放设计（固定时钟、收盘 180 根、决定封印后再贴标签、不合成盘口）是对的。材料内部计数与哈希自洽。完整离线回放本环境 BLOCKED。历史严格公开预检 BLOCKED。**

### R1. 计数与窗口（独立复算通过）

- 窗口 `[2026-09-16T00:00:00+00:00, 2026-09-18T17:45:00+00:00)` → 每币 789 点，共 2367。
- 969 根 K 线 = 180 预热 + 789 决策；预热起点 `1789462800000` 比窗口早 180 根。
- 基线/候选拒绝计数各自加总均为 2367。
- `NO_DIRECTIONAL_REGIME` 722→205，差 517；`transitions` 全部从 `NO_DIRECTIONAL_REGIME` 出发，求和 517，其中 2 条 → `RULE_MATCH`，515 条仍被后续形态门槛拦住。
- 四条结构时间戳均在窗口内且对齐 5m：ETH 2026-09-16 19:05Z、ETH 09-18 06:05Z、SOL 09-17 07:05Z、SOL 09-18 03:50Z。
- 账本 2202 matched + 174 缺时钟 = 2376。材料声称 mismatches=[]，**未独立复跑**。

### R2. 回放本身 BLOCKED

- **严重性：** 范围限制，不是材料造假证据。
- **位置：** 入口命令依赖 ZIP 内 `evidence/*_5m.json`、`cycles_selected.jsonl`、`verify.py`。
- **复现：** 工作区只有本 Markdown。
- **影响：** 不能独立再生 `decisions_sha256=f4627e2a…`，也不能复核 2,202 条一致。
- **最小建议：** 若需第二审计员签字回放，必须提供完整包；不要用 SSH 替代数据包。

### R3. 历史 K 线事后下载

- `historical_first_availability=UNKNOWN_DOWNLOADED_LATER`、`original_input_archives_present=false`。同意：可用事后 K 线复算当时规则在这些收盘价上的输出，不能重建当时盘口、深度、资金费率或首次可交易时间。

---

## SIGNAL_RECOVERY

**结论：NOT_DEMONSTRATED。禁止把“结构 2→4”写成严格信号恢复。**

独立按 `assess_candidates.py` 公式、对四条候选结构复算（全部 LONG，最有利入场=entry_low，费用=0）：

| setup_id | 币 | 决策 | RR 上限 | 与材料一致 | 区间能否容纳报价+0.0005 滑点 | 能否达到要求 RR=2 |
|---|---|---|---:|---|---|---|
| `17349ad85708812ec8a1dba4221cad1a` | ETHUSDT | 1789585500000 | **0.44400420196851** | 是 | 是 | **否** |
| `41a40407913ab0f916e7d8ec790e827b` | ETHUSDT | 1789711500000 | **0.43157611799888** | 是 | 否 | **否** |
| `42d71ea8d6b8e645e356daf4fd9e9c47` | SOLUSDT | 1789628700000 | **0.37283621837556** | 是 | 否 | **否** |
| `323d027b14f7aca4d2f4f2d0f4f9fccd` | SOLUSDT | 1789703400000 | **0.23516237402013** | 是 | 是 | **否** |

非负费用、滑点、向外止损取整、向内止盈取整都只会降低（或至多不提高）该上限。两条新增 ETH 的 `fast_momentum_direction=SHORT` 而 `direction=LONG`，正好是去掉快线斜率后门禁放行的回撤；它们仍然不可交易。

两条原 SOL 结构几何未变，仅 setup_id 因政策哈希改变。线上首先记为 `PRICE_ORDER_OR_ENTRY_RANGE` 不能在缺盘口时归因到取整；本次能证明的是它们即使最有利也过不了 RR=2。

线上 `strict selected=0` 仍是所附账本事实，不是本审核的现场验收。

---

## RESEARCH_GAIN

**结论：INCONCLUSIVE。`strategy_pnl=null`。**

`final_evaluation.json` 是事后方向收盘变动，不是成交、不是止盈止损、不是组合盈亏。独立核对报告引用的 ETH 30 分钟标签：

- `17349ad8…`：−0.4424%
- `41a40407…`：−0.1674%
- 4 小时一负一正

SOL 标签为正也不能写成策略赚钱。零信号或结构数 2→4 都不是收益证据。

---

## DEPLOYMENT_GO_NO_GO

**NO-GO。NOT_AUTHORIZED / NOT_PERFORMED。**

依据：

1. 信号恢复未证明。
2. 收益未证明。
3. 历史严格公开预检缺原始盘口，BLOCKED。
4. 所附 `final_readback.json` 显示线上仍是基线字节：`rules.py=0c4a5f27…`、`market.py=b306a45b…`、`shadow_loop.py=4e1eff54…`。不得把这份 readback 说成本审核员自己 SSH 核对。
5. 工作树已有 65 个既有 tracked 改动/删除，候选也未 commit；即使将来要上，也必须单独干净提交，不能夹带数据删除。

---

## 是否保留为研究版

**保留为冻结研究快照：可以。保留为“已恢复严格信号”的活跃候选：不可以。**

- **应保留并带走的：** `STRUCTURE_INWARD_TICK_V1`（向内 tick、触及拒绝、planner 目标恒等）。这是正确性修复，不依赖本窗口有没有单。
- **应冻结、不要再宣传为修复的：** `EMA_ALIGNMENT_AND_SLOW_SLOPE_V1`。本窗口的证据是：它确实少拦了一些仍保持排列的回撤，但放出来的结构没有一条满足必要 RR 条件。
- **禁止：** 在已经看过这 2,367 个点结果的同一窗口上继续改阈值、加保持期、或放宽 RR。
- **若开新研究：** 先写新协议，换更长、含空头、且未用于选择假设的窗口；结构匹配与严格 selected 继续分列；收益默认 INCONCLUSIVE，直到有真实或事先规定的成交口径。

内部 Astra 复核回执与本次源码结论相容（P2 触及缺口已补、趋势不能写成“只一根”）。它不是本次审核，也不能替代未跑的 ZIP 回放。

---

## 审核员自检

| 检查 | 状态 |
|---|---|
| 趋势与回撤关系 | 已读源码并跑合成测试 |
| 多空对称 | 代码镜像；真实窗口无 SHORT 结构 |
| 未来数据 | `closed_bars` / 两步 evaluate 通过 |
| 政策 hash / 去重 | 哈希独立复算一致；SOL ID 变更符合预期 |
| 目标向内 tick / 已触及 / RR 不外扩 | 源码+Decimal 通过；夹具净 RR 数字未复现 |
| 风险/通知/TimesFM 边界 | diff 未改 shadow_loop 与封存 planner |
| 2→4 是否被当成可交易信号 | 否；材料本身也未声称。审核确认不得改口 |
| 四结构 RR<2 | 独立复算全部 <0.45 |
| unittest / 完整 replay | BLOCKED |
| 部署 | NO-GO |
| 收益 | INCONCLUSIVE |
