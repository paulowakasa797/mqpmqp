# AGENTS.md — Binance Futures Research and Simulation

## Working mode

Apply this section only to this repository. User instructions take precedence over these rules and skills. Read only the project files needed for the task: use architecture documentation for service boundaries, schema documentation for schema changes, and deployment documentation only when preparing a deployment. Do not require a full repository map or every project document for a small edit.

### Terra + Astra routing

- Terra high owns task understanding, scope, reproduction, the task package, integration, final verification, and handoff.
- Complete simple or low-risk changes directly with Terra.
- For a bounded difficult implementation, root-cause investigation, or critical design question, use at most one `gpt-6-astra` child at high reasoning effort. Give it an independent context (`fork_turns="none"` when available) containing only the target, relevant paths, verified facts, constraints, and acceptance criteria. Let it determine which cited project files it needs to inspect; do not force a full-repository context dump. The child must not spawn further agents.
- Astra works until the stated acceptance criteria are met, the relevant checks have run, and failures caused by its change are repaired, or until it reports a concrete blocker. Do not ask it to stop for review after a first implementation.
- Terra runs the final affected integration/regression checks. If a core failure is attributable to the child change, return the actual error and affected context to the same child for repair when available.
- If the requested model or independent context is unavailable, say so plainly and continue with the current agent; never claim a model switch or test result that did not occur.

### Safe execution and completion

- When project evidence confirms that a local test command uses disposable fixtures and has no production access, run the narrowest relevant checks, fix failures caused by the requested change, and rerun affected checks without pausing for approval. Do not assume this is true merely because a command is named “test”.
- Do not deploy, publish, send external messages, push/merge, change production data, or perform other irreversible external actions without explicit user approval.
- Report what changed, the actual verification performed and its result, plus any concrete blocker or remaining risk.

## 交易研究与模拟

本项目只做交易研究、回测和模拟账户决策，默认不执行真实下单、资金划转或账户设置修改。

所有市场结论先标明数据事实，再分别写技术分析、情绪/持仓证据、概率判断与交易假设。价格、新闻、资金费率、持仓量、主动成交等必须使用当前可核验数据；数据缺失、延迟或异常时明确写出，不能用旧数据伪装实时结论。

没有同时满足入场逻辑、失效条件、仓位风险和执行条件的优势时，结论就是 NO TRADE。若给出候选交易，必须含方向、触发价或触发条件、止损、分批止盈/失效条件、风险金额或仓位依据，并区分“等待条件”和“已触发”。

现有币安 USDⓈ-M 工作仅用于模拟/回测。若项目文件给出扫描频率、数据时效和筛选阈值，按该配置执行；缺少实时数据或策略验证不通过时，不补造信号。修改 VPS、策略部署、通知、API、真实仓位或任何外部服务前，先完成本地/只读排查，再征得明确授权。
