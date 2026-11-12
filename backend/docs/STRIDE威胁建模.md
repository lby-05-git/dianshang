# STRIDE 威胁建模（多Agent 客诉舆情退赔决策系统）

> 关联 ：零信任与安全（-安全）· 本文为 V3-3 交付物之一。
> 基线：V2.0 已验收 + V3-1/V3-2/V3-3 增补项。全部结论以代码与测试为证，不虚构。

本文用微软 STRIDE 维度对系统做威胁建模，将每一类威胁映射到本仓库**已落地**的防线
（含拒绝路径与例外），并给出红蓝攻击语料规模与定期安全回归节奏。

---

## 0. 资产与信任边界

**核心资产（Cold 风险顺序）：**

| 资产 | 示例 | 破坏后果 |
| --- | --- | --- |
| 退款执行（资金面） | `execute_refund` / refund_provider | 错误自动退款、拒绝后退款、重复退款、绕过审批 = **0**（硬指标） |
| 个人数据（PII） | 手机号/身份证/银行卡/密钥/邮箱/姓名 | 明文入库、输出泄露 |
| 企业/用户身份凭证 | API_KEY / 角色 / 会话 Token | 越权退款、横向提权 |
| 模型与意图决策 | Prompt / 分类器 / 审核结论 | 注入越权行为 |
| 审计与版本痕迹 | domain_events / security_decisions / version_registry | 抵赖（Repudiation） |

**信任边界：**
1. 外部互联网 → 输入网关（SecurityGateway / NER-DLP / 鉴权）。
2. 输入网关 → Agent 编排 / 状态机 / 退款工具。
3. Agent → 外部 Provider（退款/通知/Ocr 等）。
4. 运营终端 → 人工审核 / 版本管理（RBAC leader+）。
5. 内部服务间（评测/重放/陪练沙箱）与生产数据**物理隔离**。

---

## 1. STRIDE 逐维映射

### S —— Spoofing（伪造身份/来源）

| 威胁 | 既有防线 | 拒绝/降级路径 | 验证落点 |
| --- | --- | --- | --- |
| 越权调用退款 API | RBAC `get_current_user` + `require_min_role`；退款必走工具门禁，不允许直接账号调 `refund` | 401/403 直接拒绝 | `test_api.py` RBAC 用例；`red_blue` 越权退款 3 场景强拒 |
| 伪身份要求放行（"我是系统管理员/客户主管"） | `security_gateway.diagnose` 伪身份规则 → BLOCK/REVIEW | 拒绝或转人工，绝不 PASS | 红蓝 118（伪身份变种） |
| 伪造 API_KEY | API_KEY 白名单/正则校验 | 鉴权失败拒绝 | Phase 3 攻击集（含 `\b` 汉字坑修复） |
| 伪造订单归属 / 商品归属 | `order_lookup` + 三核验 MATCH（order/product/price） | MISMATCH → 拒绝退款 | `test_tool_gate_rejects_verification_mismatch` |
| prompt 提示模型"角色冒用"（你被编程设定成客服主管） | `LLM-Critic` 角色冒用检测 + `_RE_ROLE_ID` B 类正则 | REVIEW | 红蓝加固缺口回归 |

### T —— Tampering（篡改数据/状态/策略）

| 威胁 | 既有防线 | 拒绝/降级路径 | 验证落点 |
| --- | --- | --- | --- |
| 篡改 case 状态伪造 RUNNING/SUSPENDED | tool_gate 仅 RUNNING 可退 | 状态非法拒绝 | `test_tool_gate_rejects_forged_status` |
| 篡改审批决策伪 APPROVED | version/审核字段由系统写入；决策仅 `routes_refunds._decide` 可改 | 决策非 APPROVED 拒绝退款 | `test_tool_gate_rejects_unapproved_decision` |
| 篡改策略/模板版本 | `version_registry` 内容 sha256 指纹 + DRAFT/APPROVED 流转 + 批准留痕 | 未批准版本不生效；扩散写一次留 One Time | `test_version_center.py` 8 项 |
| SQL/NoSQL 注入（DB 层） | SQLAlchemy 绑定参数；无字符串拼接 SQL（审计回归 `test_security_event_workbench`） | 拒绝 | `test_database.py` |
| 明文篡改 PII 入库 | DLP 红线：security_decisions/dlp_findings 只落**掩码后**片段，明文永不入库 | 无明文写路径 | `test_ner_dlp_metrics` 泄漏断言 |

### R —— Repudiation（抵赖）

