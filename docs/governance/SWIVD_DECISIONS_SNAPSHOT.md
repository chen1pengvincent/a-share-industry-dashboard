# SWIVD 历史决策摘录（只读参考）

截至 2026-09-14，从本机全局日志按 SWIVD 决策标题选取；原文未改。不是全量日志，不授予新动作权限，不迁移事实源。

## GOV-20260830-001-AUTHORIZED｜建立申万行业估值仪表盘独立研究项目

- 记录类型：`PROJECT_CREATION_AND_GOVERNED_RESEARCH_AUTHORIZATION`
- 记录时间：`2026-08-30T22:37:42+08:00`
- `decision_id=GOV-20260830-001`
- 用户批准原文：`接受，继续推进项目，直到项目完成。开启对抗式审查，确保不要出错。`
- 承接的已确认目标：独立项目、纠错后的能力复现、Tushare PRO API 加冻结快照、申万一级
  首发、SW2021 与 SW2014 分开展示、经验分布函数采用小于等于、旧版轴分轴失败关闭、
  手动离线批处理、不展示成分股数量。
- `authorization_state=ACTIVE_UNTIL_FINAL_OR_FAILED_CLOSE`

### 目标、根因与身份边界

目标是在
`/Users/chenyipeng/Desktop/Codex/Stock/sw_industry_valuation_dashboard`
建立一个新的、独立的、仅供研究使用的申万行业估值仪表盘。原压缩包只作为功能与页面
参照，不作为数据、依赖或执行权威；不得执行或导入其中的 Pickle，也不得复刻其证监会
行业冒充申万、当前成员倒灌历史、个股 PE/PB 中位数冒充行业估值、测试缓存污染、CDN
依赖或“十年 SW2021”错误陈述。

项目使用 Tushare `index_classify` 冻结 `SW2021` 与 `SW2014` 一级分类身份，使用
`trade_cal` 冻结交易日轴，使用 `sw_daily` 的申万行业指数原始 `pe/pb/close` 字段。
不得调用 `daily_basic` 或成员表来替代行业指数估值。SW2021 当前轴从 `20211213`
开始；SW2014 仅作为版本切换前的历史档案，结束日为 `20211213` 前最后一个经交易日历
验证的交易日。两轴分别计算、分别标状态，不拼接、不做跨代行业映射或排名。

### 准确允许动作与文件范围

1. 向本追加式日志写入本授权及唯一的最终关闭或失败关闭回执；不得改写既有记录。
2. 更新 `docs/project_registry.json` 顶层 `decision_id/as_of`，并新增唯一
   `sw-industry-valuation-dashboard` 项目条目；不得改变其他项目对象。
3. 创建并写入新项目 canonical root 的合同、规格、Python 源码、测试、项目 README、
   本地静态前端资源及 `output/runs/<run_id>/` 研究产物。新项目之外不得写业务文件。
4. 只允许向 `https://api.tushare.pro` 发出 Tushare 数据请求，端点限
   `trade_cal/index_classify/sw_daily`；禁止明文 HTTP、禁止重定向、禁止其他主机。
   允许从 `TUSHARE_TOKEN` 或 Tushare 官方本地 token 配置中加载凭证，但不得输出、记录、
   哈希、复制或写入项目。只有瞬时网络错误和 429 可有限重试；权限、schema、空结果或
   参数错误不得盲重试。
5. 允许生成一个明确 `as_of` 的完整研究运行。每次运行使用唯一目录，冻结原始响应、
   规范化表、请求元数据、审计、manifest、SHA-256 与自包含 HTML；不得覆盖旧 run，
   不得在失败运行更新 `latest`。测试与示例必须使用系统临时目录或独立命名空间。

### 冻结计算与展示语义

- 首版只展示申万一级行业；SW2021 当前目录预期 31 行、SW2014 历史目录预期 28 行，
  预期数量只是漂移门，不替代运行时身份核验。`is_pub=0` 保留并标
  `NA_NOT_PUBLISHED`，不得补造行情或估值。
- 当前 PE/PB 原值来自 `sw_daily`。历史百分位仅对有限且大于零的同版本日观测计算，
  公式为 `count(x <= current) / N`，当前观测包含在 N 中；页面必须披露公式、N、并列数
  与并列比例。当前值非正、非有限或 N 少于 252 时，百分位不可用；原始值仍保留。
- 展示标签只能写“历史极低位/较低位/中位/较高位/极高位”，阈值为
  `[0,20)、[20,40)、[40,60)、[60,80)、[80,100]`；不得写成内在价值或投资建议。
- 5 日、月初至今、年初至今收益使用官方指数 `close` 的锚点比值计算：分别以第 5 个
  前序交易日、上月最后交易日、上年最后交易日为基准。缺锚点即标历史不足，不使用
  舍入后的 `pct_change` 连乘或个股收益均值。
- SW2014 历史档案只展示截至切换前的历史数据与最后有效日位置，不称当前估值。若其
  官方 PE/PB 或身份覆盖不闭合，该轴标 `BLOCKED`；SW2021 可独立生成候选产物，但整体
  不得宣称双版本完整完成。
- 页面保留原项目六类能力并改为一级行业：总览、热力图、历史走势、涨跌排行、PE-PB
  象限、数据明细；另设 SW2014 历史档案页。页面完全离线，不使用 CDN、远程字体、
  外部链接或浏览器默认打开动作，不显示成分股数量。

### 产物状态、禁止事项与验证

所有输出固定为 `research_only`、`decision_eligible=false`、
`production_approved=false`。禁止自动化、通知、回测、选股、评分、交易、Claude Stock
或资金流项目集成、commit、push、Release、部署和外部发布；禁止修改任何 baseline、
authority、factor registry、screener、pointer、v3、portfolio、forward 或生产状态。

必须完成：登记表 JSON 与边界 diff、合同/规格 schema、离线单元与集成测试、冻结输入
断网重建、主键/分类/交易日/覆盖/空值/异常值/并列/收益锚点门禁、测试与正式命名空间
隔离、HTML 与规范化输出逐字段回读、无 Pickle/CDN/外链/秘密、全部产物 SHA-256、
Chrome 后台检查（Chrome 不可用则明确跳过，不以其他浏览器替代）、修改前后库存与受保护
文件哈希复核。最终回执必须列出真实接口结果、运行 ID、产物哈希、测试结果、未解决风险
与逐文件回滚；任一必需门失败不得宣称完成。

### 修改前事实与回滚

Stock 根不是 Git 仓库。修改前哈希为：

- `AGENTS.md`：`3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`；
- `README.md`：`b89126c6baafd7beb04bcee39bb67a77be7b193dd31bf5701889f0914e3653a8`；
- `docs/project_registry.json`：`f0d8acee1cb05b9a807f0e2cf8cd1d3f756546c3a6dd285d331b8d06970855ee`；
- `docs/governance_decisions.md`：`2e62952bcb360cc3be875b053029e41d875f2cfa5a19780b5e9795d41525859b`；
- 新项目路径在授权前不存在。

修改前副本位于系统临时目录 `/tmp/swivd-preflight.x2sUCh`。关闭前失败时，仅允许逐文件
恢复登记表并移走新项目目录，再追加失败关闭回执；不得删除本授权或改写历史。成功关闭后
若需回滚，项目目录可整体移出 canonical root，登记表只能通过新的 successor 决策删除或
标记退役，本追加式记录永久保留。


## GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED｜退役分类三态发布语义与分行业起点

- 记录类型：`DATA_SCHEMA_AND_VALIDATION_SEMANTICS_SUCCESSOR_AUTHORIZATION`
- 记录时间：`2026-08-30T23:05:27+08:00`
- 父决策：`GOV-20260830-001`
- 稳定回执 ID：`GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED`
- 用户批准原文：`接受，继续推进项目，直到项目完成。开启对抗式审查，确保不要出错。`
- 用户批准对象：上一轮完整披露的推荐方案，即保留 SW2014 `is_pub=null`
  原始事实，使用退役分类专用三态语义，并按每个行业自身首个官方观测日至
  `20211210` 校验无内部交易日缺口。
- 修改前账本 SHA-256：
  `8a4327279e2ef9ef1cbd4be7eeb3e18ab293249a8515e76174f3634c37bbaa2b`。

### 触发事实与根因

2026-08-30 真实 Tushare HTTPS 取证显示：

1. `index_classify(level=L1, src=SW2014)` 正好返回 28 行，`src/level/index_code/
   industry_name/industry_code` 身份字段完整，但 28 行 `is_pub` 均为 JSON `null`；
   原始响应 SHA-256 为
   `1ac63a154fdae3fd2d3c1b2c34f185c6f8fdb847827d5640cb5959ce13ab5669`。
2. 28 个代码均有同身份 `sw_daily` 数据并在 `20211210` 结束；17 个行业从
   `20140102` 起共 1935 个交易日，11 个行业从 `20140221` 起共 1904 个
   交易日，数量与各自起点后的官方交易日历一致，所有收盘价非空。
3. 既有实现把当前分类的二元发布标志和全轴统一起点错用到退役分类，
   因而错误失败关闭。根因是验证器混合了“当前是否发布”和“历史分类
   身份及实际行情覆盖”两个不同命题。

### 准确允许的语义与文件

1. 允许更新新项目的 `PROJECT_CONTRACT.md`、`PROJECT_SPEC.json`、`README.md`、
   `src/swivd/core.py`、`src/swivd/pipeline.py`、`src/swivd/render.py`、
   `src/swivd/validator.py` 及对应测试，仅用于下列 SW2014 修正。
2. SW2014 归一化分类必须保留 `is_pub` 空值，不得改写为 `1`；必须显式记录
   `publication_state=NOT_PROVIDED_FOR_RETIRED_TAXONOMY` 与
   `selection_basis=ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY`。
3. SW2014 历史白名单由该次冻结 `index_classify` 响应的全部 28 个唯一 L1 身份组成；
   只有代码、名称、层级、来源、数量或与 `sw_daily` 身份冲突时才失败关闭。
4. SW2014 完整性按行业分别校验：允许首个观测日晚于查询起点，但每个行业
   必须从自身首个观测日起覆盖每一个官方交易日直至共同结束日，不得有内部
   缺口、重复主键、白名单外代码或末日缺失；每个 PE/PB 百分位仍需至少 252 个
   有限且严格大于零的观测。
5. SW2021 的 31 行身份、`is_pub=0|1`、已发布子集、统一起点完整矩形、
   计算公式、收益锚点和所有已冻结语义保持不变。两个版本仍严禁拼接、
   名称映射或跨版本排名。

### 禁止、验证与回滚

本 successor 不允许任何新数据源、新端点、个股聚合、跨版本映射、公式/
阈值/标签/收益锚点变化、当前轴放宽、自动化、回测、评分、交易、集成、对外发布、
commit、push、Release 或生产权限。

必须新增对抗测试：SW2014 空 `is_pub` 不得被改写；早于首观测日的结构性空白
允许；首观测日后任一交易日缺口、末日缺失、伪造 `is_pub=1`、跨轴代码、
不足 252 条或 SW2021 的空 `is_pub` 均必须失败关闭。真实双轴运行、冻结输入断网重建、
独立回读验证、哈希闭包与 Chrome 后台检查全部通过前，不得宣称完成。

若实施失败，只允许逐文件恢复本项目修改前字节，保留已经冻结的运行目录，
并向本账本追加失败回执；不得改写本授权或父决策。


## GOV-20260830-001-NAME-HISTORY-AUTHORIZED｜稳定代码身份与历史名称保真

- 记录类型：`DATA_IDENTITY_AND_LABEL_HISTORY_SUCCESSOR_AUTHORIZATION`
- 记录时间：`2026-08-30T23:24:17+08:00`
- 父决策：`GOV-20260830-001`
- 前置 successor：`GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED`
- 稳定回执 ID：`GOV-20260830-001-NAME-HISTORY-AUTHORIZED`
- 用户批准原文：`接受，继续推进项目，直到项目完成。开启对抗式审查，确保不要出错。`
- 用户批准对象：上一轮完整披露的“稳定代码作为身份、历史名称原样保留、
  共同末日名称必须对齐冻结分类”推荐方案。
- 修改前账本 SHA-256：
  `e35efce1749eb5d81c69309be318ce3583418ea9cb8ab103e7d7b079438da51e`。

### 真实接口事实与根因

`SWIVD-RUN-20260828-002` 真实运行在 SW2014 的 `801040.SI` 上因 `NAME_DRIFT`
失败关闭，且正确没有更新 `latest`。随后对 28 个 SW2014 L1 行业做同一
HTTPS 数据源的全量只读核验，证明代码、交易日、共同末日和 PE/PB 覆盖全部
闭合，仅有两个同代码历史改名：

- `801040.SI`：`20140102`–`20150121` 为“黑色金属”，从 `20150122` 起为“钢铁”；
- `801210.SI`：`20140102`–`20150121` 为“餐饮旅游”，从 `20150122` 起为“休闲服务”。

两个代码在整段历史中均稳定且无内部交易日缺口，共同末日名称与冻结
`index_classify` 目录一致。根因是旧验证器错把可变展示标签 `name` 当成了不可变
主键，而真正的同版本时序身份是冻结白名单内的 `ts_code`。

### 准确允许的修正

1. 允许更新新项目的 `PROJECT_CONTRACT.md`、`PROJECT_SPEC.json`、`README.md`、
   `src/swivd/core.py`、`src/swivd/pipeline.py`、`src/swivd/render.py`、
   `src/swivd/validator.py` 及对应测试，仅用于下列同代码历史名称保真。
2. SW2014 身份仍以冻结 `index_classify(src=SW2014, level=L1)` 的 28 个唯一
   `index_code` 为白名单；`sw_daily.ts_code` 必须逐行落在该白名单，不得按名称
   连接、合并、拆分或跨版本映射。
3. `sw_daily.name` 作为可变上游标签原样保留。历史表新增 `source_name`，
   不得用冻结分类末期名称静默覆盖早期名称。稳定展示列 `industry_name`
   可使用冻结分类名称，但必须与 `source_name` 并存而不冒充原始名称。
