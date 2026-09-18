# REPORT_v3 — Drive 策略包离线终审

本报告审核 Google Drive 上传的 32 个文件（20 份源码 + 12 份 `__pycache__/*.pyc`，后者未纳入运行）。清单 `BUNDLE_MANIFEST.json` 声明的包名是 `CLAUDE_BREAKOUT_REVIEW_V3_20260919`。用户要求读取、审核并改进；未实盘、未连接生产、未在 2026-08-02 至 2026-09-01 UTC 窗口上调参或补造缺口。

## 最终裁决

```text
PACKAGE_INTEGRITY: BLOCKED
ENGINEERING: FAIL
DATA_CHAIN: BLOCKED
ARITHMETIC: BLOCKED
LEAKAGE_CONTROL: PASS
BREAKOUT_CANDIDATE: INCONCLUSIVE
GAIN_VS_ORIGINAL_R1: INCONCLUSIVE
DEPLOYMENT: NOT_AUTHORIZED
CLAUDE_REVIEW: COMPLETE
```

`DEPLOYMENT` 只能是 `NOT_AUTHORIZED` / `NO-GO`：本包没有生产授权，`production_allowed` 恒为 `false`。即使候选 runner 的失败关闭路径通过标准库测试，也不等于允许部署。

## 按严重级别排列的问题

### 1. 突袭包不完整（阻断终审复算）

- 文件：`BUNDLE_MANIFEST.json` 对照仓库根目录
- 可复现：清单 80 条路径中，导入后哈希匹配的源码为 19 个；Drive 未提供 `reports/breakout_continuation_20260919/` 下 49 个源文件（`engine.py`、`audit.py`、`run.py`、`protocol.json`、`freeze.json`、三币种 5m JSON/ZIP/CHECKSUM、`replay_02/decisions.jsonl`、`evaluation_01/trades_*.jsonl`、原 47 项 unittest 等）
- 影响：START_HERE 要求的 47 项引擎测试、522 笔独立复算、协议/冻结时间顺序、官方 USD-M CHECKSUM 链全部无法执行。不能把缺失写成“测试失败”，而是 **无法运行**
- 最小修复：重新上传完整包；在完整证据到位前保持 `PACKAGE_INTEGRITY: BLOCKED`。禁止用公开 REST 回填该窗口来冒充原包

### 2. 指标校验曾硬依赖 pandas/numpy（工程失败关闭被破坏）

- 文件：`position_data_recovery_gate.py` 原第 19–20 行；`research_acceleration/binance_comparison_data.py` 原 `validate_archive` / `collect()`
- 可复现：`python -B -m unittest tests.test_binance_comparison_data` 在未安装 pandas 时，3 项 metrics/collect 测试 `ModuleNotFoundError`
- 影响：README 声明本包只用标准库，但 metrics 归档校验会导入整个 recovery gate
- 最小修复（已做）：`METRICS_HEADER` 下放到 `binance_comparison_data.py`；`collect()` 不再预导入 recovery gate；pandas/numpy 改为可选导入，`main()` 缺依赖时明确报错

### 3. 可见快照未按 `received_at` 截断（潜在泄漏）

- 文件：`auto_trading/data.py` 原 `_visible` / `_record_error`
- 可复现：`tests.test_auto_trading_strategy.StrategyCausalityTests.test_future_received_bar_is_hidden_from_decision`
- 影响：`available_at <= as_of` 但 `received_at > as_of` 且非估计可用性时，决策仍可能看见该 K 线。策略层 `_completed` 会再滤一次，但 `validate_snapshot` 可能放行
- 最小修复（已做）：非 `availability_estimated` 记录要求 `received_at <= as_of`

### 4. 执行流对 K 线缺口静默跳过

- 文件：`auto_trading/execution.py` 原 `process_bar` 的 `processed_bars` 游标
- 可复现：`tests.test_auto_trading_execution.ExecutionTests.test_bar_gap_cancels_working_entry_and_does_not_fill`
- 影响：1m/5m 执行序列跳根后仍可能在后一根开盘成交，缺口内的止损不可见
- 最小修复（已做）：检测到 `start != previous + interval` 时记 `EXECUTION_BAR_GAP`，取消该品种工作单，不在缺口根上开仓；已有仓位仍按当前根做保护

### 5. 小 ATR 时固定 bps 成本可以压过结构风险