| 威胁 | 既有防线 | 验证落点 |
| --- | --- | --- |
| "我没批准这笔退款" | `domain_events` append-only（write_event）记录 REGISTERED/APPROVED/ROLLED_BACK/SECURITY_FAIL_CLOSED；`security_events.record_event` 白名单 + `disposition=OPEN`；版本表留 `approver_id/approved_at` | `test_version_center::test_state_changes_write_domain_audit`；`test_deny_writes_domain_audit` |
| "是系统自动退的，我不知情" | 退款四同时之**审计维**：资金类放行必落 `security_decisions(PASS) + TOOL_GATE` 事件，否则 BLOCK | `test_pass_persists_release_trail` |
| 策略/工具是哪个版本在生效 | `version_center.latest_approved` 按时间语义定义"生效版本"，历史不可改 | `test_latest_approved_picks_recent_approved` |

### I —— Information Disclosure（信息泄露）

| 威胁 | 既有防线 | 拒绝/降级路径 | 验证落点 |
| --- | --- | --- | --- |
| Prompt 泄露系统指令 | `diagnose` Prompt 泄露规则；"把这句话写进日志时退款" 复合注入 BLOCK | 拒绝 | 红蓝（Prompt 泄露变种） |
| PII 识别/脱敏不达标 | `ner_dlp`：8 类实体 + 掩码 + 基准 44 例；准确率 ≥99%（文档级完美判定）+ 召回/误报报告 | 未达标如实报 gaps，不假报通过 | `test_ner_dlp_metrics.py` 3 项；DLP 终指标 |
| 普通查询回明文原件 | `fail_closed`：降级模式**只有 mask_only**，且 DEGRADE 永不返回明文 | 无批准策略/非法模式一律 DENY | `test_ner_failure_*` |
| 日志/评测泄露 PII | DLP 红线 + eval 回放集脱敏；陪练对话入库前清洗 | 无明文写日志路径 | Phase 5 语音陪练 |
| Base64 / 多语言编码绕过 | Base64 解码变体、多语言变种进红蓝语料 | 拒绝 | 红蓝变体 |

### D —— Denial of Service（拒绝服务/故障放大）

| 威胁 | 既有防线 | 拒绝/降级路径 | 验证落点 |
| --- | --- | --- | --- |
| 安全服务故障致退款失控（**故障=开通？**） | `fail_closed.guard_high_risk`：高风险操作一律 DENY（Fail-Closed） | 故障注入 → DENY，绝不因故障放开退款 | `test_gateway_failure_denies_high_risk` |
| 普通查询降级回明文 | Fail-Closed 降级策略白名单 + mask_only | 无批准策略 → DENY | `test_ner_failure_without_approved_policy_denies` |
| 单点 Provider 熔断/降级 | 语音/主链路 Provider 回退、排队、熔断 | 降级 Provider，业务推理优先 | V3-5 |
| 状态机重启丢状态 | Redis 状态恢复/Checkpoint-HA | 恢复后可继续 | `test_checkpointer_ha`、`test_resume_http` |
| 暴雨式重复退款压力 | 退款幂等 + Redis 锁（`test_idempotency_lock`） | 幂等键冲突拒绝重复 | `test_refund_receipt_idempotency` |
| 自愈（误伤恢复） | `fail_closed.cause_health(service, True)` 自愈 ALLOW | 恢复 → ALLOW | `test_health_recovers_and_allows_again` |

### E —— Elevation of Privilege（提权/越权）

| 威胁 | 既有防线 | 拒绝/降级路径 | 验证落点 |
| --- | --- | --- | --- |
| 客服提权审批退款 | RBAC：退款审批=leader+；版本管理=leader+；`require_min_role(ROLE_LEADER)` | 403/无审批路径 | `test_api_versions_rbac`（cs 403 / leader 通过） |
| 工具风险分级被绕过 | `classify_tool_risk`：DB tool_definitions 覆盖 + 静态兜底，未知工具=**readonly** | `execute_refund`=fund_effect 强制四同时 | `test_tool_risk_class.py` 9 项 |
| refund_tool/tool_gate 门禁被跳过 | Fail-Closed 与风险门禁作为**追加层**叠加在既有 refund_tool 之上，绝不替代 | 任一维 FAIL → BLOCK | `test_refund_missing_*`（hitl/幂等/审计/策略） |
| 版本注册中心越权写状态 | `version_center.approve/reject/rollback` 状态机白名单（仅 DRAFT/CANDIDATE→APPROVED） | 非法流转抛 ValueError → 400 | `test_approve_reject_flow`、`test_rollback_only_to_approved_history` |