4. 每个代码必须在共同末日有且仅有一行，且该行 `sw_daily.name` 必须严格等于
   冻结分类名称。空名称、末日不一致、代码变更或白名单外代码均失败关闭。
5. 审计必须按代码记录按时间连续的名称分段，每段至少包含 `source_name`、
   `first_date`、`last_date`、`row_count`；页面必须显示名称是标签而非身份，并披露
   实际改名代码与分段。

### 禁止、验证与回滚

本 successor 不允许任何新分类、名称相似度映射、跨版本拼接、代码替换、新数据源、
新端点、公式/阈值/收益锚点变化、自动化、回测、评分、交易、集成、对外发布、commit、
push、Release 或生产权限。

必须新增正反对抗测试：同代码连续历史改名且末日对齐时通过；历史名称原值在
CSV 与 HTML payload 中一致；空名称、末日名称冲突、代码变更、名称字段遭静默
覆盖、跨轴或未披露改名审计均必须失败关闭。实施后需重跑全部离线门禁、
真实双轴运行、冻结输入断网重建、独立回读、哈希闭包和 Chrome 后台检查。

修改前项目哈希：合同
`84c447c0a9f34b6058619f8b043b15893df69763609a2be3af7f4d70fbdd2d4e`，规格
`6eaa417d36b3fd36176b1e1824e1304a3e12f2abd40750682abb694cf9f37516`，core
`5060539ccde734b69b24f203705ea65869a062af89e3feb7a16d13f86d0c2a93`，pipeline
`1d6f1d7a1a57ef6c2d897399a95ef71046b2f67abfab3783f0c800b7a454681b`，render
`553fe2933d384c1bf2c6794c99804cc7334d4548521aa386a1b4e696f0469fc3`，validator
`6047866d6592169f67f1a0842756da920302874ce154c9c1b28ce29abfcd14f6`。修改前回滚副本应保存在
系统临时目录；失败时只能逐文件恢复并追加失败回执，不得改写历史授权。


## GOV-20260830-001-FINAL_CLOSED｜申万行业估值仪表盘复现完成并关闭授权

- 记录类型：`FINAL_SUCCESS_CLOSE`
- 记录时间：`2026-08-31T01:17:32+08:00`
- 父决策：`GOV-20260830-001`
- 已承接 successor：`GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED`、
  `GOV-20260830-001-NAME-HISTORY-AUTHORIZED`
- 稳定关闭 ID：`GOV-20260830-001-FINAL_CLOSED`
- 用户批准原文：`接受，继续推进项目，直到项目完成。开启对抗式审查，确保不要出错。`
- 追加前治理账本 SHA-256：
  `c24962344949884abb584f2c7b157dc50679c0543963716b5fdf412ddb1763b5`
- `authorization_state=CLOSED_SUCCESS`；本记录写入后，父决策及两个 successor
  不得复用于任何后续写入、运行、数据刷新、自动化、晋级、提交或发布。

### 最终真实运行与产物身份

最终运行 `SWIVD-RUN-20260828-004` 在
`2026-08-31T01:12:57+08:00` 创建、`2026-08-31T01:13:40+08:00` 完成，
只通过 HTTPS 调用 `trade_cal/index_classify/sw_daily`，共 62 个请求。
运行状态为 `execution_status=COMPLETED`、
`artifact_publish_state=LOCAL_RESEARCH_CANDIDATE_COMPLETE`、
`live_validation_state=PASS`，SW2021 与 SW2014 两轴均为 `PASS`；同时严格保持
`research_grade=RESEARCH_ONLY`、`decision_eligible=false`、
`production_approved=false`。这里的 `COMPLETE` 只表示本地研究候选的两条分类轴
闭合，不表示可交易、可评分、可生产或构成投资建议。

RUN-004 manifest SHA-256 为
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`；
核心产物哈希为：`dashboard.html`
`d0995de80a0a5159e2ced8feac1cf437c8de42607b5bf63293c610a7b9f552a0`，
`audit.json` `798ddfced3e2b888f911592ac8c6114ca21a3e5ff04265f2385349bd6442ae34`，
`reports/adversarial_review.md`
`8e947432b26c7fd95a4dae0d04fd64704fa5c177c5eb64da20976a0e65902aee`。
`output/latest_run.json` 已精确指向 RUN-004 manifest，指针文件 SHA-256 为
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`；
追加式运行账本现为 5 行，SHA-256 为
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`，
末两条依次为 RUN-004 `RUN_SUMMARY` 与 `LATEST_POINTER_UPDATED`。

此前 RUN-001、RUN-002、RUN-003 均保持不可变、仍为
`LOCAL_RESEARCH_CANDIDATE_PARTIAL`，且 SW2014 均为 `BLOCKED`；新版验证器只在
原 canonical 绝对路径、原 manifest 哈希和原状态联合命中时允许验证这些历史封存，
复制或重封不能继承兼容，也不能据此升级状态。

### 数据闭合事实与异常披露

- SW2021 冻结分类为 31 个一级行业，`20211213` 至 `20260828` 共 1143 个
  交易日、35,433 行，分类、当前摘要与历史主键均唯一。
- SW2014 冻结分类为 28 个一级行业，截止 `20211210` 共 53,839 行：17 个行业
  自 `20140102` 起各 1935 行，11 个行业自 `20140221` 起各 1904 行；每个行业
  从自身官方首观测日至共同末日无内部交易日缺口，分类、档案摘要与历史主键均唯一。
- SW2014 以 `ts_code` 为稳定身份，保留 `sw_daily.name` 为 `source_name`；共
  28 个代码、30 个名称分段、2 个历史改名代码：`801040.SI` 从“黑色金属”变为
  “钢铁”，`801210.SI` 从“餐饮旅游”变为“休闲服务”，切换日均为 `20150122`，
  共同末日名称与冻结分类严格一致。
- SW2021 未发现 OHLC 排序异常。SW2014 原样披露两行
  `HIGH_BELOW_CLOSE`：`801080.SI/20151230` 的 high `3582.2`、close `3582.41`，
  以及 `801780.SI/20151230` 的 high `3392.81`、close `3392.82`。
  `open/low/high` 不参与冻结公式，故正数且有限时的相互排序异常仅披露、不改值、
  不阻断；`close` 以及任一已提供 OHLC 非正或非有限仍失败关闭，PE/PB 仍只使用
  有限且严格大于零的观测。

### 验证、对抗审查与浏览器证据

1. 最终全量离线测试 `92/92` 通过；源码编译、机器规格验证及 RUN-001/002/003/004
   独立验证全部通过。对抗测试覆盖跨版本拼接、主键重复、日期断档、分类错配、
   百分位与收益锚点篡改、原始/规范化漂移、HTML/报告状态伪造、秘密与 Pickle、
   source closure、外部路径写入、latest 失败保护和兼容规则复制攻击。
2. 审查中真实发现并关闭一个 P1：独立验证器原先未复核已提供
   `open/low/high > 0`。攻击者同步篡改 raw、normalized、audit、report、HTML、
   manifest 与 SHA 后曾可能骗过验证；最终验证器已与生产门统一为 close 严格正数、
   任一非空 OHLC 严格正数，新增全链重封负数攻击测试并通过。最终独立复验无未解决
   P0/P1/P2。
3. RUN-004 的 `SHA256SUMS` 全部通过。另从冻结规范化输入向独立 `/tmp` 目录断网
   重建 `sw2021_current/history`、`sw2014_archive/history`、Dashboard 与对抗报告，
   六个文件均与正式运行逐字节相同；运行内的隔离新进程 validate/rebuild 也均为 PASS。
4. Google Chrome 仅以后台 headless 模式加载本地 Dashboard，成功生成
   1440×1200 PNG，SHA-256 为
   `102b57c75bc7ce45291be6dc7718d304d1cff3ccedb888ed6671f728e3775583`；
   画面可见 RUN-004、日期、双轴 PASS、RESEARCH ONLY、7 个标签页和总览数据，
   未出现空白页或脚本崩溃。未使用 Edge 或其他浏览器替代。
5. HTML 不含 CDN、远程脚本、远程样式、HTTP 链接或浏览器自动打开动作；项目目录
   不含 `__pycache__`、`.pyc`、`.pickle` 或 `.pkl`。

### 受保护状态、边界与剩余限制

Stock 根 `AGENTS.md`、根 `README.md` 与项目登记表最终 SHA-256 分别保持
`3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`、
`b89126c6baafd7beb04bcee39bb67a77be7b193dd31bf5701889f0914e3653a8`、
`74a134f75ca25ff1586e7326d6a8fcaf47d142559eeb7a02095a7185c724d993`。
Claude Stock 的六个 authority/baseline、两个 artifact pointer、screener lock/pointer、
forward 配置、factor registry，以及 Codex 自动化的 jitter salt/TOML/memory 哈希均与
运行前完全一致；`latest_v3_clean.json` 与 `latest_portfolio.json` 仍不存在。
没有触发自动化、回测、选股、评分、交易、Claude Stock 集成、commit、push、Release、
部署或外部发布。

剩余不可消除限制：本次冻结原始响应、请求元数据和交易日连续性能够证明“本次观察到的
Tushare 返回”以及本地产物的可追溯复算，但不能反向证明提供方在首次观察之前从未调整、
修订或截断历史数据；后续若刷新数据，必须取得新的准确授权并创建新的不可变运行，不能
覆盖 RUN-004 或复用本决策。

成功后的回滚不得改写本记录或历史 run。若未来需要退役项目，只能通过新 successor 决策
把项目目录整体移出 canonical root，并追加更新登记表；不得静默删除、覆盖或把本次
research-only 产物接入生产。本记录是 `GOV-20260830-001` 的最后一次治理写入。


## GOV-20260901-001-AUTHORIZED｜申万行业估值仪表盘 v2 本地应用与三级分类 successor

- 记录类型：`PROJECT_V2_GOVERNED_RESEARCH_AUTHORIZATION`
- 记录时间：`2026-09-01T11:51:32+08:00`
- `decision_id=GOV-20260901-001`
- 前置关闭：`GOV-20260830-001-FINAL_CLOSED`
- 用户批准原文：`PLEASE IMPLEMENT THIS PLAN:`，其后完整粘贴并批准“申万行业估值仪表盘 v2 实施计划”。
- 授权状态：`ACTIVE_UNTIL_FINAL_OR_FAILED_CLOSE`

### 目标与冻结语义

本 successor 只允许把既有独立 research-only 仪表盘升级为本地浏览器应用：当前轴扩展
为 SW2021 L1/L2/L3，SW2014 继续仅保留既有 L1 历史档案；行业估值仍只使用
`sw_daily.pe/pb/close`，禁止用个股聚合替代。新增成分股点时查询和当日
`daily_basic` 估值快照；PE_TTM/PB 同业百分位固定为有限正值上的
`count(x <= current) / valid_N * 100`，包含自身并披露 N、并列与覆盖，N 小于 5 时不可用。
成分边界证据不足、重叠路径、分类/行情身份冲突或双轮成员漂移必须显式 UNKNOWN/BLOCKED，
不得静默选择、补值或复用其他项目的一次性修正规则。

### 准确允许的接口、运行和文件范围

1. Tushare HTTPS 白名单扩展为 `trade_cal/index_classify/sw_daily/index_member_all/daily_basic`；
   只允许读取 `TUSHARE_TOKEN` 或 Tushare 本机配置，token 仅驻留内存并禁止出现在参数、
   URL、网页、日志、错误、进度、raw、manifest 或任意产物。
2. 允许修改本项目合同、规格、README、Python 源码、测试、项目内维护/架构/运行文档，
   新增 `pyproject.toml`、本地 HTML/CSS/JavaScript 资源及 macOS/Windows 启动脚本。
3. 允许新增只绑定 `127.0.0.1` 的本地 HTTP 服务、独立后台更新进程、跨进程单写者锁、
   手动 `UPDATE_LATEST/MATERIALIZE_DATE` 作业、进度状态和懒加载快照 API。每次页面明确点击
   或 CLI 明确命令构成该准确目标日期的一次运行意图；不授权定时器、通知或无人触发更新。
4. 允许在本项目 canonical root 或显式用户数据目录创建新的不可变 v2 run、job 状态、
   运行 ledger 和内容寻址 pointer；历史日期作业不得更新 current latest，失败运行不得更新
   任何 latest。允许复制经验证的历史数据目录到另一电脑，但 token 不得随迁移。
5. 允许只修改 `docs/project_registry.json` 中本项目对象及顶层决策时间，向本文件只追加
   本授权、必要 successor 和唯一最终成功或失败关闭记录；不得改变其他项目对象。

### 状态、验证与禁止事项

所有新输出固定为 `RESEARCH_ONLY`、`decision_eligible=false`、
`production_approved=false`。必须保留 RUN-001 至 RUN-004 原字节和旧 schema 验证能力。
离线门必须覆盖三级分类、发布状态、Y/N 成员生命周期、边界/重叠/漂移、个股估值空值与
样本不足、禁止行业估值替代、增量父血缘、并发/中断/latest 原子性、Host/Origin/nonce/
路径穿越、token 金丝雀、中文与空格路径及 v3 兼容。真实门必须覆盖权限/行限探针、首个
v2 基线、一次日更、一次历史日期物化、断网重建、独立重放、Chrome 和哈希闭包。

禁止：自动化调度、通知、云部署、账户系统、网页保存 token、Docker/Kubernetes、个股
自身多年历史百分位、回测、评分、交易、Claude Stock 或资金流项目集成、commit、push、
Release、外部发布及任何生产状态修改。Windows 只可标记设计支持；真实 Windows E2E
完成前必须保持 `WINDOWS_E2E_UNVERIFIED`。

### 修改前事实与回滚

修改前备份位于 `/tmp/swivd-v2-preflight.DG8hIv`。关键 SHA-256：`AGENTS.md`
`3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`，根 README
`b89126c6baafd7beb04bcee39bb67a77be7b193dd31bf5701889f0914e3653a8`，登记表
`74a134f75ca25ff1586e7326d6a8fcaf47d142559eeb7a02095a7185c724d993`，本日志追加前
`102007d0ffcef380f26948d51b4662a1205d9123f5ab2991ff860cd62ba13cbe`，合同
`b27a8ac50a7c924cad9c9cbd8a326763a9cb4039955c1eaee617e97aee2e5b8d`，规格
`c872516118bda0f2b7429d384f6b4ab97d0105a81ba3e416faf5785bfd9a0b98`，latest
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`，ledger
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`，RUN-004 manifest
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`。

