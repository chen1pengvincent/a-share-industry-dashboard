# 申万行业估值全景仪表盘 v2 项目合同

- 合同版本：`swivd-contract-v2.3.0`
- 规格：`swivd-project-spec-v4.3`
- 决策：`GOV-20260906-001`；父决策 `GOV-20260901-004` 已关闭
- 项目 ID：`sw-industry-valuation-dashboard`
- 状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

## 1. 目标与身份

本项目是在本机浏览器中查看申万行业指数估值位置、收益和成分股当日估值的研究工具。SW2021 当前轴包含 L1/L2/L3；SW2014 只保留 RUN-004 已封存的 L1 历史档案。分类版本、行业官方估值、点时成员和个股横截面估值是四种不同事实，不得相互替代。

本项目不做选股、回测、评分、交易或投资建议。百分位只描述供应商观测值在指定样本中的经验位置，不证明内在价值、未来收益或可交易性。

SW2021 行业的稳定业务身份固定为 `industry_uid=SW2021:{level}:{industry_code}`，不再假设一个 `index_code` 能同时代表分类目录、行情和成员端点。每条规范化分类必须分别保留：

- `catalog_index_code`：本次原始分类目录代码；只用于追溯，不改写 raw；
- `quote_index_code`：本次通过证据门的 `sw_daily` 代码；
- `member_index_code`：本次通过两轮稳定性门的 `index_member_all` 代码；
- `index_code`：本次运行最终解析、供当前快照路由的代码；
- `source_ts_code/source_l3_code`：规范化行情与成员行中保留的端点原始代码；
- `identity_state/identity_rule_version`：解析状态和规则版本。

除 `SW2021:L3:230501` 外，目录、行情、成员和当前解析代码必须直接一致。唯一获准的候选集合为 `850401.SI/850412.SI`；它不构成名称模糊匹配、通用别名平台或其他行业的修正授权。

## 2. 数据链

```text
trade_cal -> 目标开市日与交易日轴
index_classify(SW2021,L1/L2/L3) -> 当前分类、父子关系、is_pub
sw_daily -> 官方行业指数 close/pe/pb -> 行业历史百分位与锚点收益
index_member_all(is_new=Y/N, 两轮) -> 成员生命周期与目标日候选关系
daily_basic(trade_date) -> 个股当日原始估值
成员关系 + 个股原值 -> 对应 L1/L2/L3 的 PE_TTM/PB 横截面百分位
```

行业 PE/PB 只能来自 `sw_daily`。`daily_basic` 只能用于成分股行，禁止以均值、中位数、市值加权或任何其他个股聚合补造行业 PE/PB。`is_pub=0` 行业保留目录和成分视图，但行业行情、收益和行业估值状态必须为 `NA_NOT_PUBLISHED`。

## 3. 分类、时间和完整性

- SW2021 分类计数门为 L1=31、L2=134、L3=346；数量只用于漂移检测，不替代代码、名称、父子关系和发布状态核验。
- SW2021 历史起点为 `20211213`。L1 继续要求共同起点完整矩形；L2/L3 允许不同指数从自身首个官方观测日起出现，但从首观测日至目标日不得有内部开市日缺口。
- `UPDATE_LATEST` 以 `Asia/Shanghai` 为唯一时区。若今天是 SSE 开市日且本地时刻早于
  `18:30:00`，目标日在联网获取目标日行情前明确选择为 `trade_cal.pretrade_date`；恰好
  18:30 起今天才有资格成为目标日。若今天休市，则使用其 `pretrade_date`，不得额外再退
  一个交易日。页面与 CLI 必须显示目标日及固定原因码。
- 上述规则是发布时间前的目标日选择，不是请求失败后的回退。目标日一经选定，数据为空、
  不完整或身份冲突时仍失败关闭，禁止继续回退。若所选目标日已是经验证 current，则幂等
  返回 `ALREADY_UP_TO_DATE`，不创建 run、不重抓行业/成员/个股业务数据、不改写 latest；
  运行前用于权威选日的一次 `trade_cal` 探针仍然保留。若目标早于 current 则拒绝执行，禁止
  倒退 current。
- 分类、行业行情、成员和个股估值必须绑定同一个 `as_of`；禁止混合日期。
- 分类/行情代码冲突、schema 漂移、重复主键、接口触及行限或当前已发布指数缺少目标日行时失败关闭。唯一例外是本合同第 3.1 节的版本化身份规则；不自动沿用其他项目的修正规则。

### 3.1 特钢三级行业的端点身份规则

