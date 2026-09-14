# 申万行业估值全景仪表盘项目合同

- 合同版本：`swivd-contract-v1.2.0`
- 决策：`GOV-20260830-001`
- 数据语义 successor：`GOV-20260830-001-SW2014-SEMANTICS-AUTHORIZED`
- 名称历史 successor：`GOV-20260830-001-NAME-HISTORY-AUTHORIZED`
- 项目 ID：`sw-industry-valuation-dashboard`
- canonical root：`/Users/chenyipeng/Desktop/Codex/Stock/sw_industry_valuation_dashboard`
- 状态：`research_only`

## 1. 真实目标

本项目复现原压缩包的研究工作流与七类本地可视化能力，但不复刻其错误数据语义。
核心问题是：在明确的申万分类版本内，某个一级行业指数当前的 Tushare 原始 PE/PB
位于自身同版本历史的什么位置，并且最近 5 个交易日、月初至今、年初至今的官方指数
收盘价收益如何。

本项目不是选股、因子、回测、估值模型或投资建议。历史位置只描述经验分布，不证明
内在价值、未来收益或可交易性。

## 2. 唯一数据链

```text
Tushare trade_cal
  -> 交易日轴
Tushare index_classify(level=L1, src=SW2021/SW2014)
  -> 两套互不混合的行业身份白名单
Tushare sw_daily(ts_code, start_date, end_date)
  -> 官方行业指数 close/pe/pb 原始字段
  -> 同版本百分位与收盘价锚点收益
  -> 规范化 CSV + 审计 + manifest + 自包含 HTML
```

禁止使用 `daily_basic`、当前成分股或任意个股聚合补造行业估值。禁止使用
`index_dailybasic`，因为它不是申万行业估值接口。

## 3. 时间、版本与样本

- 当前轴：`SW2021`、一级行业、`20211213` 至显式 `as_of`。
- 历史档案轴：`SW2014`、一级行业、查询起点 `20140101` 至 `20211213` 前最后一个
  经 `trade_cal` 验证的开市日。
- 两个版本各自冻结分类和行情，不拼接、不做名称映射、不做跨版本排序。
- Tushare 对退役 SW2014 目录的 28 行 `is_pub` 均返回 `null`。原值必须保留，
  不得改写为 `1`。该轴的身份白名单是冻结响应中全部 28 个唯一 L1 行业，
  并显式记录
  `publication_state=NOT_PROVIDED_FOR_RETIRED_TAXONOMY` 和
  `selection_basis=ALL_CLASSIFIED_L1_ROWS_FOR_RETIRED_TAXONOMY`。
- SW2014 允许不同行业的首个官方观测日不同。每个行业必须从自身首观测日起，
  无内部交易日缺口地连续至共同结束日；首观测日前的结构性空白不是数据缺口。
- SW2014 时序身份只由冻结白名单内的 `ts_code` 确定；`sw_daily.name` 是可变
  上游标签，不是主键。同代码的历史改名允许且必须保真，但共同末日名称
  必须严格等于冻结分类名称。禁止按名称连接、合并、拆分、替换代码或跨版本映射。
- 历史表中 `industry_name` 是冻结分类的稳定展示名，`source_name` 是对应日
  `sw_daily.name` 的原样值；两者必须并存，不得互相冒充。审计必须按代码输出
  连续名称分段及各段首日、末日和行数。
- SW2021 仍要求 `is_pub` 严格为 `0|1`，只对 `is_pub=1` 的子集取行情，并从统一
  起点校验完整交易日矩形。
- SW2014 不显示为当前估值。若其身份或 PE/PB 覆盖不闭合，仅该轴 `BLOCKED`；
  SW2021 可以独立生成候选产物，但整体状态不得写成双版本完整。
- `as_of` 必须由调用者显式提供且必须是开市日；不静默退回最近日期。

## 4. 冻结公式

对每个版本、行业和字段分别计算。原始当前值保留；只有有限且严格大于零的 PE/PB
进入历史百分位：

```text
percentile_le = count(history_value <= current_value) / valid_count * 100
```