失败时只允许逐文件恢复修改前字节、保留失败 run/job 证据并追加 FAILED_CLOSED；不得删除
本授权、历史 run 或 ledger。成功后若需回滚，必须另建 successor，验证目标 manifest 哈希
后追加新 pointer；不得原地改写历史或复用本决策。


## GOV-20260901-001-FAILED_CLOSED｜v2 实现完成但真实基线因上游身份冲突失败关闭

- 记录类型：`PROJECT_V2_GOVERNED_RESEARCH_FAILED_CLOSE`
- 记录时间：`2026-09-01T12:50:23+08:00`
- 父决策：`GOV-20260901-001`
- 授权状态：`CLOSED_FAILED_NO_REUSE`
- 结论：本地应用、离线数据链与安全门已实现并通过隔离验证；真实 SW2021 L3
  分类/行情身份未闭合，Chrome 扩展连接不可用，因此没有可发布的 v2 真实基线、日更或
  历史日期快照。本记录不得被表述为真实 E2E 完成。

### 已实施范围与源码身份

新增 v2 合同 `PROJECT_CONTRACT_V2.md`、v4 机器规格、三级领域计算、唯一 Tushare
provider、不可变快照/父血缘、跨进程单写者锁、后台作业状态机、loopback HTTP 服务、
Host/Origin/nonce/JSON/CSP 门禁、原生 HTML/CSS/JavaScript 懒加载 UI、历史日期显式载入、
CLI doctor/serve/update/materialize-date/rebuild-v2、固定依赖、macOS/Windows 启动脚本、
架构/数据字典/运行/故障文档和项目内维护规则。v1 冻结合同、规格、输出与验证入口保留。

排除 `output/` 与可重建缓存后的 37 个项目源码/文档文件形成确定性树哈希
`04e859bebb491355851e6ae07a486cf1ef4de9350c6124a93eed87ffe041e1c9`。
v2 合同 SHA-256 为
`bb5698c9232d6caf77764dc48f31a22e8710e686fef5274b7b1ffab6c17c062b`，v4 规格为
`b40aa76c0e694bc2ed9c3d0fb250a0d41617c7fc4b7b572e32a64f29f9888fba`，CLI 为
`ad072993f54d355bcb580fdc625ce95244e8b641432475bc3ba24180b9e65749`。项目登记表最终
SHA-256 为 `b90a91c63d2260f4a51d88f018be0e172caf2fc24f9ac581c2d3c915d8229658`；
本记录追加前决策日志 SHA-256 为
`91940802b339a9247279dd087596d36180afddc0e1543a642217508ee03a768b`。

### 离线与本地服务验证事实

1. 最终离线套件共 102 项：沙箱内 101 项通过，唯一跳过项是沙箱禁止 loopback bind；
   同一 Host/Origin/nonce/Content-Type/CORS 测试在宿主环境独立执行并通过，因此必需测试
   没有未验证 skip。源码编译、JavaScript 语法、v3/v4 规格验证与 RUN-004 独立验证通过。
2. v2 fake-provider 全链在含中文和空格路径建立 511 个行业目录分片，完成不可变基线、
   历史日期物化、失败不改 latest、历史作业不改 current、断网字节级重建、父 manifest/raw
   自包含血缘和 RUN-004 不变复核。独立验证器成功拒绝重封后的错误 PE_TTM 百分位和 token
   金丝雀文件。
3. loopback 服务实际返回 CSP、本地 HTML/CSS/JS、L3 分片和 5 条成分股；历史日期控件可见，
   三级目录合计 511。恶意 Host、缺 nonce、错误 Content-Type 和 OPTIONS/CORS 请求分别被
   421/403/415/405 拒绝，合法同源 JSON 作业返回 202。服务从未监听非 loopback。
4. 使用本机真实配置 token 作为金丝雀扫描 1003 个最终项目/运行/隔离验证文件、进程参数
   和最终 HTTP payload，文件、argv、payload 命中数均为 0。只报告布尔来源类型，未输出
   token。三个测试生成的 `__pycache__` 目录已删除；它们可由 Python 重新生成，不含研究数据。

### 真实接口失败关闭证据

真实只读权限与行限探针成功：`trade_cal=32` 行，SW2021 分类严格为
L1=31/L2=134/L3=346，`sw_daily(20260831)=439` 行，抽样 L1 成员 Y=126/N=58，
`daily_basic(20260831)=5545` 行。成员抽样同时确认 Y 的 out_date 全空、N 的 out_date
全非空，符合冻结的生命周期规范化前提。

发布子集计数为 L1=31、L2=124、L3=259；L1/L2 当日行情齐全，L3 唯一缺少分类发布代码
`850401.SI（特钢Ⅲ）`。对该代码单独查询 2026-08 月 `sw_daily` 返回 0 行，而当日行情
出现分类目录外的 `850412.SI（特钢Ⅲ）`。名称相同不能证明身份等价，且父授权明确禁止
按名称映射或复用一次性修正，故系统按合同失败关闭。

默认数据目录的真实尝试 `SWIVD2-RUN-20260831-001` 在五个请求后以
`CURRENT_INDUSTRY_ROW_MISSING` 终止；`failure.json` SHA-256 为
`ecbfe87ca2568c5a6cd67536193b2c322c6890bad0c583f7ee70b57b2c9aa381`，追加 ledger
SHA-256 为 `9ec12361921a4b53b61ed6267d83b0e4b90998e099fdcf1f43422270bcd74bbd`。
失败目录保留原始交易日、三级分类和当日行情响应及请求哈希。默认数据目录没有
`latest_run.json`，没有把失败 run 伪装成基线。

### 未通过门、保护状态与后续权限

Chrome 控制 skill 已按要求尝试连接真实 Google Chrome，但当前主机没有可用的 Chrome
浏览器扩展连接；依照浏览器合同没有用内置浏览器、Edge、headless CLI 或其他表面替代，
故真实 Chrome E2E 保持 `UNVERIFIED`。真实 v4 基线未成立，页面真实日更、真实历史日期
物化、真实冻结输入断网重建与真实快照独立重放均不能继续；Windows 仍为
`WINDOWS_E2E_UNVERIFIED`。

Stock 根 `AGENTS.md`、根 README、旧 v1 合同、旧 v3 规格、旧 latest、旧 ledger 和
RUN-004 manifest SHA-256 分别保持
`3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`、
`b89126c6baafd7beb04bcee39bb67a77be7b193dd31bf5701889f0914e3653a8`、
`b27a8ac50a7c924cad9c9cbd8a326763a9cb4039955c1eaee617e97aee2e5b8d`、
`c872516118bda0f2b7429d384f6b4ab97d0105a81ba3e416faf5785bfd9a0b98`、
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`、
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`、
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`。
没有修改 Claude Stock、资金流、baseline、authority、factor、portfolio、生产、自动化，
也没有 commit、push、Release、部署或外部发布。

本次授权在此失败关闭。任何采用 `850412.SI` 替代 `850401.SI`、建立明确的代码修正表、
等待 Tushare 修复后重试、创建真实基线、运行页面更新/历史物化或完成 Chrome/Windows E2E，
都必须取得新的准确 successor 批准；不得复用 `GOV-20260901-001`。逐文件回滚可使用
`/tmp/swivd-v2-preflight.DG8hIv`，但不得删除本记录、真实失败 run 或追加式 ledger，且不得
覆盖 RUN-001 至 RUN-004。


## GOV-20260901-002｜原压缩包视觉一致性与本地页面加载修复授权

- 记录类型：`PROJECT_UI_REPAIR_AUTHORIZATION`
- 记录时间：`2026-09-01T13:59:06+08:00`
- 父决策：`GOV-20260901-001-FAILED_CLOSED`
- 用户任务：定位页面与原压缩包样式完全不同的原因，找到问题后实施修复并进行对抗式审查
- 授权状态：`ACTIVE_LOCAL_CHANGE_ONLY`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 已核验问题与权威视觉证据

用户截图中白底、默认表单、`.hidden` 区域同时展开、行业目录和动态数据为空，证明
`app.css/history.css/bootstrap.js/app.js` 均未加载。`web/index.html` 使用根绝对资源路径；
直接以 `file://` 打开时四个路径解析到文件系统根，形成截图中的裸 DOM。HTTP 服务现有静态
路由表面正确，但此前把源码 HTML 当作可直接打开的页面属于交付和验收缺陷。

原附件本体已被系统共享粘贴板清理；其只读审计保存的 `step3_visualize.py` 原始输出及 ZIP
SHA-256 `3d21d0182a46c26d5e997dd66b5e6d4a00d977be17688e8d6020218d9dc3876e`
构成视觉基准：GitHub-dark 色板、粘性顶部与六标签信息架构、摘要卡、条形/饼图/雷达、
热力图、历史走势、排行、PE-PB 散点和明细表。当前 v2 即使加载 CSS，也仅剩左侧目录与
单行业表格，属于结构性 UI 回归。原 ZIP 的 baostock、伪申万映射、个股聚合行业估值、
“低估/高估”结论和 ECharts CDN 均不是可恢复的设计权威。

### 允许动作与准确文件范围

1. 允许修改项目 `web/index.html`、`web/app.css`、`web/history.css`、`web/app.js`，恢复原 ZIP
   的深色视觉语言与六类全景视图，并嵌入 v2 的 L1/L2/L3、成分股、历史日期、手动更新、
   安全进度和显式快照切换；图表只能使用本地原生 HTML/CSS/SVG/JavaScript。
2. 允许修改 `src/swivd/v2_pipeline.py`，把已经计算并已写入行业汇总表的只读摘要字段复制到
   `ui/catalog.json`，避免页面一次请求 511 个分片；不得新增或改变任何估值、收益、成员公式。
3. 允许修改 `src/swivd/v2_validator.py`，验证目录摘要与行业分片一致、禁止远程资源，并保持
   旧 schema 的验证兼容；允许修改 `src/swivd/v2_server.py`，增加固定白名单的 SW2014 档案
   只读目录/单行业接口，不得执行档案 HTML 或放宽 CSP、Host、Origin、nonce、CORS 门禁。
4. 允许修改 `tests/test_v2.py` 及必要的项目合同、README、运行手册和故障排查文档，补充
   CSS/JS HTTP 状态和 MIME、六类视图、三级切换、file 误开失败提示、目录摘要一致性、档案
   只读、无 CDN、token 零暴露和视觉结构回归测试。所有运行只写系统临时目录且禁止联网。
5. 允许只修改 `docs/project_registry.json` 中本项目对象和顶层决策时间，并向本文件追加本授权
   及唯一最终成功或失败关闭记录。

### 明确禁止与关闭条件

本授权不允许真实 Tushare 请求、真实 `UPDATE_LATEST/MATERIALIZE_DATE`、建立或切换 v2
真实基线、修改任何 `latest`、ledger、既有 run、manifest、哈希、数据公式、数据源、分类
身份或 850401.SI/850412.SI 处理规则；也不允许 CDN、远程字体、网页 token、自动打开浏览器、
非 loopback 监听、commit、push、Release、部署或外部发布。RUN-001 至 RUN-004 和
`SWIVD2-RUN-20260831-001` 必须保持原字节。

关闭前必须通过：JSON/语法检查、完整离线套件、fake-provider 快照与断网重建、HTTP 静态
资源及安全门、目录摘要/分片一致性、六类视图和 L1/L2/L3/成分股交互结构、file 误开提示、
无远程资源/秘密扫描、旧 latest/ledger/RUN-004 哈希复核；真实 Chrome 连接可用时必须执行
视觉验收，不可用时必须如实保留 `CHROME_E2E_UNVERIFIED`，不得以其他浏览器冒充。

修改前备份位于 `/private/tmp/swivd-ui-preflight.EJkdVb`。关键 SHA-256：`web/index.html`
`8b26b16a7155b30da0af22ef4f2b92549038b0d2b587f99a86a01b50d0de2ac9`，`web/app.css`
`79d170d23bc8be89a44dd867abcca5519b6ed238222d8e9ae64ea03d034d4273`，`web/app.js`
`e861c47ef82f3aa057da7b5230fa650eebf895f89d366cd0093ef4b284b5994b`，pipeline
`db9a8901a7097a535d1480467265cb5f12597caf09d49713cebe3afc80b3c7a3`，server
`7bde1c433a99ba34eeeefecf96569020615dfbb6c81b2e712e52384f3c122a75`，validator/tests
修改前分别以备份字节为准，旧 latest `2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`，
旧 ledger `c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`，RUN-004
manifest `5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`。

失败时逐文件恢复上述备份并追加 `GOV-20260901-002-FAILED_CLOSED`；成功时追加唯一
`GOV-20260901-002-FINAL_CLOSED`。任何数据身份修正或真实运行仍需另行 successor。


## GOV-20260901-002-FINAL_CLOSED｜原压缩包视觉一致性与本地页面加载修复完成

- 记录类型：`PROJECT_UI_REPAIR_FINAL_CLOSE`
- 记录时间：`2026-09-01T14:49:58+08:00`
- 父决策：`GOV-20260901-002`
- 授权状态：`CLOSED_COMPLETE_NO_REUSE`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 根因与实施结果

用户截图中的白底默认控件并非主题色被轻微改坏，而是 `web/index.html` 使用 `/app.css`、
`/history.css`、`/bootstrap.js`、`/app.js` 根绝对路径后被按 `file://` 直接预览，四项资源均未
加载；`.hidden` 区域因而同时暴露。第二层根因是 v2 页面即使通过 HTTP 正常加载，也曾把
原压缩包的 GitHub-dark 六类全景仪表盘缩减为左侧目录和单行业详情，形成结构性视觉回归。

