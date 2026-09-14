# 数据字典

- `industry_uid`：稳定业务主键，格式 `SW2021:{level}:{industry_code}`。不得由行情代码或中文名称生成。
- `catalog_index_code`：本次原始 `index_classify` 分类目录代码；只用于追溯，不覆盖 raw。
- `quote_index_code`：通过本次证据门的 `sw_daily` 代码。
- `member_index_code`：通过本次两轮成员门的 `index_member_all` 代码。
- `index_member_all` 冻结主键：`(l3_code, ts_code, in_date, out_date, is_new)`；同一原始响应、拆分后的联合结果或跨查询联合结果中，只要该主键重复即失败关闭。即使两行完全相同也不得静默去重，非主键字段不同则同样视为上游歧义。
- `index_code`：本次快照的当前解析代码和 UI 路由代码；不是原始字段的替代品。
- `identity_state`：`DIRECT`、`EVIDENCE_GATED_ALIAS` 或 `EVIDENCE_GATED_TRANSITION`。
- `identity_rule/identity_rule_version`：实际命中的规则与版本；唯一特例版本为 `swivd-special-steel-identity-v1`。
- `current_index_code`：catalog 和行业分片对外披露的当前解析代码，必须等于 `index_code/quote_index_code`。
- `identity_reason_disclosure`：`DIRECT` 固定为 `NOT_APPLICABLE_DIRECT_IDENTITY`；非 `DIRECT` 固定为 `UNKNOWN_UPSTREAM_INTERNAL_CAUSE`，表示本地只能证明端点返回事实，不知道上游内部原因。
- `provider_kind`：`LIVE_SECURE_TUSHARE` 或 `TEST_INJECTED_CLIENT`。必须在 manifest、audit 和每条 request audit 中一致。真实 provider 的 request `transport=HTTPS_NO_REDIRECT`；注入测试 provider 为 `INJECTED_TEST_CLIENT`，不得标记为真实 HTTPS。
- `live_validation_state/live_validation_reason`：真实 provider 为 `PASS/SECURE_TUSHARE_CLIENT`；注入测试 provider 为 `NOT_LIVE_TEST_PROVIDER/EXPLICIT_CLIENT_INJECTION`。`offline_validation.status=PASS` 只代表离线验证通过，不会改变后者。

- `classification_sw2021_l1/l2/l3.csv`：以上身份字段及 `industry_code/parent_code`、名称、层级、发布状态；`is_pub=0` 仍保留目录。规范化主键是 `industry_uid`。
- `industry_identity_resolution.json`：schema `swivd-industry-identity-resolution-v1`；记录当前代码角色、状态、目标日、官方行情区段、规则和证据摘要。固定路径为 `inputs/normalized/industry_identity_resolution.json`。
- `sw_daily_sw2021_l1/l2/l3.csv`：官方行业 `close/pe/pb`，另保留 `source_ts_code` 和 `industry_uid`；`ts_code` 为本次解析后的 `quote_index_code`。L1 自 20211213 共同连续；L2/L3 从各自首个官方观测日至 as_of 连续。多代码历史只允许由身份解析文件声明的无重叠、无开市日缺口官方区段组成。
- `membership_episodes.csv`：完整 L1/L2/L3 路径、股票、`in_date/out_date/is_new`，另保留 `source_l3_code` 和 `industry_uid`；`l3_code` 为通过证据门的 `member_index_code`。同一路径与纳入日的旧 Y 和闭合 N 才可合并。
- `membership_snapshot.csv`：目标日候选，状态为 `ACTIVE`、`MEMBERSHIP_BOUNDARY_UNKNOWN` 或 `MEMBERSHIP_OVERLAP_UNKNOWN`。
- `stock_valuation_snapshot.csv`：目标日 `close, pe, pe_ttm, pb, ps_ttm, dv_ttm, total_mv, circ_mv`；缺行不填前值。
- `stock_peer_valuation.csv`：每只候选分别展开到 L1/L2/L3。`pe_ttm/pb_percentile_le = count(x <= current) / valid_N × 100`，只接受有限正值并包含自身。

每个百分位披露 `member_count/valid_n/coverage/tie_count/percentile_state`。`valid_n < 5`、当前值无效或组内任何成员关系未知时不计算。

新 `manifest.json` 使用 `swivd-local-snapshot-manifest-v4`；其 `identity_resolution` 对象记录
`rule_version/industry_uid/state/current_index_code/catalog_index_code/quote_index_code/member_index_code/evidence_path/evidence_sha256`。
`audit.json` 使用 `swivd-v2-audit-v2`，保存相同安全摘要及 raw request 哈希。UI 的
`catalog.json` 使用 `swivd-ui-catalog-v3`，行业分片使用 `swivd-industry-shard-v2`，非
`DIRECT` 行业必须披露代码角色、状态、规则版本和“上游内部原因未知”。

`SHA256SUMS`、`latest_run.json`、`historical_index.json`、`run_ledger.ndjson` 分别承担文件闭包、
current 定位、历史日期定位与追加式事件记录。每条 pointer 记录精确包含
`pointer_kind/scope/run_id/as_of/target_path/target_sha256`；解引用同时核对 run ID 日期、
目标 manifest 的 `run_id/as_of/purpose`。历史索引新写为 `swivd-historical-index-v2`，旧 v1
只允许经显式日期适配读取，并在下一次历史发布时原子升级。旧 manifest v2、audit v1、catalog v1/v2、shard
v1 只读兼容，不得原地补字段。

## v2.3 补充

- `daily_basic` 全市场空响应：`DAILY_BASIC_EMPTY`，不得发布新快照；个别股票缺行仍为 `VALUATION_UNAVAILABLE`。
- `total_mv/circ_mv`：Tushare原值单位万元，页面显示亿元时除以10000；原始表不改单位。
- `dv_ttm`：原值已经是百分比数值，显示百分号但不得再次乘100。
- `pe_ttm/pb_coverage`：对应指标有效正值数/既定去重成员数×100，搜索仅过滤可见行。
- 股票展示`name`不参与同一稳定生命周期合并；N退出记录可覆盖旧Y开放记录，raw保留所有原名。
- 历史身份`evidence.endpoint_current_member_code`为抓取时Y端点码，`membership_reference_date`为目标日；不得把前者当作历史日成员身份。
- `lineage_layout=FLAT_ANCESTOR_RAW_V1`：manifest v4 扁平祖先证据目录。旧v3嵌套目录只读验证，不原地迁移。
- `runtime.dependency_versions/dependencies_locked/timezone_available`：固定包名的安全版本证据、是否匹配冻结锁、上海时区是否可用。不保存主机名/用户名/解释器绝对路径/环境变量。
- `ACTIVE`只表示供应商成员关系按现有规则有效，不证明目标日仍上市、停牌原因或可交易；上市生命周期与分母重定义待第二批。