规则版本固定为 `swivd-special-steel-identity-v1`，稳定身份固定为 `SW2021:L3:230501`，冻结名称路径为 `钢铁(230000) / 特钢Ⅱ(230500) / 特钢Ⅲ(230501)`，候选代码只允许 `850401.SI` 与 `850412.SI`。当前已核验形态为目录 `850401.SI`、行情与成员 `850412.SI`，状态为 `EVIDENCE_GATED_ALIAS`，当前解析代码为 `850412.SI`。这是对端点返回事实的受控连接，不声称已知 Tushare 内部形成原因。

每次运行必须从本次新请求重新验证，不能把父 run 的结论当作当前证据：

1. 三层分类计数、唯一 `industry_uid`、名称、父子路径、发布状态和候选代码集合全部符合冻结规则；
2. 当前目标日不得同时出现两个候选行情代码；当前有效成员不得同时落在两个候选代码；
3. `quote_index_code` 与 `member_index_code` 必须同步；成员 Y/N 两轮规范化仍须完全稳定；
4. 行情各段只能来自官方 `sw_daily`，按交易日历无重叠、无内部缺口地连接；禁止插值、补前值或用个股聚合；
5. 任何第二别名、代码被另一分类占用、名称或路径漂移、双码并存、行情缺口、成员不同步或证据不完整均失败关闭。

若未来分类、当前行情和成员端点直接一致，则状态为 `DIRECT`。若官方代码在连续开市日间无缝切换，历史可保留两个有明确起止日的官方 `sw_daily` 区段，状态为 `EVIDENCE_GATED_TRANSITION`；切换日重叠、断档或成员未同步时不得建立快照。上游目录直接修复为当前行情/成员代码时可以退出别名状态，但仍须通过同一套当前运行证据门。

原始分类、行情和成员响应保持原样。规范化证据固定写入 `inputs/normalized/industry_identity_resolution.json`；身份专用 raw 证据写入 `inputs/raw/identity_resolution/sw_daily/` 和 `inputs/raw/identity_resolution/index_member_all/round_{1,2}/{Y,N}/`，并与正常分类、目标日行情及主成员 raw 共同构成证据闭包。

## 4. 点时成员

成员原样保存 L1/L2/L3 代码与名称、股票代码与名称、`in_date/out_date/is_new`。对于获准身份规则，还须保留 `source_l3_code`，以 `member_index_code` 连接稳定 `industry_uid`，不得把端点原值覆盖成目录代码。每次运行同时分批抓取 Y/N 两种状态并完成两轮规范化稳定性比较；任一批达到 2000 行时继续按下一层拆分，最小批次仍触限即失败。

普通非边界日期只接受唯一有效 L3 路径，并由该路径确定 L2/L1。若目标日恰等于 `in_date` 或 `out_date` 且没有新的权威证据，相关记录为 `MEMBERSHIP_BOUNDARY_UNKNOWN`；多个候选路径同时有效则为 `MEMBERSHIP_OVERLAP_UNKNOWN`。未知记录和候选原样展示，但受影响行业不计算同业百分位。禁止任意挑选、按名称模糊映射或把当前成员倒灌到历史。

历史查询只允许 `20211213` 以来的官方开市日，必须由用户显式执行 `MATERIALIZE_DATE`；它生成独立不可变快照，不更新 current latest。

## 5. 个股估值和横截面百分位

个股原始展示字段为 `close/pe/pe_ttm/pb/ps_ttm/dv_ttm/total_mv/circ_mv`。供应商缺行或字段为空必须保留为空并标 `VALUATION_UNAVAILABLE`，不得补前值、补零或推断亏损原因。

只对 PE_TTM 和 PB 按所选层级行业分别计算：

```text
percentile_le = count(valid_value <= current_value) / valid_N * 100
```

有效值必须有限且严格大于零；样本包含自身。输出必须同时包含 `member_count`、`valid_N`、`tie_count` 和 `coverage`。`valid_N < 5`、当前值无效或成员状态未知时百分位为空并给出机器状态码。页面只能写“同业百分位/相对位置”，不得写“低估/高估”。

行业历史 PE/PB 百分位继续采用同一经验分布函数，但最少需要 252 个有效历史观测；收益继续使用官方指数 close 的 5 日、MTD、YTD 精确交易日锚点，不连乘舍入后的涨跌幅。

## 6. 本地服务和更新