页面现改为相对同源资源，直接 `file://` 只显示本地服务启动指引；loopback 页面恢复原项目
深色变量、粘性页头/标签、摘要卡、总览、热力图、历史走势、涨跌排行、PE-PB 象限和明细，
并保留 v2 的 L1/L2/L3、行业成分、历史日期、后台进度、显式快照切换及 SW2014 只读档案。
图表使用本地原生 SVG/CSS/JavaScript，无 CDN、远程字体或内联脚本/样式。

两轮独立对抗审查进一步发现并已闭合：行业详情误读 `*_percentile_le`、总览抽样漏行、浏览器
重算历史位置标签、catalog v1/v2 消费不一致、主轴详情路由代码格式门与 catalog 不一致、
档案元数据未参与验证、趋势失败残留上一行业统计、新快照切换异常未处理、隐藏趋势页提前
请求分片、全局搜索不作用于两个行业选择器、单点历史重复日期布局，以及首次验证后继续服务
漂移磁盘字节的时间差。catalog v1 现在只用同一快照内已验证分类与分片做内存 v2 投影；
catalog、分片及档案 CSV 每次解析前均按首次验证所得 manifest 字节数和 SHA-256 复核。

### 验证证据与源码身份

最终离线全套执行 110 项，结果 `OK (skipped=3)`；三项 skip 均只因沙箱禁止 loopback bind，
随后在宿主 `127.0.0.1` 独立执行并全部通过，覆盖 Host/Origin/nonce/JSON/CORS、HTML/CSS/JS/
bootstrap 状态码、MIME、CSP、catalog v1 内存适配、验证后磁盘漂移拒绝、SW2014 catalog/detail
GET 和 POST 拒绝。fake provider 重新建立并验证 511 个行业分片、断网重建和 RUN-004 不变；
JavaScript 语法、v3/v4 规格、RUN-004 旧 schema 独立验证及项目登记 JSON 均通过。后置只读
对抗审查结论为无新增 P0/P1/P2。

排除 `output/`、`__pycache__` 和 `.pyc` 后，37 个源码/文档文件的确定性树 SHA-256 为
`b1f54618e6b5e0bbc64b654e559d071082070500f4e8b5e6e9bc3ed5e0094e36`。关键最终 SHA-256：
`web/index.html` `c44de33e68226b2506c01534f43fb346e5d5578a2cb77275f7bb1b788d5268f6`，
`web/app.css` `d9a0df46f804d42f44433e7295efc8864c66955d13be8995c1f72edf7d8755ab`，
`web/app.js` `f2668c802842562850bca29eb849fe3b73e1aedb1f1e2fe8a42c07bbc9f23216`，
pipeline `354560ca881bd53c993e8d5b0ab30213deba1e134e65305b917ea24832294370`，
server `73691c136ad2ecd2f75cfe067c53d30b969d27f8c5d74360127c2330aef899de`，
validator `a24f854e9af4cc5cadafd1f579747849cd8083d569f8a470992b1884d09ea113`，
tests `8d811a6860076c008b09b4b8044959ae7f7d8be8875cf1d3dff806d6a740473a`。项目登记表
SHA-256 为 `54b42dcd90e790873aa772f653724c9aba7e3f5c53ad5d7d3575e456fee7db29`；本记录追加前
决策日志 SHA-256 为 `f70d3204a075d92261f9fbbbb10b5914b533238d64e4784343961a0714ad545e`。

### 安全边界、未通过门与回滚

本机配置 token 仅作为内存金丝雀，扫描最终项目及两份治理文件共 275 个文件，命中数为 0；
网页源码也没有 token/localStorage/cookie 路径。测试生成的项目内 `__pycache__` 已删除并复核
零残留。没有发起真实 Tushare 请求、真实 UPDATE_LATEST/MATERIALIZE_DATE，也没有建立或
切换 v2 基线。

旧 `output/` 与实施前备份逐文件一致。Stock 根 AGENTS、根 README、旧 v1 合同、旧 v3 规格、
旧 latest、旧 ledger、RUN-004 manifest SHA-256 分别保持
`3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`、
`b89126c6baafd7beb04bcee39bb67a77be7b193dd31bf5701889f0914e3653a8`、
`b27a8ac50a7c924cad9c9cbd8a326763a9cb4039955c1eaee617e97aee2e5b8d`、
`c872516118bda0f2b7429d384f6b4ab97d0105a81ba3e416faf5785bfd9a0b98`、
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`、
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`、
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`。
默认数据目录失败 run 的 ledger 与 `failure.json` 也分别保持
`9ec12361921a4b53b61ed6267d83b0e4b90998e099fdcf1f43422270bcd74bbd`、
`ecbfe87ca2568c5a6cd67536193b2c322c6890bad0c583f7ee70b57b2c9aa381`。

真实 Chrome 已确认安装且扩展文件存在，但 Chrome 控制插件在获准启动 Chrome 并按排障合同
等待后重试一次仍报告浏览器不可用；未用内置浏览器、Edge、headless 或 Playwright 冒充，
故视觉终验保持 `CHROME_E2E_UNVERIFIED`。此前 `850401.SI/850412.SI` 上游身份冲突仍未解决，
真实 v2 基线继续 `BLOCKED`；本关闭只证明源码视觉修复、离线/loopback 行为和安全门，不证明
真实页面数据 E2E。

逐文件回滚基线位于 `/private/tmp/swivd-ui-preflight.EJkdVb`；旧全项目/产物基线位于
`/private/tmp/swivd-v2-preflight.DG8hIv`。回滚不得删除本记录、旧 run、失败 run 或 ledger，
也不得用旧页面恢复错误数据语义。本授权至此关闭且不得复用；Chrome 视觉终验、真实数据运行、
分类身份修正、commit、push、Release 或部署均须新的准确授权。


## GOV-20260901-003-AUTHORIZED｜UPDATE_LATEST 的 UTC+8 盘后截止时间目标日选择

- 记录类型：`PROJECT_UPDATE_TARGET_POLICY_SUCCESSOR`
- 记录时间：`2026-09-01T15:19:59+08:00`
- 父决策：`GOV-20260901-002-FINAL_CLOSED`
- 用户批准原文：`如果是因为0901数据是空的，说明失败原因是当天还没收盘，后续需要新增一个判断逻辑，看看UTC+8是不是到了更新时间，如果没到的话默认更新到前一个交易日数据，并说明原因。开启对抗式审查，确保不要出错。`
- 授权状态：`ACTIVE_LOCAL_CHANGE_ONLY`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 根因、冻结解释与准确范围

本 successor 只修正 `UPDATE_LATEST` 的运行前目标日期选择，不改变显式
`MATERIALIZE_DATE`、行业估值、收益、成员、个股横截面公式或供应商身份。Tushare 官方
`sw_daily` 口径为交易日 18:30 更新，因此规则冻结为：以 `Asia/Shanghai` 为唯一时区；
若今天是 SSE 开市日且本地时刻早于 `18:30:00`，直接把 `trade_cal.pretrade_date` 作为
目标日；恰好 18:30 起今天才有资格成为目标日；若今天休市，则使用其
`pretrade_date`，不得再多退一个交易日。该行为属于联网取数前显式选择目标日，不是请求
今天失败后的静默回退。

目标日选定后，`sw_daily` 空行、部分已发布行业缺行、schema 漂移、行限或
`850401.SI/850412.SI` 身份冲突仍必须失败关闭，不得继续回退或按名称映射。若已验证 current
恰好等于所选目标日，应返回 `ALREADY_UP_TO_DATE`，不得创建新 run、重复联网或改写 latest；
若 current 晚于所选目标日则失败关闭，不得回滚 current。

允许修改项目 `PROJECT_CONTRACT_V2.md`、`PROJECT_SPEC_V4.json`、`README.md`、
`run_dashboard.py`、`src/swivd/v2_pipeline.py`、`src/swivd/v2_jobs.py`、
`src/swivd/v2_validator.py`、`web/app.js`、`tests/test_v2.py`、`docs/runbook.md` 和
`docs/troubleshooting.md`；允许以 minor successor 更新合同/规格身份并保持旧 v2.0/v4
快照只读验证兼容。页面和 CLI 只可显示服务端生成的固定原因码、目标日、时区与截止时间，
不得回传自由文本上游错误或任何 token。允许只修改 `docs/project_registry.json` 中本项目
对象和顶层决策信息，并向本日志追加本授权及唯一最终成功或失败关闭记录。

### 验证、禁止事项与回滚

验证只允许 fake client、冻结输入和系统临时目录，至少覆盖 18:29:59/18:30:00、UTC 时刻
换算、周末/节假日、跨月/跨年、非法或空 `pretrade_date`、18:30 后缺数继续失败、已是最新
的幂等路径、current 晚于目标的拒绝、网页/CLI 原因一致、token 零暴露及旧快照验证兼容。
本授权不允许真实 Tushare 请求、真实 UPDATE/MATERIALIZE、建立或切换真实 v2 基线、修改
既有 run/latest/ledger/manifest、解决或绕过行业代码冲突、commit、push、Release、部署或
外部发布。

canonical 为非 Git 目录；修改前 37 文件确定性树哈希仍为
`b1f54618e6b5e0bbc64b654e559d071082070500f4e8b5e6e9bc3ed5e0094e36`。本次逐文件回滚
基线位于 `/private/tmp/swivd-cutoff-preflight.vDZAOE`。关键修改前 SHA-256：合同
`f451145df9829e24b8c201374a9fc23ec550359346ce450c5c435b9341a7c894`，规格
`b40aa76c0e694bc2ed9c3d0fb250a0d41617c7fc4b7b572e32a64f29f9888fba`，pipeline
`354560ca881bd53c993e8d5b0ab30213deba1e134e65305b917ea24832294370`，jobs
`2dbd459eb42e098d71ea338668c8553ad4f0e09af8ea9b482912bcba433f6333`，validator
`a24f854e9af4cc5cadafd1f579747849cd8083d569f8a470992b1884d09ea113`，app.js
`f2668c802842562850bca29eb849fe3b73e1aedb1f1e2fe8a42c07bbc9f23216`，tests
`8d811a6860076c008b09b4b8044959ae7f7d8be8875cf1d3dff806d6a740473a`，登记表
`54b42dcd90e790873aa772f653724c9aba7e3f5c53ad5d7d3575e456fee7db29`，本日志追加前
`51b63f03d70bf328db19839701c39486addf483d8b5ff30a459a9b19472e3cb8`。

用户运行数据只读保护：默认数据目录 ledger
`8a328c663821ecb616a078df2e9b8e733d5934539e558c91b209e839c3795818`，
`SWIVD2-RUN-20260901-001/failure.json`
`3b2fed927373c59ff40b16d0c884377cb1d97f82fd5f0a8eb7e384d7070f8cbb`，旧 RUN-004 manifest
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`；均不得修改。

### GOV-20260901-003 实施依赖澄清

前置调用链复核确认通用只读 `validate-spec` 分发器
`src/swivd/validator.py` 目前只识别精确字符串 `swivd-project-spec-v4`。为使本授权已经允许的
minor successor `swivd-project-spec-v4.1` 能通过同一只读验证入口，允许在该文件仅增加 v4.1
到既有 v2 validator 的路由，不得改变 v3 规格、研究算法或运行行为。该依赖未扩大用户批准的
日期选择目标；修改前 SHA-256 为
`c922a979e8a1ca087036f46c62f36b1b60f5c2623586604a6fd3304e12eef9ab`，备份已加入同一
`/private/tmp/swivd-cutoff-preflight.vDZAOE` 回滚闭包。本澄清追加前决策日志 SHA-256 为
`a7ee98b0dd0efa4018f81373190507c4daee2eb1ad9095656cb8605d5616e863`。


## GOV-20260901-003-FINAL_CLOSED｜UTC+8 盘后截止时间目标日选择完成

- 记录类型：`PROJECT_UPDATE_TARGET_POLICY_FINAL_CLOSE`
- 记录时间：`2026-09-01T15:56:27+08:00`
- 父决策：`GOV-20260901-003-AUTHORIZED`
- 授权状态：`CLOSED_COMPLETE_NO_REUSE`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 事实诊断与实现结果

用户 2026-09-01 14:55 的失败不是请求次数超限：冻结的 `sw_daily(trade_date=20260901)`
响应为 `code=0`、15 个字段、0 行、`attempt_count=1`，且同一失败 run 的 SSE 交易日历明确
记录 `20260901 is_open=1 / pretrade_date=20260831`。因此根因之一是开市日盘后数据尚未到
官方发布时间，而非限流；本轮没有增加会掩盖该事实的盲目限速。

`UPDATE_LATEST` 现先用单日官方 `trade_cal` 在 `Asia/Shanghai` 下选择目标日：开市日
18:30 前选 `pretrade_date`，恰好 18:30 起选择当天，休市日选当天日历行的
`pretrade_date`。选日与完整性检查严格分离；目标一经选择，空行、缺行、schema/身份冲突
仍失败关闭，不进行请求失败后的二次回退。CLI 的日历探针和完整运行已置于同一跨进程单写
锁内；网页作业使用 `swivd-job-v2` 保存目标日、固定原因码、时区、截止时间与评估时刻。

已验证 current 与目标日相同时返回 `ALREADY_UP_TO_DATE`，不创建 run、不重抓行业、成员或
个股业务数据、不改 latest；运行前的一次权威 `trade_cal` 选日探针仍保留。current 晚于
目标时返回 `TARGET_BEFORE_CURRENT`。页面在旧快照 A 上收到 current 快照 B 的 no-op 时不再
误称“当前页面已是最新”或隐藏 B 的切换入口；原因码采用 own-property 白名单并绑定
`target_selection.as_of == job.as_of`，成功结果也校验 run id、日期和 outcome 枚举。

合同/规格升级为 `swivd-contract-v2.1.0 / swivd-project-spec-v4.1`；验证器同时接受冻结的
旧 `v2.0/v4/GOV-20260901-001` 身份，但拒绝旧、新日期策略字段混杂。manifest schema 仍为
`swivd-local-snapshot-manifest-v2`，本轮没有向 manifest/audit 增加目标选择字段，也没有修改
任何历史 manifest；选择原因只存在于 job-v2 和 CLI 本次输出。

### 对抗验证与源码身份