- 文件：`auto_trading/strategy.py` `assess_entry`；`auto_trading/profile.json` `fee_rate=0.0005`、`slippage_bps=5`、`stop_buffer_atr=0.1`，无 `min_stop_atr`
- 可复现：多空各一笔手算。LONG `entry=100`、`stop=99.95`、`targets=[101,102,103]`：`stop_cost=(100+99.95)*0.0005 + 99.95*0.0005 + 100*0.0003 ≈ 0.1799`，结构距离仅 0.05，成本 > 结构风险。SHORT `entry=100`、`stop=100.05` 对称。这不是单位错误，是小 ATR 止损与固定 bps 的真实组合
- 影响：若仍按净 RR 放行，1R 会被手续费定义，而不是结构止损
- 最小修复（已做）：`stop_cost >= abs(entry-stop)` 时拒绝 `COST_DOMINATES_STRUCTURAL_RISK`。未改 `profile.json` 参数，避免在已看窗口上调参

### 6. 原 R1 与突袭方案无法比收益

- 文件：`reports/pump_r1_review_20260919/replay.py` 第 112–113 行已写明 `historical_public_preflight='BLOCKED_MISSING_ORIGINAL_QUOTES_AND_RECEIPTS'`；候选 `auto_trading` 同样没有该窗口的盘口 receipt
- 可复现：包内无 `replay_02`、无 evaluation jsonl、无 R1 市场 JSON
- 影响：同窗结构匹配不能升级为严格成交或 A/B 收益。`GAIN_VS_ORIGINAL_R1` 必须保持 `INCONCLUSIVE`

## 必审问题（在现有源码上）

1. **因果与泄漏**：`confirmed_pivots` 只在右翼闭合后确认；`_strong_break` 用 `rows[:-1]` 与 `trigger["period_start"]` 排除当前突破 K 线。R1 `closed_bars` / `context_at` 丢弃未收盘与未来 K 线。已补 `received_at` 截断。
2. **配置冻结**：`protocol.json` / `freeze.json` 缺失，无法证明下载前封存。限制：只能证明当前 `profile.json` 哈希为 `0a0da535…e09f`，不能证明时间顺序。
3. **数据**：三币种官方 5m 档案与 CHECKSUM 缺失。`binance_comparison_data` 对 Vision ZIP 做成员名/校验和/OHLC 检查，且不把源时间写成历史 `available_at`。
4. **成交假设**：候选执行用盘口加减滑点，止损先于止盈；时间/失效退出在后续开盘。缺口处理已加强。没有 522 笔账本可重建。
5. **成本**：双边 5bps 手续费 + 出场 5bps 滑点 + 入场资金费预留。2 倍杠杆在 `max_leverage=3` 的账户约束里，不是 START_HERE 突袭引擎的 2x 成本情景（该情景的 jsonl 不在包内）。K 线估价未被称作真实成交。
6. **账本**：无 `evaluation_01/trades_*.jsonl`，不能独立重建 522 笔。
7. **汇总**：无按币种/方向/周的官方汇总可复核。两成本情景是否同一批 522 事件无法验证。
8. **审计器负向能力**：原 `audit.py` 不在包内。本仓库用标准库测试覆盖：重复 signal、未知 regime、paper 无报告、live 环境开关、路径逃逸、checksum 失败、zip 穿越。
9. **原 R1**：规则哈希绑定在 `rules.py`；`RULE_MATCH` 的 `execution_authorized=False`。无历史盘口则严格成交保持 BLOCKED。
10. **结论边界**：固定突袭方案在本窗口三币平均净 R 为负 **无法在本包验证**。部署 `NO-GO`；相对原策略增益 `INCONCLUSIVE`；生产 `NOT_AUTHORIZED`。

## 改进摘要（相对导入快照）

- `contracts.load_profile`：拒绝 `production_allowed is not False`
- `data.visible_snapshot` / `validate_snapshot`：决策时刻之后收到的非估计记录不可见
- `execution.process_bar`：执行缺口失败关闭
- `strategy.assess_entry`：成本压过结构风险则拒绝
- `feature_engineering`：非正周期返回空值，不编造
- `binance_comparison_data`：去掉 pandas 导入链
- 新增标准库测试 29 项（连同原比较数据测试共 50 项，退出码 0）
- 不修改 `profile.json` 研究参数，不填 2026-08 窗口

## 实际运行

```text
python 3.12.3
python -B -m unittest discover -s tests -v
Ran 50 tests in 0.241s
OK
```

```text
python -B -m unittest -v reports.breakout_continuation_20260919.test_engine
exit 1 — ModuleNotFoundError（模块不在 Drive 上传中，不是断言失败）
```

`python -m auto_trading paper --run paper-refused` 返回 `RESEARCH_GATE_REFUSED`，`production_allowed: false`。