当前观测包含在历史样本。必须同时输出 `valid_count`、`tie_count`、`tie_ratio`、首末有效日。
当前值非正或非有限，或者 `valid_count < 252`，百分位为不可用。不得补零、前填、后填或
裁剪极端值。

展示阈值固定为：`[0,20)` 历史极低位、`[20,40)` 历史较低位、`[40,60)` 历史中位、
`[60,80)` 历史较高位、`[80,100]` 历史极高位。不得使用“低估/高估”。

收益仅由官方指数 `close` 计算：

- 5 日：`close(as_of) / close(t-5个交易日) - 1`；
- MTD：`close(as_of) / close(上月最后交易日) - 1`；
- YTD：`close(as_of) / close(上年最后交易日) - 1`。

缺少精确锚点时字段为空并标 `HISTORY_INSUFFICIENT`。不得连乘舍入后的 `pct_change`。

## 5. 状态与失败关闭

状态至少分开记录：

- `execution_status`：程序是否完成；
- `artifact_publish_state`：是否形成完整本地候选；
- `live_validation_state`：真实接口、日期与覆盖是否闭合；
- `sw2021_axis_state`、`sw2014_axis_state`：两个版本分别状态；
- `research_grade=RESEARCH_ONLY`；
- `decision_eligible=false`；
- `production_approved=false`。

以下情况不得发布对应轴：schema 漂移、分类数量或身份异常、重复主键、白名单外代码、
日期越界、接口触及行限、当前日已发布行业缺行、交易日不一致、token/权限错误、请求被
重定向或使用非 HTTPS。失败不得回退旧缓存，也不得更新 `latest`。

对 SW2014，`is_pub` 非空、未记录三态语义、任一行业首观测日后存在内部交易日
缺口、任一行业末日缺失，或 PE/PB 任一字段的有效样本少于 252，均必须该轴
`BLOCKED`。对 SW2021，`is_pub` 为空仍必须失败关闭。

对 SW2014，任意 `sw_daily.name` 为空、代码不在白名单、共同末日名称与冻结分类
不一致、历史 `source_name` 与冻结日线不一致，或审计遗漏/伪造名称分段，均必须
该轴 `BLOCKED`。同代码的非空历史名称变更本身不是失败条件。

## 6. 产物与运行隔离

正式运行只写 `output/runs/<run_id>/`。每个运行至少包含：

- `inputs/raw/`：原始接口响应；
- `inputs/normalized/`：分类、交易日、两版日线 CSV；
- `tables/`：当前 SW2021 一级行业、SW2021 历史、SW2014 历史档案 CSV；
- `dashboard.html`：无 CDN、无远程字体、无外链的自包含页面；
- `audit.json`、`manifest.json`、`SHA256SUMS`、`reports/adversarial_review.md`。

禁止 Pickle。测试只写系统临时目录。任何 `--test` 或 fixture 不得写正式运行目录或
正式 ledger。成功与失败运行都向 `output/run_ledger.ndjson` 追加一条摘要；只有全部必需门
通过的运行才可更新内容寻址的 `output/latest_run.json`。

归一化分类 CSV 在 Tushare 原字段之外必须增加 `publication_state` 与
`selection_basis`。SW2014 历史表的 `is_pub` 必须保持空值；页面只能展示空值与三态
说明，不得派生“已发布”断言。

SW2014 历史 CSV 还必须包含 `source_name`，并逐主键等于冻结的
`inputs/normalized/sw_daily_sw2014.csv.name`。`industry_name` 仅作为同代码的稳定展示名。

## 7. 页面范围

SW2021 保留六类能力：总览、估值热力图、历史走势、涨跌排行、PE-PB 象限、数据明细；
另设 SW2014 历史档案页。历史档案页必须说明退役分类的 `is_pub` 未提供且按全部
28 个 L1 身份纳入，不得说成当前发布状态。首版只做一级行业，不展示成分股数量，
不自动打开浏览器。

历史档案页还必须明示 `ts_code` 是身份、`name` 是可变标签，并披露所有实际
改名代码的原名、新名、起止日和行数。

## 8. 权限边界

禁止自动化、通知、回测、选股、评分、交易、外部发布、Claude Stock 或资金流项目集成。
任何产物固定为 `research_only`、`decision_eligible=false`、`production_approved=false`。