最终完整离线套件执行 121 项，结果 `OK (skipped=3)`；三项 skip 仅因为沙箱禁止 loopback
bind，随后在宿主 `127.0.0.1` 临时端口独立执行并全部通过，覆盖 Host、Origin、nonce、
Content-Type、CORS、静态资源安全头/MIME 及 SW2014 只读接口。新增跨年选日与最终 UI 契约
另行复核 2 项通过；18:29:59/18:30:00、UTC 转换、跨月、休市、空行情不再回退、幂等、
current 倒退拒绝、CLI 锁顺序、结果身份和旧规格兼容均通过。JavaScript 语法、当前/旧 v4
规格、RUN-004 旧 schema 和项目登记 JSON 均通过；项目源码/文档无秘密赋值命中，网页无
token、localStorage、cookie 或 Authorization 通道，项目内无 `.pyc/.pyo` 残留。独立只读
审查未发现 P0/P1/P2。

排除 `output/`、`__pycache__` 和 Python 字节码后，37 个源码/文档文件的确定性树 SHA-256
为 `6f6feda4012273d0c837a9cf06c366bef0fca469ea8953a3c78a4a4e69bb7ea0`。关键最终
SHA-256：合同 `2d1a23a3b978573738e9a983b6f95c6c62bee5188cac74a99c696b3903f31853`，规格
`83ec12901bb865a2c550c11609b83f01f5e2d246e9e81b3536a6b84be8e2efb1`，pipeline
`df6b02b14eba1b97e4d4b8df44173fc5ec1d02f72c2ef3f661cf66411b82dc7d`，jobs
`c1898ea22fc620c047162f287c1a12495481a210b6be95d2c47c82357756df80`，v2 validator
`492ad4ed0e348ed81a34afafd3b866e4e4e9aa8538edbc3f6804fa87ee577911`，通用 validator
`fb74d4dcd50a8dbb663fd4a2d48198ada011b10160b38790d9cf4905fc53c4a2`，页面脚本
`bcefaf2d6bd9e43c8f6a2f15cbf8bd54baefb7dee6d1f49c3792abcb3fac01fa`，测试
`c19588dda6a78e0ac767e403eda53aa96cc65edd89aa7d47509ed27ddf4e9c05`。登记表最终
SHA-256 为 `86ca028c6d13917f696187545fa80a0025e26785e2825d9c0835c3458024b1b3`；本记录追加前
决策日志 SHA-256 为 `4dd4c1f1e15b6f2125341ee1afb78f1a804869a2562fc912c4c93371c36ed741`。

### 未通过门、正式状态与回滚

本次只执行 fake/frozen/temp 验证，没有真实 Tushare 更新、真实
`UPDATE_LATEST/MATERIALIZE_DATE`、基线建立、pointer 切换、commit、push、Release、部署或
外部发布。正式项目 latest、ledger、RUN-004 manifest 继续保持
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`、
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`、
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`。用户默认数据目录
ledger、现有 job、20260831/20260901 两个 failure 分别保持
`8a328c663821ecb616a078df2e9b8e733d5934539e558c91b209e839c3795818`、
`361d063d4432ca927719647a3e1ab8107bc84d469c1df5db1596edfbe0953a0c`、
`ecbfe87ca2568c5a6cd67536193b2c322c6890bad0c583f7ee70b57b2c9aa381`、
`3b2fed927373c59ff40b16d0c884377cb1d97f82fd5f0a8eb7e384d7070f8cbb`；v2
`latest_run.json` 仍不存在。

前一交易日 20260831 的独立冻结证据仍有 439 行行情，但 414 个已发布分类代码中缺
`850401.SI`，同时行情出现未登记的 `850412.SI`；因此日期逻辑完成不等于真实基线可用。
该身份冲突、真实 Tushare/Chrome/Windows E2E 与首个真实 v4.1 基线继续 `BLOCKED/UNVERIFIED`，
不得静默改码或宣称落地完成。运行中的旧 `serve` 进程不会热加载 Python/JavaScript 源码，
必须由用户停止后重新启动并刷新 Chrome 才会使用本实现。

逐文件回滚基线位于 `/private/tmp/swivd-cutoff-preflight.vDZAOE`；回滚不得删除本记录、旧 run、
失败 run 或追加式 ledger，也不得覆盖用户正式数据。本授权至此关闭且不得复用；解决行业
代码身份冲突、真实运行、Chrome/Windows 验收或其他语义变更均须新的准确 successor。


## GOV-20260901-004-AUTHORIZED｜SW2021 特钢三级端点感知身份解析

- 记录类型：`PROJECT_INDUSTRY_IDENTITY_SUCCESSOR`
- 记录时间：`2026-09-01T19:29:34+08:00`
- 父决策：`GOV-20260901-003-FINAL_CLOSED`
- 用户批准原文：`按照方案A执行，开启对抗式审查， 确保不要出错。`
- 授权状态：`ACTIVE_GOVERNED_CHANGE`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 已选方案、第一性原理与事实门

本授权采用此前向用户说明并由用户选定的方案 A：不把 `850401.SI` 直接替换为
`850412.SI`，也不按中文名称自动连接；稳定业务身份冻结为
`SW2021:L3:230501`，目录、官方行情和成员端点代码分别保存、分别验证。名称、父级路径、
发布状态和日期只作为精确失效门，不能替代业务主键。行业估值仍只来自官方 `sw_daily`，
不得由个股聚合补造；成员点时规则和个股横截面公式保持不变。

北京时间 18:30 后已在系统临时目录完成限速只读探针，共冻结 15 份 HTTPS 原始响应；探针
摘要为 `/private/tmp/swivd-identity-preflight.qeyF1v/probe/probe_summary.json`。事实结果为：
SW2021 数量 L1/L2/L3=`31/134/346`；目录中 `230501/特钢Ⅲ/230500/L3/is_pub=1`
唯一对应 `850401.SI`，`850412.SI` 未被其他分类占用；`20260901` 行情 439 行，唯一缺失
已发布代码为 `850401.SI`，唯一同名额外代码为 `850412.SI`；前者 20211213–20260901
历史为 0 行，后者连续覆盖全部 1,145 个 SSE 开市日；前者两轮 Y/N 成员均为 0，后者
两轮均为 Y=13/N=12 且路径严格为 `钢铁→特钢Ⅱ→特钢Ⅲ`。新增请求均一次成功，原始响应
和摘要中 token 命中为 0。因此当前分支认定为 `PERSISTENT_ALIAS_EVIDENCE_PASS`；该认定
不声称已知 Tushare 内部根因，也不继承其他项目的一次性修正授权。

### 准确允许范围

1. 将合同升级为 `swivd-contract-v2.2.0`、规格升级为
   `swivd-project-spec-v4.2`；新增稳定 `industry_uid`、原始目录码、行情来源码、成员来源码、
   当前解析码、解析状态、规则版本和证据哈希。新快照使用诚实升级后的 manifest/audit/catalog/
   shard schema；旧 v4/v4.1、manifest-v2 及既有快照必须继续只读验证兼容。
2. 在一个中央、版本化解析器中实现精确单例规则。当前仅允许
   `SW2021:L3:230501` 的候选集 `{850401.SI,850412.SI}`；每次运行必须重新验证目录唯一性、
   代码无碰撞、目标日唯一有效行情、双码历史无重叠/缺口、名称与路径、双码 Y/N 两轮原始
   稳定性，以及成员当前端点与行情当前端点一致。任何第二别名、双码同日、其他已发布行业
   缺行、历史断裂、成员不同步或证据漂移均失败关闭。
3. 原始响应永不改写；规范化历史和成员必须保留实际来源码，并投影到稳定业务身份。上游若
   收敛为单一码可进入 `DIRECT`；若未来出现无重叠、无缺口且成员同步的合法切换，可形成有
   生效日的受控区间；其他变化不得按名称猜测或静默沿用当前别名。
4. 允许修改本项目 `PROJECT_CONTRACT_V2.md`、`PROJECT_SPEC_V4.json`、`README.md`、
   `src/swivd/v2_domain.py`、`src/swivd/v2_provider.py`、`src/swivd/v2_pipeline.py`、
   `src/swivd/v2_validator.py`、`src/swivd/validator.py`、`web/app.js`、现有 v2 测试，新增
   身份解析模块及身份专项测试/夹具，并更新 `docs/architecture.md`、
   `docs/data_dictionary.md`、`docs/runbook.md`、`docs/troubleshooting.md`。仅在调用链证明
   必需时才可触及 `v2_jobs.py`、`v2_server.py`、`v2_storage.py` 或 `run_dashboard.py`；
   不允许修改 Tushare 端点白名单、token 来源或依赖。
5. 允许只修改 `docs/project_registry.json` 的顶层决策/时间和本项目对象，并向本日志只追加
   本授权及唯一最终成功或失败关闭记录。允许所有离线测试仅写系统临时目录；全部门通过后，
   允许在默认用户数据目录执行一次真实 `UPDATE_LATEST` 建立首个 v4.2 研究快照并独立验证、
   断网重建。真实失败只可追加 failure/ledger，禁止修改 `latest`；成功才可原子创建 v2
   `latest`。不执行真实 `MATERIALIZE_DATE`。

### 关闭门、禁止事项与回滚

关闭前必须覆盖：持续别名、上游单码收敛、合法无缝切换、双码并存、代码碰撞、第二别名、
历史断裂、名称/父级漂移、成员不同步、双码成员、两轮原始漂移、2000 行拆分、父快照退出
映射、重哈希篡改 normalized/audit/manifest 仍被独立验证器拒绝、失败不改 latest、断网重建、
511 个行业单实体、token 金丝雀零命中、旧快照兼容和完整注册离线套件。真实运行还必须复核
官方行业估值未由个股替代、原始证据闭包、受保护状态和 loopback 安全门；真实 Chrome 或
Windows 未完成时必须继续标记 `UNVERIFIED`。

明确禁止按名称模糊 join、私改 `is_pub`、忽略缺失行业、用前值或个股聚合补行情、删除或
回退既有失败 run/ledger、修改旧 RUN-004、写入其他 Stock 项目、自动切换/前台打开浏览器、
定时任务、通知、账户、云部署、commit、push、Release、部署或外部发布。

canonical 为非 Git 目录，无 HEAD/clean-tree 证明。修改前逐文件备份位于
`/private/tmp/swivd-identity-preflight.qeyF1v`。关键修改前 SHA-256：合同
`2d1a23a3b978573738e9a983b6f95c6c62bee5188cac74a99c696b3903f31853`，规格
`83ec12901bb865a2c550c11609b83f01f5e2d246e9e81b3536a6b84be8e2efb1`，domain
`313f7ab24a964738a958f0129a0e435f4bddc5d16f4e2448cf1ea25104586d6a`，provider
`56c84b6fb93ef92210ad0b1db94a9f0051a4ab5d6ceac453cc1fe3e9d8f6efb7`，pipeline
`df6b02b14eba1b97e4d4b8df44173fc5ec1d02f72c2ef3f661cf66411b82dc7d`，v2 validator
`492ad4ed0e348ed81a34afafd3b866e4e4e9aa8538edbc3f6804fa87ee577911`，通用 validator
`fb74d4dcd50a8dbb663fd4a2d48198ada011b10160b38790d9cf4905fc53c4a2`，页面脚本
`bcefaf2d6bd9e43c8f6a2f15cbf8bd54baefb7dee6d1f49c3792abcb3fac01fa`，现有测试
`c19588dda6a78e0ac767e403eda53aa96cc65edd89aa7d47509ed27ddf4e9c05`，登记表
`86ca028c6d13917f696187545fa80a0025e26785e2825d9c0835c3458024b1b3`，本日志追加前
`6b7d3221a227a47c1ee2d840a70ad452db67d052aeec2a7d141cf0a065c8402e`。

受保护状态：项目旧 latest `2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`、
旧 ledger `c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`、
RUN-004 manifest `5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`
必须保持原字节；默认 v2 `latest_run.json` 当前不存在，runtime ledger 当前为
`1fd8a1dc184e3be41da3aaf2643ae3442892c697812096443d38258ea51bef1c`，已有四个失败 run，
均只能保留和追加。失败时逐文件恢复项目/登记表备份并追加
`GOV-20260901-004-FAILED_CLOSED`；治理日志不得回退。成功后如需撤销真实 v2 latest，必须
另建 successor pointer，不得删除本次历史或原地改写 ledger。


## GOV-20260901-004-FINAL_CLOSED｜SW2021 特钢三级端点感知身份解析完成

- 记录类型：`PROJECT_INDUSTRY_IDENTITY_FINAL_CLOSE`
- 记录时间：`2026-09-01T21:41:09+08:00`
- 父决策：`GOV-20260901-004-AUTHORIZED`
- 用户方案批准原文：`按照方案A执行，开启对抗式审查， 确保不要出错。`
- Chrome 验收授权原文：`允许启动chrome完成验收。`
- 授权状态：`CLOSED_COMPLETE_NO_REUSE`
- 研究状态：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`

### 方案 A 的最终身份结论

稳定业务身份冻结为 `SW2021:L3:230501`。目录端点继续原样保存
`catalog_index_code=850401.SI`；官方行情、成员和目标日当前解析端点分别为
`quote_index_code=850412.SI`、`member_index_code=850412.SI`、
`current_index_code=850412.SI`。状态为 `EVIDENCE_GATED_ALIAS`，规则版本为
`swivd-special-steel-identity-v1`。页面必须披露“上游内部原因未知；本地仅做证据门连接”，
不得把本地解析写成 Tushare 官方更名，也不得按中文名称对其他行业推广。

真实运行再次满足授权门：目录中 `230501/特钢Ⅲ/230500/L3/is_pub=1` 唯一对应
`850401.SI`；`850412.SI` 未占用其他分类；`850401.SI` 没有有效历史或成员记录；
`850412.SI` 从 `20211213` 到 `20260901` 连续覆盖 1,145 个 SSE 开市日，目标日有唯一
行情，成员两轮 Y/N 原始结果稳定，当前成员 13 条。代码、名称、父级、发布状态、历史连续性、
双码无重叠、行情与成员端点一致均由不可变原始响应、规范化证据和独立 validator 闭合。
行业 PE/PB 仍只来自官方 `sw_daily`，个股 `daily_basic` 只用于成分股横截面，不存在个股
聚合补造行业估值。