服务只绑定 `127.0.0.1`，禁止非 loopback 监听。GET 只读；POST 创建一次明确的 `UPDATE_LATEST` 或 `MATERIALIZE_DATE` 作业，必须通过 Host、Origin、JSON Content-Type 和会话 nonce 校验。关闭 CORS，静态资源只来自项目本地并设置 CSP。

作业状态依次为 `QUEUED/PREFLIGHT/CLASSIFICATION/INDUSTRY_DATA/MEMBERSHIP/STOCK_VALUATION/BUILD/VALIDATE/PUBLISH`，终态为 `SUCCEEDED/FAILED/INTERRUPTED`。过期阶段回调不得把状态倒退；同一阶段内 `completed_units/total_units` 均不得减小，进入下一阶段后单位计数才可按新阶段重置；全局 `percent` 只能单调增加，终态忽略迟到回调。错误只返回安全状态码。单写者锁同时约束 CLI 和网页；失败或中断不得更新 latest。发布固定采用可恢复事务：先持久化 `PREPARED` intent，再原子替换 pointer，随后持久化 `COMMITTED` 作为唯一提交点。提交点前中断必须恢复 pointer 前像并只追加一个 `RUN_INTERRUPTED`；提交点后中断必须保留新 pointer 并幂等补齐唯一 `RUN_SUCCEEDED`。存在未恢复 intent 时所有 pointer 读取失败关闭，CLI 或服务启动后只能在单写者锁内恢复；文件替换、追加、intent 删除及相关目录项均执行持久化。浏览器固定读取打开时的 run，新 run 成功后只显示提示，由用户点击切换。

## 7. Token 与迁移

Token 读取顺序为 `TUSHARE_TOKEN`，其次 `tushare.get_token()`。浏览器没有 token 字段；token 不得进入 URL、argv、配置、localStorage、日志、进度、错误、raw、manifest、HTML、哈希或任何导出。项目不创建或修改 Tushare 本机 token 配置。

源码与数据目录分离；manifest 内路径必须相对 run 根。历史数据目录可以在电脑之间复制，但导入或服务前必须逐 run 校验 manifest 和哈希，token 必须在目标电脑重新配置。正常 CLI/网页运行的 provider 固定为 `LIVE_SECURE_TUSHARE`。任何向 `run_snapshot` 显式注入的 client 都必须标记为 `TEST_INJECTED_CLIENT`，只能写入 Python 系统临时目录的严格子目录，其 `live_validation_state=NOT_LIVE_TEST_PROVIDER`，不得伪称真实联网 PASS。真实与测试 provider 的血缘禁止混接。支持 Python 3.11–3.14；macOS 需真实 Chrome E2E。完成真实 Windows 验收前状态固定为 `WINDOWS_E2E_UNVERIFIED`。

## 8. 不可变产物与权限

每个新 run 使用 `swivd-local-snapshot-manifest-v4`、`swivd-v2-audit-v2`、`swivd-ui-catalog-v3` 和 `swivd-industry-shard-v2`。至少包含冻结 raw、规范化交易日/分类/身份解析/行业行情/成员/个股估值，派生行业摘要、同业百分位、懒加载 UI 分片、`audit.json`、`manifest.json`、`SHA256SUMS` 和对抗报告。manifest 的 `identity_resolution` 必须记录 `rule_version/industry_uid/state/current_index_code/catalog_index_code/quote_index_code/member_index_code/evidence_path/evidence_sha256`；manifest、audit 和每条 request audit 还必须以 `provider_kind` 一致披露数据 provider，manifest/audit 分别记录与之匹配的 `live_validation_state/live_validation_reason`。audit v2 保存同一安全摘要及其 raw request 哈希。增量 run 记录父 manifest，但必须自包含，不使用软链接或依赖可变父目录；父 raw 与祖先证据采用 `FLAT_ANCESTOR_RAW_V1` 扁平布局，逐文件绑定未改写的父/祖先 manifest `artifacts`，不得递归嵌套目录。current 与 historical 的每条 pointer 记录固定包含 `pointer_kind/scope/run_id/as_of/target_path/target_sha256`，并同时绑定 run ID 日期、目标 manifest 身份与作业 purpose；historical index 新写为 v2，旧 v1 只读适配并在下次发布时原子升级。发布 intent 固定为 `swivd-publication-transaction-v1`，路径 `transactions/publication.json`；同一 run 的 `RUN_SUCCEEDED/RUN_FAILED/RUN_INTERRUPTED` 最多只能存在一个且不得互斥并存。只有全部必需门通过的 `UPDATE_LATEST` 才可进入发布事务；所有运行追加 ledger。