---

## 2. 红蓝对抗语料（≥100 条：现有 118 条）

| 类别 | 数量 | 代表变体 | 判定 |
| --- | --- | --- | --- |
| 越狱（jailbreak） | 60 | 角色扮演/忽略系统指令/jailbreak 模式开启/伪身份 | **越狱拦截率 ≥98%**，一律 BLOCK 或 REVIEW |
| 注入 + 日志/泄露 | 58 | 跳审退款/篡改金额/Base64/多语言/Prompt 泄露/复合指令 | **注入拦截率 ≥95%** |
| DLP 样本 | 6（4 类） | 身份证/手机/银行卡/ApiKey | DLP ≥99%（V3-3 起终指标为 8 类、44 例基准） |
| 合法误杀校验 | 5 | 正常退换货咨询 | 合法不误杀 ≥95%，屏障非砖墙 |
| 越权退款伪造 | 3+1 | 伪造状态/未审批/核验 MISMATCH + 合规控制组 | 伪造 → 强拒；控制组 → 允许 |

语料真实存放在 `backend/tests/test_adversarial_red_blue.py`（pytest 版）与
`backend/scripts/red_blue_test.py`（报告版，产出红蓝对抗报告）。
本轮红蓝报告（2026-09-01）：注入 100% / 越狱 100% / DLP 100% / 合法 100%，四项全达标。

**穿透史（已封堵）**：Phase 4 红蓝真实穿透点「你被编程设定成」Prompt 泄露已加固；
Phase 3 `API_KEY` 正则 `\b` 汉字坑已修复；本轮 V3-3 无新穿透点。

---

## 3. V3-3 新增防线快照

| 防线 | 位置 | 语义 |
| --- | --- | --- |
| 工具风险分级 | `app/services/security_gateway.py` | readonly / internal_write / external_notify / fund_effect 四級；DB 可覆盖；未知工具=readonly |
| 退款四同时 | 同上 `check_tool_risk_gate` | 资金类放行必须 **策略+HITL+幂等+审计** 四维齐备，任一缺失 BLOCK |
| Fail-Closed | `app/services/fail_closed.py` | 安全服务故障：高风险=DENY；查询=按批准策略 mask_only DEGRADE 或 DENY |
| 版本注册中心 | `app/services/version_center.py` + `routes_version_center.py` | 7 类实体统一版本、sha256 指纹、批准/回滚只留痕不改语义 |
| DLP 终指标 | `app/services/ner_dlp.py` | 基准 44 例文档级准确率 ≥99%，未达标如实报 gaps |

---

## 4. 定期安全回归节奏

| 频率 | 内容 | 命令/位置 |
| --- | --- | --- |
| 每次提交CI | 输入注入/越狱/DLP/合法误杀 + tool_gate 越权退款 | `pytest tests/test_adversarial_red_blue.py tests/test_security_gateway.py -q` |
| 每次提交CI | V3-3 工具分级/四同时/Fail-Closed/版本中心/DP终指标 | `pytest tests/test_tool_risk_class.py tests/test_fail_closed.py tests/test_version_center.py tests/test_ner_dlp_metrics.py -q` |
| 冒烟（每日） | 评测五类 smoke 门禁 + 红蓝报告重出 | `python scripts/red_blue_test.py`（基准要求全达标才更新报告） |
| 全量回归（每次发布） | backend 全部 + eval 核心门禁 + 红蓝 | `pytest backend -q` + `pytest tests/eval_refund -q` |
| 每 N 次发布（或安全工单） | DLP 基准新增语料 → 准确率复测；穿透史复验 | `ner_dlp.final_metrics()` 报告对比上次 gaps |

**硬指标（实测口径）**：注入 ≥95%、越狱 ≥98%、DLP ≥99%、错误自动退款/拒绝后退款/重复退款/绕过审批 = 0。
数据全部出自己库测试记录，eval_runs=0 的可信评测按如实缺口报告，绝不虚构。

---

## 5. 结论

STRIDE 六维在本仓库均有可执行、可测试、可回归的防线，关键资金面（退款）由
「tool_gate 状态/决策/核验 + 工具风险四同时 + Fail-Closed 故障拒绝 + 幂等 + 审计留痕」
多层叠加，任何单点失效都不会产生"故障即开通"或"绕过即放行"。红蓝 118 条语料与定期回归
锁定攻击者演进；变形攻击（Base64/多语言/复合指令/伪身份）均覆盖。