### 实施与真实产物

合同/规格升级为 `swivd-contract-v2.2.0 / swivd-project-spec-v4.2`；新产物使用
manifest v3、audit v2、catalog v3、industry shard v2，同时继续只读兼容旧 v4/v4.1、
manifest v2 和 RUN-004。成员生命周期主键冻结为
`(l3_code, ts_code, in_date, out_date, is_new)`；完全重复记录可规范化去重，键相同而内容冲突
必须失败关闭。发布链实现可恢复事务
`PREPARED → pointer replace → COMMITTED → unique terminal`；`COMMITTED` 是唯一提交点，
提交前崩溃恢复旧 pointer 并唯一记录 interrupted，提交后崩溃保留新 pointer 并幂等补齐成功
终态，未决 intent 会阻止 pointer 读取。

授权范围内唯一一次真实 `UPDATE_LATEST` 已成功，未执行真实 `MATERIALIZE_DATE`。产物为
`SWIVD2-RUN-20260901-002`，目标日 `20260901`，manifest SHA-256 为
`6f8422dedaf820866e38b9d009a7f8c28a3945c527535dee46001509bcd49514`。manifest 记录
`execution_status=COMPLETED`、`artifact_publish_state=LOCAL_RESEARCH_CANDIDATE_COMPLETE`、
`live_validation_state=PASS`、`provider_kind=LIVE_SECURE_TUSHARE`、
`RESEARCH_ONLY / false / false`。目录数量 L1/L2/L3=`31/134/346`，成员生命周期 7,904 条、
目标日成员 5,898 条、个股估值 5,546 条、同业投影 17,694 条。

本次共 553 个 HTTPS、拒绝重定向、字段白名单请求，全部 `attempt_count=1`；其中
`trade_cal=1`、`index_classify=3`、`sw_daily=416`、`index_member_all=132`、
`daily_basic=1`。没有 429、重试、达到或超过接口行限的响应，因此先前冲突不是请求次数超限，
不得以盲目限速掩盖身份或完整性错误。

### 对抗验证与 Chrome 真机验收

最终完整离线套件执行 161 项并通过，3 项因沙箱禁止 loopback bind 的测试随后在宿主随机
`127.0.0.1` 端口独立执行并全部通过，覆盖 Host、Origin、nonce、JSON Content-Type、
CORS、CSP、安全 MIME、SW2014 只读接口。发布事务另以三个 `os._exit` 子进程覆盖提交前、
提交后和恢复幂等。合法持续别名、上游单码收敛、合法无缝切换、双码并存、代码碰撞、第二
别名、历史断裂、名称/父级漂移、成员不同步、双码成员、两轮漂移、2000 行拆分、父快照退出
映射、规范化/审计/manifest 重哈希篡改、失败不改 latest、旧 schema 兼容均已覆盖。

独立 `validate-run` 对真实 run 为 PASS；冻结输入断网重建比较 516 个文件，全部逐字节一致。
项目源码、默认数据目录和本地 HTTP header/bootstrap/catalog/industry payload 合计扫描均为
token 零命中；浏览器不提供网页 token 输入，进程参数、日志和页面控制台也没有 token。

经用户单独授权启动 Google Chrome 152 的 Default profile，在当前代码临时 loopback 服务
完成真实 E2E：页面加载 `SWIVD2-RUN-20260901-002 / 20260901`，L1/L2/L3 分别显示
`31/134/346`；特钢三级选择项显示 `850412.SI / EVIDENCE_GATED_ALIAS`，详情同时披露
UID、目录 `850401.SI`、行情/成员/当前 `850412.SI`、规则版本和未知上游原因；官方
PE/PB、1,145 个历史样本、13 条成分记录及 PE_TTM/PB 横截面信息真实渲染。成分股搜索从
13 条筛到“中信特钢”1 条后可恢复 13 条，控制台无 warning/error。未点击更新或历史载入，
临时 8766 服务已停止。

### 哈希闭包、受保护状态与限制

排除 `output/`、`__pycache__` 和字节码后，40 个项目源码/文档文件的确定性树 SHA-256 为
`601d8a534a3f9179aa5b2a947629abaa0cad6e0403c227efef967d9fef44a450`。关键 SHA-256：合同
`3ca7ca326ca1e9520462eb2eaec3683c50a5b066a4fd0934aecad14100425904`，规格
`4ca68fbc0eeabdf8ae8afe1092d7cfaaabd01977003c0aca37e1daa00763d541`，入口
`95dd5b0f94450b09973413d1d0a17149602db6dccf6911e8a7dfe4fc8e9e3d2f`，身份解析器
`4054703ec7a95efccd02fcc7483adce1fbe5a66514ed17c3080edbea97817e46`，pipeline
`211c0eb8396cde188952ba76fcd14fb20202eb7fd0e7175dea5b553d060e6788`，provider
`90482f7a66ed7f6e78c0743f6ad5a7bd9652354f5d800422e7bc493fff0dcb66`，validator
`23b6458334a038c3bfd05ab391bc3bfc8ee5f2cb187b58443795a605ea1b3967`，storage
`8bea6e230d46ea6debeb817b6b152574a1f9930fcb34a10dfb3155f2819f4846`，jobs
`37c9519e6e78e15decc2faf678ee02d992ddd4d3ce990693bedf066bbc2570ee`，页面脚本
`bc3d2cd4414f331735d0147761611a829edd94d4907f33017d8ebe7b5c4f7784`。登记表由修改前
`c8ccef57d790ed24b257f2aba0cebbda38e01f158df7521fbfd8338163a532b0` 更新为
`2aa959906b3d6d5f28141cd5270a75518ab6859100ce8178801000d5c485c03e`；本记录追加前日志
SHA-256 为 `c79b9154e44dcc86a2f1a03a1fea65106e0877a63565e4a2686c9285d1b2c8c5`。

项目旧 latest、旧 ledger、RUN-004 manifest、RUN-004 SHA256SUMS 继续保持
`2ec3ac30170773134b75822f5f95d580f54fe81985bcca3f3640c82c6eafff75`、
`c3ffa8f8fdc2014c9e04d749624794f9ee6e8d02f592e5e9ddacae9c1a96d793`、
`5180fd5eb20e2cd556a5a5066039fba676272d735ae2d91b9b798e59b33daea0`、
`257557ad09aeedac9dfa5bbaf78ae6e62ba7fcf84d306442a30d928dcfb58437`。v2 runtime ledger
只有四条旧失败和一条本次成功；本次 run 恰好一条 `RUN_SUCCEEDED`，没有未决 transaction。
当前 `latest_run.json` 指向上述真实 manifest，pointer SHA-256 为
`bc4d537ea2688a5bd6643e0ab98a0b3194ce33ee0ed1d78ad43bb8c39332bfa7`，runtime ledger
SHA-256 为 `3faf662c5e754570624b05bd2ef851b7898e67b48284aab022e9d2e77292e0d7`。

真实 Windows 主机 E2E 尚未执行，必须继续标记 `WINDOWS_E2E_UNVERIFIED`；这不影响 macOS
本地研究使用，但不得宣称 Windows 真机已验。项目仍不是投资结论、交易系统或生产服务。
没有 commit、push、Release、部署、通知、定时任务或其他 Stock 项目写入。

如需回滚源码，使用 `/private/tmp/swivd-identity-preflight.qeyF1v` 的逐文件备份并单独复验；
不得删除本记录、真实 run、旧失败 run 或追加式 ledger。撤销当前 v2 latest、再次真实日更、
扩展其他身份别名或变更任何受保护语义，均必须建立新的准确 successor；本授权至此耗尽，
不得复用。


## GOV-20260906-001-AUTHORIZED｜SWIVD 第一批正确性与可维护性修复

- 记录时间：`2026-09-06T00:18:21+08:00`
- 父决策：`GOV-20260901-004-FINAL_CLOSED`
- 用户批准原文：`接受建议，按照你推荐的方式进行分批施工：1. 创建多个 Agent 和线程，加快施工节奏。2. 完成施工后开启对抗式审查，确保不要出错。`
- 批准上下文：本任务上一轮列出的第一批确定缺陷、单位/覆盖率、异常恢复、依赖与针对性测试；第二批证券上市生命周期、新数据来源、成员分母调整仍待单独确认。
- 状态：`ACTIVE_LOCAL_CHANGE_AND_ISOLATED_VALIDATE`；研究权限不变。

### 目标、事实、最小范围与分工

canonical 为非 Git 目录，没有 HEAD；修改前源文件、测试、页面、文档、登记和状态备份位于
`/private/tmp/swivd-v23-preflight.g016zG`。上一轮只读审查及临时反例确认：daily_basic 全空
仍成功、JSON 转义可绕过凭据明文扫描、父血缘递归增长、缺失图表连线、单位及覆盖率遗漏、
改名妨碍成员合并、当前身份混用于历史日期、轮询失败不恢复、只读启动受写锁阻断、并发首载
重复完整验证、依赖及启动脚本不完整。修复遵循原官方估值来源、百分位公式和边界未知规则。

允许修改本项目 `src/swivd/v2_domain.py`、`v2_identity.py`、`tushare_client.py`、
`v2_pipeline.py`、`v2_validator.py`、`validator.py`、`v2_storage.py`、`v2_jobs.py`、
`v2_server.py`，按调用链新增凭据/血缘/环境等必要小模块；允许修改 `web/`、
`run_dashboard.py` 的诊断集成、启动脚本、依赖文件及必要新增锁文件、相关 tests/docs/README。
允许当前合同和规格建立 `v2.3.0/v4.3` successor、新 manifest-v4 声明扁平血缘布局；
旧规格和旧 manifest 必须仍可离线验证，旧产物不改写。允许本日志追加开始/关闭记录、
项目登记仅更新本项目及顶层决策时间，并在项目 docs 保存本次准确 diff/验证/回滚证据。

并行任务独占文件：数据域 Agent 负责 domain/identity；运行 Agent 负责 jobs/server；
页面 Agent 负责 web；独立任务负责客户端凭据检查、迁移依赖/doctor/安装文档；主任务负责
pipeline/validator/storage/血缘、合同规格、治理、集成与最终验收。所有修复先读调用链，
不覆盖其他任务或用户改动。任何新增数据源、分母、通用别名、投资结论均不在本批。

### 验证与不变量

验证仅用显式假 provider 和系统临时目录；新反例先证明回归修复，再跑已登记全套离线测试、
规格校验、旧 RUN-004 与真实 v4.2 快照只读验证/断网重建、连续增量和长路径、凭据金丝雀、
HTTP 及前端行为测试，并由不同实现者独立对抗复核。可用临时隔离数据进行本机 Chrome
交互验收，但不得自动触发真实 API、正式更新或改变现有浏览器任务。真实 Windows 未验证
必须仍标 `WINDOWS_E2E_UNVERIFIED`。不以无网络测试冒称真实接口验收。

禁止本批运行真实 UPDATE_LATEST/MATERIALIZE_DATE、修改原始快照、latest、运行 ledger、
RUN-004、其他 Stock 项目、authority/baseline/factor/strategy/v3/portfolio/自动化状态，
禁止读出真实 token、新增 token 来源、定时更新、部署、commit/push/Release。

基线 SHA-256：合同 `3ca7ca326ca1e9520462eb2eaec3683c50a5b066a4fd0934aecad14100425904`；
规格 `4ca68fbc0eeabdf8ae8afe1092d7cfaaabd01977003c0aca37e1daa00763d541`；
登记 `2aa959906b3d6d5f28141cd5270a75518ab6859100ce8178801000d5c485c03e`；
追加前日志 `0660f9848315ab551d46687c510f1c0f35928226201f2c78663ac5b1eb87ef1b`；
v2 latest `bc4d537ea2688a5bd6643e0ab98a0b3194ce33ee0ed1d78ad43bb8c39332bfa7`；
v2 ledger `3faf662c5e754570624b05bd2ef851b7898e67b48284aab022e9d2e77292e0d7`；
真实 manifest `6f8422dedaf820866e38b9d009a7f8c28a3945c527535dee46001509bcd49514`。

回滚仅按修改清单逐文件从本批备份恢复并验证，新增文件逐项处理，不回滚追加式决策日志、
不删除运行记录。失败保留诊断和旧 latest，关闭结论按实际验证层级陈述。


## GOV-20260906-001-LOCAL_VALIDATED_ACCEPTANCE_PENDING｜第一批实现完成，浏览器验收待解锁

- 记录时间：`2026-09-06T01:31:39+08:00`；父记录：`GOV-20260906-001-AUTHORIZED`。
- `execution_status=LOCAL_IMPLEMENTATION_COMPLETED`；`artifact_publish_state=SOURCE_ONLY_NO_NEW_REAL_RUN`。
- `offline_validation_state=PASS_WITH_HOST_SUPPLEMENT`；`live_validation_state=NOT_RUN_FOR_V4_3`。
- `chrome_e2e_state=BLOCKED_MAC_LOCKED`；`windows_e2e_state=WINDOWS_E2E_UNVERIFIED`。
- `research_grade=RESEARCH_ONLY / decision_eligible=false / production_approved=false`。
- 多 Agent 和两个独立用户任务已完成本批独占文件施工、交叉对抗审查和最后反例修复。

当前合同/规格分别为 v2.3.0/v4.3，新快照格式 manifest-v4；准确变更为项目内 33 个实现、
测试及维护文件（9 个新增），详见项目 `docs/reviews/GOV-20260906-001-changes.json` 与
同前缀 `.patch`。全局登记仅更新本项目状态投影和顶层决策/时间；其他项目条目逐项比对不变。

锁定依赖临时环境整套收集 239 项：236 通过，3 个沙箱 HTTP 跳过；最后门禁后宿主 15/15
补测覆盖这三个跳过项；独立完整验证器的相等/倒挂/合法历史日期反例另 3/3 通过。最终源码
不是以一次 242 项整套运行声明通过，证据分层列明。真实临时依赖安装、pip check、时区、
移植专项通过；未修改全局/项目虚拟环境，未读取真实 token，也未访问 Tushare。