RUN-001 至 RUN-004 永不覆盖。旧 manifest v2、audit v1、catalog v1/v2 和 shard v1 只读兼容，不得原地升级或冒充 v3/v2 新产物；RUN-004 只在原路径、原 manifest 哈希和原状态联合命中时保留旧 schema 兼容，复制或重封不得继承。

禁止定时更新、通知、云部署、账户系统、网页保存 token、个股自身多年历史百分位、回测、评分、交易、Claude Stock/资金流项目集成、commit、push、Release 和外部发布。

## 9. 页面与视觉一致性

本地服务页面必须保留原压缩包 `step3_visualize.py` 的 GitHub-dark 视觉语言及六类全景信息架构：总览、估值热力图、历史走势、涨跌排行、PE-PB 象限和数据明细；在此基础上增加 L1/L2/L3 统一切换、行业成分和 SW2014 封存档案。原压缩包的视觉结构不授权恢复其 baostock 数据语义、个股聚合行业估值、“低估/高估”结论或远程 ECharts CDN。

总览、热力图、排行、散点和行业明细只能读取快照内已计算的行业摘要；目录摘要必须与对应行业分片逐字段一致，不得在浏览器重新定义公式。行业历史和成分股仍按单行业懒加载；SW2014 只读接口只能读取已验证 RUN-004 的 CSV，不执行其 HTML，也不与 SW2021 拼接。

页面中的历史位置文字必须直接读取 Python 产物的 `pe_history_label/pb_history_label`（SW2014 为 `pe_label/pb_label`），浏览器只能按数值着色，不能自行重算定性标签。对于非 `DIRECT` 行业，catalog 与行业分片必须披露稳定 `industry_uid`、目录/行情/成员/当前解析代码、身份状态、规则版本，并固定输出 `identity_reason_disclosure=UNKNOWN_UPSTREAM_INTERNAL_CAUSE`，在页面显示“上游内部原因未知”；`DIRECT` 固定为 `NOT_APPLICABLE_DIRECT_IDENTITY`。不得把本地解析伪装成供应商修复。服务可将已验证的旧 catalog 在内存中投影为兼容响应，但不得为旧快照补造身份解析证据、不得改写旧快照；投影字段必须来自同一快照内已验证的分类和行业分片。服务每次读取 catalog、分片或档案 CSV 时，必须用首次验证所得 manifest 字节数和 SHA-256 再核对目标字节，检测到漂移即失败关闭。

`web/index.html` 是源码，唯一运行入口为 loopback HTTP。直接通过 `file://` 打开时必须失败关闭并显示启动指引，不得为了支持文件预览而嵌入 token、快照或放宽 CSP。页面资源必须使用同源相对路径；HTTP 验收必须覆盖 HTML、CSS、JavaScript 和 bootstrap 的状态码、MIME 与安全头。

## 10. v2.3 正确性修复与兼容界限

全市场 daily_basic 空响应必须失败关闭，个别成员缺行继续保留；非空响应只证明非空，不自动证明全市场完整。证券上市/退市生命周期及成员分母不在本批更改。Y/N 同一生命周期以稳定身份而非股票展示名称归并，原始名称保留在 raw；不同代码路径、纳入日或矛盾退出日不得静默归并。

新历史快照将请求时当前成员端点与目标日有效成员码分开披露；按目标日生命周期候选验证与该日行情码一致。边界、双码、两轮漂移与身份碰撞继续失败关闭或按原成员未知规则处理，不能放宽为名称替换。拆分成员查询时同时覆盖唯一获准特钢候选集合，不增加正式行业实体。

客户端在原始字节和 JSON 解码后（包含重复键覆盖前的值）检查凭据反射，任何命中在返回或落盘前拒绝。页面市值明确单位、股息率为供应商百分比原值；PE_TTM/PB 分别披露成员数/有效数/覆盖率，搜索不改变分母。走势图保留日期轴并在无效值处断线。

服务初次完整验证按run合并并发，不用可变mtime替代哈希。写锁占用不阻止只读服务就绪；未决发布事务仍不读取pointer。进度暂不可达不等于任务失败，允许恢复原job跟踪但不自动重发POST。

旧manifest-v3只读兼容；GOV-20260901-004封存真实快照以其精确manifest哈希作为旧源码信任锚，不要求旧源码等于升级后源码。仍检查原有文件哈希和数据重放，不执行冻结源码。新manifest-v4记录扁平血缘及安全运行依赖信息。各自完整快照继续自包含；保留所有运行仍会占用累计磁盘空间，扁平化不等于跨快照去重。