旧 RUN-004、真实 RUN-002 只读验证及其派生重放通过；终检 75/1130 个文件哈希闭包仍通过。
原 v1/v2 latest、运行 ledger、两份原 manifest 和 Stock 根 AGENTS.md 哈希与开始基准相同。
无真实 UPDATE_LATEST/MATERIALIZE_DATE、正式数据发布、投资/生产晋级、其他项目、
自动化、commit/push/Release 或外部消息写入。

Chrome 两次受 Mac 锁屏阻挡，已要求手动解锁；临时只读服务已停止，端口无监听且无残留
job/transaction。因此本记录不是 FINAL_CLOSED，也不沿用旧版本 Chrome PASS。后续只允许
继续本批已批准的隔离 Chrome 验收和必要证据收尾；不扩展第二批数据来源/生命周期/成员分母，
不默认创建真实更新。不得用本次离线通过替代真实接口或 Windows 主机验收。

证据文件：`sw_industry_valuation_dashboard/docs/reviews/GOV-20260906-001.md`。
合同 SHA-256：`8d605d695e7dc3f04f3f65ad2879aa6ae4752b0feae54e50042fcda151ddbc67`；
规格 SHA-256：`3495ff8d9727bd2bdf1cb4f9fe14ccec2db1b9fb799f9871e15b6ed1efc2ff26`；
变更清单 SHA-256：`f18c4265c744bd434459e7bcf4372afefd788250f380f224075eb81daf6d14a6`；
统一 diff SHA-256：`ae75e79098577b1112ef88a2a9ed2cb6455ea27ca8cc3917ff5e690db94e9135`；
验收报告 SHA-256：`f12c04769d3b9268c6fdf5d28b88a1a2056b24bed690b36ca12065ada47d7fa3`；
登记更新后 SHA-256：`d8a8dc9025cbf344410804bb6e455270e89f2093ab9dd0bc606e05b962c95153`。

回滚保持开始记录中的逐文件方法：先核对 after hash，再按备份或统一 diff 恢复 before hash；
新增文件仅逐项处理。不得整体回滚其他用户改动、删除旧快照或回退追加式日志。


## GOV-20260906-001-FINAL_CLOSED｜第一批本地源码与隔离验收关闭

- 关闭时间：`2026-09-06T10:27:22+08:00`。
- 用户续行批准原文：`继续完成验收。`；父记录为本决策 AUTHORIZED 与 LOCAL_VALIDATED_ACCEPTANCE_PENDING。
- 完成范围：已冻结第一批源码修复、此前离线/宿主/依赖验证，本轮真实 Chrome 只读及受控 worker 交互验收。
- `execution_status=LOCAL_IMPLEMENTATION_AND_ISOLATED_ACCEPTANCE_COMPLETED`。
- `artifact_publish_state=SOURCE_ONLY_NO_NEW_REAL_RUN`；`live_validation_state=NOT_RUN_FOR_V4_3`。
- `chrome_e2e_state=CHROME_READONLY_AND_CONTROLLED_WORKER_PASS`。
- `windows_e2e_state=WINDOWS_E2E_UNVERIFIED`。
- `research_grade=RESEARCH_ONLY / decision_eligible=false / production_approved=false`。

Chrome 152.0.7977.76、锁定依赖 CPython 3.14.6 下完成三层数量、特钢身份、
成分单位/覆盖率及搜索不改分母、未发布行业、键盘排序空值置后、SW2014档案、
焦炭真实112个空值的断线图、散点键盘及无error/warn观察。受控 worker 停在38%时
仍可浏览，刷新恢复同一作业；受控失败恢复按钮并保留旧快照，受控无新数据结果到100%。
独立Agent复核源码/状态、raw单位及真实缺口，并确认报告没有混淆真实与受控更新。

测试仅使用原真实RUN-002临时副本及显式替换的目标选择/worker，不调用provider或读取token；
固定时钟/目标不代表验收当天最新日期。不得将本记录称为v4.3真实数据更新、发布或历史生成E2E。
本轮没有新增产品源码变更：33/33文件仍匹配冻结清单。正式v1/v2 latest、ledger、
RUN-004/RUN-002 manifest、根AGENTS共7项哈希未变。临时run仍仅复制品1个；
仅两个测试job分别FAILED/SUCCEEDED终态，无新run/ledger/transaction。Chrome临时标签页已关，
服务已退出，18765无监听，临时写锁可重新取得。

本轮登记表只更新本项目及顶层时间；其他项目逐项相同。旧报告及先前final-evidence保持原样，
本次补充报告为 `sw_industry_valuation_dashboard/docs/reviews/GOV-20260906-001-CHROME.md`。
报告SHA-256：`ecd2a2e777e9404d0a031815eea729cac6c6d45066ef15beaa441c93e93888ca`；
观察JSON：`34c21ed9a588205eb2b1543c1f9260edb92c83312e43f1e2fa0bdc53264ef0c3`；
临时脚本存档：`5df98d7c15b4f352529f3301929006a57da23697a8db0056b8678f4ac98285f4`。
登记修改前后：`d8a8dc9025cbf344410804bb6e455270e89f2093ab9dd0bc606e05b962c95153` →
`065dfe2f56ff5766c8a3e65a1f2d164355b9ddf2f9f4dc0900f28ef0916e7422`；本记录追加前日志：`746f116b67cf4c242bb2ac8beee9470a6118972948dc6867f92849ee38951a30`。

本决策本地实现/隔离验收授权至此关闭，不能复用为第二批数据来源、证券生命周期、成员分母、
真实更新、生产权限、自动化、commit/push/Release或其他项目操作。首次完整验证耗时与Windows
真机缺口仍保留。回滚只按本轮治理差异恢复登记投影；历史日志追加撤销，不删除或改写本记录、
既有源码冻结证据和旧数据。没有可交付的新回测结论。


## GOV-20260906-002-AUTHORIZED｜本地 Token 真实隔离验证与非银金融取证

- 时间：`2026-09-06T10:41:00+08:00`；前序实施决策 `GOV-20260906-001` 已关闭，不复用其授权。
- 当前用户批准原文：`你读本地Tushare Token 完成验证。同时你确认一个问题，非银金融的估值水平是0.1是数据有问题还是本身就这样？`
- 准确范围：保持合同2.3/规格4.3/源码不变，经既有 `load_tushare_token → SecureTushareClient → V2TushareProvider → execute_update_locked/execute_locked` 完成真实 HTTPS 验证；仅在系统临时目录复制经验证的 RUN-002 与 pointer，运行 UPDATE_LATEST 和一次 20260901 MATERIALIZE_DATE、独立验证及断网重建。非银金融只读对照冻结历史、真实接口及官方口径。
- Token 只按现有 ENV→SDK本地读取顺序进入进程内存和目标 HTTPS 请求正文；禁止打印、落盘、argv、URL、页面和原始响应泄露。接口仅既有五项白名单，不安装或改变权限。
- 当前事实：项目非Git；RUN-002 as_of=20260901；正式manifest SHA `6f8422dedaf820866e38b9d009a7f8c28a3945c527535dee46001509bcd49514`；规格 SHA `3495ff8d9727bd2bdf1cb4f9fe14ccec2db1b9fb799f9871e15b6ed1efc2ff26`。非银金融0.1初步定位为PE历史百分位，原始PE跳变原因未知。
- 临时执行/evidence根：`/private/tmp/swivd-live-acceptance.8HyU1s`。允许新增项目 `docs/reviews/GOV-20260906-002*` 安全证据、追加本日志及仅本项目验收状态投影；不得改旧批准或旧证据。
- 受保护集合延用前序7项：根AGENTS、v1/v2 latest及ledger、RUN-004/RUN-002 manifest；执行前后逐项哈希对照。全局登记修改前SHA `065dfe2f56ff5766c8a3e65a1f2d164355b9ddf2f9f4dc0900f28ef0916e7422`；本日志追加前SHA `2cda4f85add2af7308a7c52440af35154cfe342748eade4b0ee0955ea7d1ccec`。
- 失败规则：权限、行限、schema、身份、连续性或验证失败即停止对应流水线，不补值、不改公式、不改正式latest；报告失败目录及残留。回滚仅处理本次临时副本和新增报告，登记按before备份逐字段恢复，日志仅追加撤销。
- 禁止：正式运行发布/数据pointer或ledger写入、baseline更换、公式和经济口径修正、其他项目、自动化、投资/生产晋级、commit/push/Release。真实数据产物若成功也仅为隔离验收候选，不是正式更新。Windows真机仍未验收。


## GOV-20260906-002-FAILED_CLOSED｜真实取数通过，更新安全验证阻断

本次授权验证已执行并失败关闭，不追加成功FINAL_CLOSED。真实取数成功不替代完整运行通过。
用户批准、本次边界和回滚沿用同决策AUTHORIZED；下一次源码闭包修复需新的准确批准，旧授权不复用。

- `execution_status=UPDATE_VALIDATION_BLOCKED`；`artifact_publish_state=NO_FORMAL_PUBLICATION`。
- `live_validation_state=FETCH_PASS_UPDATE_VALIDATION_FAILED`；历史载入、独立完整断网重建未执行。
- 本地Token经SDK读取，既有HTTPS客户端完成真实请求；更新作业142请求全部attempt=1，
  无行限/限流错误。三层31/134/346，特钢身份证据完整复核，目标交易日20260904。
- 失败准确原因：源码快照递归包含历史review，`GOV-20260906-001.md` 第84行明确假凭据doctor示例
  被最终secret扫描拒绝。非真实Token泄露；2435个临时文件实际Token扫描零命中。
  离线副本保持所有manifest声明字节，仅排除失败后追加标记，复现同一错误。
- 非银金融0.1是20260901 PE历史百分位：PE9.66，N1145，rank1；真实再取值相同。
  最新20260904 PE9.69，N1148，rank4，显示约0.3%。1145天PE/PB/close逐值比对无差异。
  PE跳变具体经济原因仍UNKNOWN，财报分母更新仅为假说；不据此产生投资结论。
- 33项源码哈希、7项受保护哈希及正式runtime 1163个文件前后不变。其他项目登记逐项相同。
  临时仅新增失败run及其RUN_FAILED ledger；临时latest未变、transaction不存在、写锁可重取。
  保留临时失败证据与诊断副本，无删除、正式发布、Chrome服务、自动化或commit/push/Release。
- `research_grade=RESEARCH_ONLY / decision_eligible=false / production_approved=false`；
  `WINDOWS_E2E_UNVERIFIED`。没有可交付的新回测结论。

主报告：`sw_industry_valuation_dashboard/docs/reviews/GOV-20260906-002.md`，
SHA `5fddcfb00785efb0b13ca89bc3db845ce6373c77c95ce6fa7ed8de1138c4d2d5`。
关闭证据SHA `6b2ec020b3bd54a89de9f001966b1cbe3bb8d31059f197fccdf37e4e0931e192`；
登记after SHA `2dde3ee0ccdbe1e8aed89af987ff3ed23ddfe70be1deb9b1f50131f769f53e1d`。
登记仅更新本项目验收状态及顶层时间/决策，统一diff为同前缀 `-registry.patch`。
本记录追加前日志SHA `acadbc7b7b61dedae89e8f933c7dad84d3d94b50d30d252942911fe46d9d59b6`。


## GOV-20260906-003-AUTHORIZED｜源码文档打包修复与隔离重验

- 批准时间：2026-09-06（Asia/Shanghai）；用户针对上一轮最小修复与重跑提议明确答复：`同意`。
- 上轮定位：历史review假凭据示例被递归打入源码，秘密扫描正确拒绝候选；实际Token扫描零命中。
- 准确修改：`sw_industry_valuation_dashboard/src/swivd/v2_pipeline.py` 的 `_copy_source_snapshot`
  仅保留现有四份运行/维护文档，停止递归收录review；新增 `tests/test_v23_source_snapshot.py`。
  独立验证器已有同一四份必要文档门禁与全量秘密扫描，无需改其语义。
- 合同、规格、数据schema、公式、成员规则、Token来源和安全扫描不变；新产物仍使用当前v4.3，
  manifest源文件哈希绑定修复字节；本决策授权修复，不复用已关闭的GOV-20260906-001/002。
- 项目非Git；修改前pipeline SHA `20bba1ad4b14dd02eb0ea83af50145c25531c9e3054932f5ceb7201bc88c635e`。
  备份/验证根 `/private/tmp/swivd-packaging-fix.YUp0LZ`；登记before SHA
  `2dde3ee0ccdbe1e8aed89af987ff3ed23ddfe70be1deb9b1f50131f769f53e1d`，日志before SHA
  `8478c551d9f5908e2714187cf941dcfc27f6a044f735f3655e1ac6615b0b77fe`。
- 回归覆盖：旧假凭据review不入包、四份必需文档存在且字节闭合、缺失或软链接文档仍失败、
  fake-provider新快照完整验证通过且打包产物中的凭据候选仍失败。运行已有离线套件和必要宿主补测。
- 用户已授权的真实Token仅进内存及Tushare HTTPS请求正文，不打印、不落盘、不通过argv/URL/UI传递。
  在新系统临时目录复制验证后的正式RUN-002及pointer，重跑真实UPDATE_LATEST、20260901
  MATERIALIZE_DATE及禁止联网进程的独立重建、中文空格单run迁移验证；新运行只更新临时状态。
- 旧review、旧失败run、旧manifest均保留；7项受保护集合及正式runtime全文件inventory前后核验。
  允许新增本决策review/机器证据、追加本日志、仅更新本项目登记验收投影；不改其他项目。
- 失败即保留证据与旧latest，不为通过而排除运行必需文件、放宽扫描或修改业务数据。
  回滚先核对after哈希，再逐文件恢复本次pipeline备份、移除本次新增测试；登记逐字段恢复；日志仅追加撤销。
- 禁止正式数据发布、正式latest/ledger更新、权限晋级、自动化、commit/push/Release。
  RESEARCH_ONLY / decision_eligible=false / production_approved=false；Windows真机仍未验收。


## GOV-20260906-003-FINAL_CLOSED｜打包修复及批准范围隔离验收通过

- 关闭日期：2026-09-06，Asia/Shanghai；批准引用为同决策AUTHORIZED及用户针对最小修复的`同意`。
- `execution_status=APPROVED_SCOPE_PASS`；`artifact_publish_state=SOURCE_FIX_AND_ISOLATED_CANDIDATES_ONLY`；
  `live_validation_state=UPDATE_AND_HISTORICAL_PASS`；`decision_eligible=false / production_approved=false`。
- 产品改动仅 `src/swivd/v2_pipeline.py` 文档清单；新增 `tests/test_v23_source_snapshot.py` 三项回归。
  四份维护文档仍必需，历史review不再入包；未修改validator、秘密扫描、公式、合同或规格。
- 完整宿主离线套件245/245，0错误/失败/跳过；临时runner首轮的导入路径错误保留，修正后完整重跑。
  真实日更20260904为142请求，历史20260901为140请求，作业请求全部首次成功。
  两个真实run均通过验证和临时发布；历史current pointer不变、两条独立RUN_SUCCEEDED记录。
- 新run分别复制到中文空格的独立目录，禁止联网进程完整验证及重建，各516派生文件逐字节一致。
  无publication intent残留，临时写锁可取得；3860个临时文件真实Token扫描零命中。
- 原实现32项不变、7项保护哈希不变、正式runtime1163文件不变、RUN-004全部75产物哈希闭合。
  旧review、旧失败run及前轮证据不变；本次未更新正式latest/ledger、未启动持续服务或浏览器。
- 本轮没有新增真实Chrome按钮E2E，原Chrome证据保留原范围；`WINDOWS_E2E_UNVERIFIED`。
  RESEARCH_ONLY，没有可交付的新回测结论；无其他项目、自动化、commit/push/Release或外部发布。
- 独立Agent复核了实际diff、必要文档、脚本、成功run的manifest/ledger/pointer/source闭包及最终报告，
  未发现实质问题。其阅读复核不替代上述程序验证。

报告：`sw_industry_valuation_dashboard/docs/reviews/GOV-20260906-003.md`，
SHA `640e323a71b6564fdef3781be210deeda20c8e7c01c2b92ea05082b406318b26`。
关闭检查SHA `603538b388023062dbddbd07deb5db46a478ad77f84fe4f90a403abb2974dccd`；
代码diff SHA `67a242bcb26d55908b0b425d6adb9d6fbb84eaff67dbd0e78e19b10e3fdb1ce8`；
pipeline after SHA `b3b0a6f8bd7a29912a16ee6318e11aa4040bb38e5f1e68cb00b4337a092cc6aa`。
登记after SHA `83467d5b676aec1c15f9dd517756ba58fca943e8128b91ab270554c277be1f5b`；
本记录追加前日志SHA `9b01cb6ec5432e690b3a2e0149099c78a3cc289c0b60b37d5dcb65f23381aa7c`。

本次修复与隔离重验授权至此关闭，不转为正式数据发布或后续源码/合同变化授权。
回滚使用报告中的逐文件diff、after哈希及临时before备份；登记逐字段恢复，历史日志仅追加撤销。


## GOV-20260906-004-AUTHORIZED｜分平台便携包构建与隔离验收

- 日期：2026-09-06，Asia/Shanghai。批准原文：用户要求“帮我把这个项目打包，确保移到别的 Mac 或 Windows 之后都可运行”，只需补充 Tushare token，并行 Agent 与对抗审查。
- 准确对象：`sw_industry_valuation_dashboard/portable/` 新增外层启动器、构建脚本、测试、中文指南与供应链清单；`portable-dist/` 新增本地交付包；`docs/reviews/GOV-20260906-004*` 新增证据；本日志只追加。不存在 Git 提交树，不进行 commit/push/Release 或外部发布。
- 已知事实与根因：现启动器依赖目标机已有 Python；日更依赖完整 RUN-004 档案；v4 seed 校验绑定原字节 source。最小方案为平台专用私有 Python 3.14.6、原锁定依赖、原字节 app、完整 RUN-004 及 9 月 4 日隔离验收 seed 的外层组合。不修改已有领域代码、公式、schema、合同、规格、manifest 或正式指针。
- seed 固定为前轮系统临时目录的 `SWIVD2-RUN-20260904-001`，manifest SHA `cfb0e20fa13fa76ea8fbdc1ac611b246f0f48bebd87e1b80f92bb03f522dba6a`。分发与启动导入只复制不可变字节；新包的数据根显式独立，禁止与正式 Application Support 数据合并。该种子是隔离验收结果，不宣称已切换正式 current。
- 允许从官方 Python/Astral/PyPI HTTPS 来源下载指定平台运行时及锁定 wheels，在系统临时目录解包、哈希核验、组装和执行必要验证；不安装系统 Python、全局依赖、虚拟机、插件，不改系统安全设置。默认覆盖 Mac arm64/x86_64、Windows x64；真实 Windows 主机缺失时保留 `WINDOWS_E2E_UNVERIFIED`。
- 入口 `外层启动器 → 原字节 run_dashboard.main → serve/v2_jobs/v2_pipeline`；Token 来源仍 ENV→本机 SDK，缺失时终端无回显输入到本进程 ENV。只保存在内存，不进入文件、argv、浏览器、错误正文或系统持久配置；可不输入而只读浏览。
- 失败场景：架构/系统不兼容、依赖/种子损坏、无回显终端不可用、中文空格路径、重复启动、端口冲突、导入中断、已有数据覆盖和供给链/凭据风险。必须失败关闭，不回退系统 Python、不修改旧 manifest、不覆盖已有 data。
- 验证：包内运行时锁定依赖/时区/legacy 门，最终压缩包在新中文空格路径解压、离线 seed 哈希验证和重建、loopback HTTP、启动器凭据与路径对抗测试，Mac 真机验证和 Windows 静态验证分开记录。真实 token 如用于必要安全扫描或最小 HTTPS 诊断只在内存，不追加正式数据更新；不启动持续服务或自动打开浏览器。
- before 证据：`/private/tmp/swivd-portable.Kgl6I0/boundary-before.json`；全局规则 SHA `3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`，登记 SHA `83467d5b676aec1c15f9dd517756ba58fca943e8128b91ab270554c277be1f5b`，本日志追加前 SHA `3388ad1f1a8373cc3d646d11eee4a2fddffe92ca3a87d7a811b1e86117d044c5`。
- 前后复核原项目文件、正式 runtime、RUN-004 和全局规则/登记；新增构建与测试只写本批准路径或系统临时目录。回滚核对哈希后仅移除本次新增便携文件/包，日志追加撤销；正式状态无需回滚且不得触碰。
- 保持 `RESEARCH_ONLY / decision_eligible=false / production_approved=false`；不新增回测、投资结论、交易或生产权限。完整跨平台可用性只有实际目标主机验证后才能宣称。


## GOV-20260906-004-LOCAL_DELIVERED_PENDING_PLATFORM_E2E｜便携包本地交付，保留异机验收缺口

- 记录日期：2026-09-07，Asia/Shanghai；同范围用户继续指令承接本决策批准，不复用旧决策授权。
- 已交付 `sw_industry_valuation_dashboard/portable-dist/2.3.0-portable.1/` 三份 ZIP，分别面向 Mac arm64、Mac x86_64、Windows x86_64。各包内置 Python 3.14.6、20 个锁定依赖、原字节 app、完整 RUN-004 与 20260904 隔离种子。
- `execution_status=PACKAGED_MAC_ARM64_VALIDATED_WITH_FOREIGN_PLATFORM_GAPS`；`artifact_publish_state=LOCAL_RESEARCH_PORTABLE_ZIPS`；`live_validation_state=MAC_ARM64_AUTHENTICATED_HTTPS_CALENDAR_PROBE_PASS`；`decision_eligible=false / production_approved=false`。
- 新增便携层 11 个源码/测试/指南文件；49 项便携层测试通过、0 失败/跳过。Mac 最终 ZIP 新路径原生验收通过：516 文件断网重建逐字节一致，目录 31/134/346、行业分片、实际启动器、首次原子导入、HTTP 安全、并发端口与正常停止通过。
- 真 token 只读本机 SDK 并仅在内存完成单交易日 HTTPS 诊断，6778 个包内文件零命中；另一独立假 token 安全验收 8070 文件及 HTTP/日志/真实 PID argv 零命中。没有从便携包发起完整真实更新作业，没有修改正式数据或启动持续服务。
- 两种 Mac 各 90 个 Mach-O、Windows 109 个 PE64 静态组成检查通过；三包 ZIP 和源/依赖/seed 哈希绑定复核通过。但 `WINDOWS_E2E_UNVERIFIED / INTEL_MACOS_E2E_UNVERIFIED`，本轮浏览器交互未验。不得将此记录解释为全平台最终验收关闭。
- 收尾复核原项目 324 文件、正式 runtime 1163 文件字节不变，全局规则和项目登记不变，旧 latest/ledger/manifest 未改。新增文件与统一 diff 有记录；日志为追加式。回滚仅针对核对哈希后的本决策新增源码与版本目录，先保留用户后续便携 data，日志只追加撤销。
- 报告：`sw_industry_valuation_dashboard/docs/reviews/GOV-20260906-004.md`，SHA `02191966640c99b3d08e658fdcd3cae9dc15e2e396642808798245276bf497ca`。
- 闭合检查：`GOV-20260906-004-closure.json`，SHA `ff9ef209572cfbf9cb70a71a84afda9e5a62f976225708323f6f9b747296c8b6`；源码 diff SHA `12c14a149ab84c21d10ca70c7bdbb0521c9ff2a42dafb33e60263676a0e797c7`；本记录追加前日志 SHA `fcbc1991906fb4bdf059b2de706fbdfe6a81b7507346c2ae381ba359d0427a3b`。
- 本地打包和已执行的 Mac 范围到此交付。Windows/Intel 真机及相应浏览器/真实更新仍需目标主机证据后另追加验收记录；未追加 `GOV-20260906-004-FINAL_CLOSED`。不转为提交、外部发布、定时任务、交易或生产授权。


## GOV-20260909-001-DOCS_DELIVERED｜SWIVD 使用与工程交接文档

- 日期：2026-09-09，Asia/Shanghai。批准原文：用户要求“写两份文档：1. 写一份使用引导手册，方便人上手使用；2. 写一份工程交接文档，能让任何人或agent拿到后可以无障碍进行项目迭代和维护。开启对抗式审查，确保不要出错。”本记录只承接当前文档任务，不复用历史构建/真实取数批准。
- 分类与准确范围：`LOCAL_CHANGE / DOCUMENTATION_ONLY`。新增 `sw_industry_valuation_dashboard/docs/handbook/使用引导手册.md`、`工程交接文档.md`；本次检查证据和统一 diff 为 `sw_industry_valuation_dashboard/docs/reviews/GOV-20260909-001-docs.json`、`GOV-20260909-001-docs.patch`；本日志仅追加。本次不改现有 README、合同、规格、代码、依赖、便携启动器、已交付 ZIP、原始数据、manifest、pointer、ledger 或项目登记。
- 已知事实与文档缺口：项目为非 Git 独立 SWIVD canonical root，HEAD 不适用；原有使用、构建和验收说明分散。便携 ZIP 包含冻结 app 与 seed，但不含完整 portable 构建工程和上级治理材料；新手册不能直接加入旧运行包精确库存。两份手册据现行调用链、实际 ZIP 和哈希绑定证据编写，明确维护材料、seed/source 绑定、入口副作用、平台验收限制、失败处理和逐文件回滚，不新增治理规则或业务语义。
- 修改前边界：维护项目 378 个既有文件、正式 runtime 1163 个文件；上级 AGENTS SHA `3e19909c0b7134391571bc0ab56f5115bea65756fac4dac04773dd4b131da50a`，登记 SHA `83467d5b676aec1c15f9dd517756ba58fca943e8128b91ab270554c277be1f5b`；本日志追加前 203753 bytes，SHA `838c2278c1922c87ab354ed11714afd98c3d47ab296c2c06b20c68ec8b401c43`。两份目标文件此前均不存在。
- 文档 after SHA：使用手册 `0d9824ee4339ad394343f45c112076f3980ab9c4c5fc88b11911bdc8c0dbcb4d`；工程交接 `417c6f2befe026e83f8f5913c198abd9b9268c1ff8edd673efbfdc5258c6bcfa`。已核对 24 个本地链接、8 个 shell 块语法、5 个 CLI 帮助入口及凭据/危险命令字面量；三份 ZIP、package manifest、每包 49 个冻结 source 文件与维护源码相符，12 份原验收证据 SHA 相符。
- 对抗审查：独立只读审查发现 PE-PB 象限坐标被误写为历史百分位，已按实际 PE/PB 坐标、历史百分位颜色和过滤后中位数修正；补入实际启动错误码 `PACKAGE_HASH_MISMATCH`。主审补全命令前序失败停止条件，并与独立复核共同确认便携 serve 先恢复 state、verify/doctor 只读拒绝待恢复事务的差异。已发现问题关闭，未声称绝对无错。
- 验证范围：本次只进行文档/源码/ZIP 读取、CLI `--help`、结构与哈希检查；未启动应用服务、未读取实际 Token、未运行数据作业、未重跑核心/便携/浏览器/异机 E2E。Windows/Intel 验收缺口与研究状态不变；另一台 Mac 的用户解压/启动反馈不升级为完整 E2E。
- 回读与回滚：既有项目文件、正式 runtime、三份原包及桌面总包字节未变；最终证据记录完整 before 清单、差异与追加式日志前缀复核。回滚仅在核对 after SHA 和后续用户改动后移走本次两份新手册；审查证据可保留，日志若需撤销只追加。无需也不得回滚原程序或正式状态。
- 状态：`execution_status=DOCUMENTATION_COMPLETE`，`artifact_publish_state=LOCAL_MARKDOWN_NOT_EMBEDDED_IN_OLD_PACKAGES`，`live_validation_state=NOT_RUN_DOCUMENTATION_ONLY`；项目仍为 `RESEARCH_ONLY / decision_eligible=false / production_approved=false`。没有可交付的新回测结论。本次文档工作到此交付，不授权后续实现、真实更新、重打包、提交、外部发布或生产。


