# 集成接口（实施固定契约）

本文件将已批准产品方案落实为模块边界。主项目 `src/industry_workbench`；旧 `src/swivd` 保持兼容。日期一律 YYYYMMDD；时间带 UTC offset；JSON 金融小数为十进制定点字符串（不允许 NaN/Inf），资金金额 `flow_cent` 为0.01万元整数的字符串。

## 模块边界

| 模块 | 单一职责 | 允许依赖 | 禁止承担 |
|---|---|---|---|
| models / taxonomy / metrics / periods | 身份、成员时点、金融定义与自然周期纯计算 | 标准库和已审核的旧身份规则 | 网络、磁盘、系统时钟、页面状态 |
| transport / provider | HTTPS白名单接口、限流、完备性、规范化输入和原始证据 | 客户端、分类规则、注入clock/put_raw | 行业估值或资金合计、发布current |
| storage | 不可变对象、哈希校验、单写锁和可恢复发布 | 标准库 | Tushare、财务计算、HTTP |
| history_state | 历史日期失败/恢复证据、索引与记录链校验、可重试判定 | store、源码身份、受控错误契约 | 财务计算、网络、页面状态、发布成功日 |
| validation | 独立有理数重算、数量和状态检查 | 标准库 | 复用被测聚合函数、网络或写入 |
| query | 固定批次查询、周期投影和历史统计 | 领域层、store、排序键 | 取数、写请求处理、DOM |
| jobs / scheduler | 数据流程编排、检查点、优先队列、应用内定时 | provider、领域、store、query、validation | HTTP协议和页面布局 |
| exports | 同批次CSV/Excel、精确排序、回读后原子生成 | query、排序、文件格式依赖 | 独立Tushare取数、重定义统计 |
| runtime | Python/平台/依赖锁的只读检查和环境证据 | 标准库、旧锁解析器 | 凭据、安装依赖、任何写入 |
| server / cli | 本地传输边界和显式命令 | 应用服务 | 自己实现财务公式、隐式启动旧任务 |
| web | 导航、筛选、精确显示和三态排序 | 本地只读API及显式作业API | 中位数、百分位、资金聚合 |
| legacy | 可选旧快照的严格只读兼容 | 原验证器和读取器 | 降低旧验证标准、旧日更入口 |

`src/swivd` 的旧计算、身份、验证与HTTPS凭据适配代码保持原基线字节；新增流水线只通过明确适配器使用它们。旧页面另存 `web/legacy`，新页面和新数据 schema 独立。以上是代码依赖职责；实际数据流为 provider → 纯计算及独立复核 → store 发布 → query → HTTP → web。

## Provider -> domain

`TushareProvider(client=None, *, put_raw=None, clock=None, progress=None)`。
`calendar(start_date,end_date) -> list[dict]` 返回 trade_cal 字段。
`fetch_day(trade_date) -> dict` 返回以下 DayInputs；不得计算行业统计。`put_raw(metadata,raw_bytes) -> dict` 由store注入，metadata不含Token；返回不可变引用。原始/规范化输出含Decimal时主程序序列化为精确字符串。

DayInputs = {
 trade_date, captured_at, provider_kind: LIVE_SECURE_TUSHARE|TEST_INJECTED_CLIENT,
 industries: [{uid,taxonomy:SW|THS|TDX|CI,version,level,code,name,parent_uid:null|string,is_pub:bool,market_code:null|string,membership_code:null|string}],
 memberships: [{uid,ts_code,state:ACTIVE|MEMBERSHIP_BOUNDARY_UNKNOWN|MEMBERSHIP_OVERLAP_UNKNOWN|HISTORY_UNAVAILABLE,evidence_kind:OFFICIAL_DATED|OBSERVED_SAME_DAY|UNKNOWN,in_date,out_date}],
 unknown_taxonomies: [SW|THS|TDX|CI],
 stocks: [{ts_code,name,list_date,delist_date,market,...}],
 daily: [Tushare daily rows], daily_basic: [Tushare daily_basic rows], moneyflow: [Tushare moneyflow rows],
 official: [{uid,trade_date,close,pe,pb}],
 audit: {base_complete:bool,eligible_count,traded_count,...},
 source_refs: [put_raw返回的引用]
}

同日一股可以有同花顺多归属；互斥分类多归属需UNKNOWN。原始接口失败、截断或全市场资金/行情缺口抛出安全错误，不伪装成DayInputs成功。THS历史没有同日成员响应时只返回该体系HISTORY_UNAVAILABLE，不取今天成员倒灌。SW/CI日期边界继承估值UNKNOWN。股票范围沪深A；完整性按生命周期、全天停牌、真实成交及资金一致性验证。资金与估值有效样本独立。

## Domain -> snapshot

`compute_day(inputs) -> DayResult`，DayResult={trade_date,captured_at,provider_kind,industries:[IndustryRow],members:[MemberRow],audit,source_refs}。IndustryRow identity同industries，加 metrics、counts、membership_evidence_kind、status。
Metric={value:null|string,status:OK|SMALL_SAMPLE|NA,reason_codes:[],metric_date,valid_count?,expected_count?,received_count?}。
metrics keys: pe_ttm_median,pb_median,official_pe,official_pb,close,flow_cent,net_mf_vol,pe_percentile,pb_percentile,official_pe_percentile,official_pb_percentile。
另有return_5d/return_mtd/return_ytd，单位为百分数（1代表1%，前端不得再乘100）。百分位附first_valid_date/last_valid_date/expected_days/missing_days/equal_count，包含当日的有效日样本。
counts: member_count,basic_received,pe_valid,pb_valid,flow_expected,flow_received。member_count指期末已确认成员数，并非存在未知归属时的真实总成员数；flow_expected/flow_received是期末应有/已取资金记录数，并非周/月期间所有日记录数。UI/导出名称必须明确这些范围。
MemberRow={uid,ts_code,name,membership_state,evidence_kind,pe_ttm,pb,flow_cent,net_mf_vol,has_trade,is_endpoint_member}。

## HTTP -> UI

GET /api/v2/bootstrap -> {nonce,version,default_taxonomy:'SW',default_level:'L1'}
GET /api/v2/current -> {batch_id:null|string,as_of:null|string,created_at,publication_state,history:{start,end,completed_days,missing_days},history_scan:null|HistoryScan,history_scan_error:null|{code,message},update_blocked:null|{code,message},development:boolean,scheduler:{enabled,times?,timezone?,last_error?,history_error?,history_gaps?,pending_update?,pending_history?},job:null|Job,legacy:{state,...}}
GET /api/v2/batches/{batch}/catalog -> {batch_id,as_of,taxonomies:[{id,name,levels:[{id,name}]}],trade_dates:[],history,publication_state}
GET /api/v2/batches/{batch}/industries?taxonomy=SW&level_or_series=L1&period_kind=day&period_key=YYYYMMDD -> {batch_id,as_of,period:{kind,key,start,end,as_of,endpoint,expected_days,available_days,status},rows:[IndustryRow]}
`week` period_key 接受 YYYY-MM-DD 或 YYYYMMDD 该周任意日，响应规范化为周一YYYYMMDD；`month` key为YYYY-MM。
GET /api/v2/batches/{batch}/industries/{urlencoded_uid}/history?period_kind=day|week|month -> {batch_id,uid,rows:[{trade_date,period?,metrics,...}]}
GET /api/v2/batches/{batch}/industries/{uid}/members?period_kind=...&period_key=... -> {batch_id,uid,period,rows:[MemberRow]}; 期间flow累计按每日当时归属，保留退出成员；估值/名称为期末记录，非期末成员is_endpoint_member=false。
GET /api/v2/jobs/active 与 /api/v2/jobs/{job_id} -> Job或{job:null}。
GET /api/v2/batches/{batch}/quality?period_kind=&period_key= -> {batch_id,period,days:[{trade_date,captured_at,audit,source_ref}],missing_dates}；audit.classification_coverage按taxonomy/level分组，含unknown_stocks与资金诊断。所有行业/成员响应附name_sort_key（固定pypinyin版本的中文排序键）。catalog另含industries身份全集用于父行业筛选。
POST /api/v2/jobs/update body={} -> Job
POST /api/v2/jobs/backfill body={start_date,end_date,retry_failed?:boolean} -> Job；retry_failed默认false，仅接受JSON布尔值，不接受字符串/数字。未知参数拒绝。
POST /api/v2/jobs/export body={batch_id,taxonomy,level_or_series,period_kind,period_key,format:'csv'|'xlsx',query:'',sort_key:null|string,sort_direction:'default'|'asc'|'desc'} -> Job，完成 result.download_url 可GET。
CSV另外传page/view/valuation_basis/parent_uid以精确保持当前视图；XLSX明确导出同一批次、周期的全部四分类、口径说明、审计6个Sheet，不受当前筛选影响。
导出完整性诊断列按周期解释：日频资金已取/应有记录数来自counts.flow_received/flow_expected，缺日保持NA；周/月有效/应有交易日数来自期间指标的received_count/expected_count。不能把日频股票记录数标成交易日数。
POST头：Content-Type: application/json，X-Workbench-Nonce: bootstrap.nonce。
Job={job_id,kind,status:QUEUED|RUNNING|SUCCEEDED|FAILED,phase,completed_units,total_units,message,result:null|dict,error:null|{code,message}}。
错误响应={error:{code,message}}；UI一律textContent安全显示。GET不联网、不创建作业。

`history`来自固定已发布批次，`history_scan`是独立的当前回补操作状态；不得混成同一个完整性承诺。HistoryScan={blocked_dates:[{trade_date,code,reason,evidence_version,evidence_ref}],pending_dates:[YYYYMMDD],remaining_days,remaining_attemptable_days,history_complete,evidence_validation}。历史索引损坏时保持已验证批次可读，history_scan=null并明确history_scan_error/update_blocked；UI展示“未知”，不得回退旧成功状态或提交回补。

## 历史日期隔离与重试

每个合格交易日独立原子发布。仅早于最新目标日、来自受控models.DataError或taxonomy.DataError的`INDEPENDENT_UNIVERSE_INCOMPLETE`可以按日留证后继续。最新日和网络、权限、schema、来源、日期、截断、哈希、存储等其他错误仍停止作业；已成功提交的子批次保留。不得把失败日构造为DayInputs成功或写入manifest.days。

失败证据包括该日daily/daily_basic/moneyflow/suspend_d、L/D/P stock_basic响应、捕获源码目录与冻结tar。history_state以不可变history-attempt-v1记录串联全局previous_attempt和同日supersedes，再原子更新history/index.json。校验重建整链与日期索引精确相等，并核对已发布coverage.blocked_dates证据锚；缺失、回退或损坏不能让失败日期静默消失。

evidence_version绑定相关数据代码与政策；仅UI变化不触发自动重试。显式retry_failed每作业每日期最多一次，内部retry_id和已尝试集合跨块保持。max_days约束尝试数，包含失败；HTTP不接受调用方指定retry_id。CLI显式`--retry-failed`默认关闭。

首次回填先采最新目标日时，history_start不得晚于该日；发布拒绝history_start>as_of，盘前/休市日的最新有效交易日应可直接查询。

Job.result含累计attempted_days/captured_days/published_batches，以及blocked_dates/pending_dates/remaining_days/remaining_attemptable_days/scan_complete等。remaining_days包括失败日和未尝试日；只有remaining_attemptable_days驱动续跑。全部可尝试日期扫描完仍有失败时为FAILED/HISTORY_INCOMPLETE、phase=COMPLETE_WITH_GAPS；有界块仍有未尝试日时phase=CHUNK_COMPLETE或CHUNK_COMPLETE_WITH_GAPS。扫描完成不代表历史完整，历史日已入库也不代表各行业指标无缺口。

只读Pipeline.history_status(current=None)核对索引、记录链、源身份与冻结源码，返回evidence_validation=RECORDS_ONLY，避免页面轮询重复解压所有底表。写任务决定跳过日期及发布前使用FULL_CLOSURE校验全部原始证据。日期BLOCKED只表示当前证据未通过，不宣称永久不可恢复。

## UI计算边界

允许格式化、精确十进制比较、过滤、排序、绘图；不在JS计算中位数、百分位或期间资金合计。全页固定batch_id，新批次显式统一切换。默认中文、GitHub dark风格，紧凑但可读；单一固定顶栏+三主tab+就近筛选；图表不得成为阻塞表格阅读的装饰。保留原估值六类视图和成分入口。legacy为旧结果只读归档，不替代新估值功能。

## 缺口小计与可用范围

行业 `metrics.flow_cent.known_subtotal` 与 `net_mf_vol.known_subtotal` 只累计已确认ACTIVE归属；完整值为NA时小计仍保留。周/月累计每天的小计，不把缺日或未知归属当0、不将小计替代完整值。没有任何已确认贡献的成员小计为null，真实已确认零为字符串"0"。

MemberRow另含 `known_subtotal`、`net_mf_vol_known_subtotal`；周期Query.members把每日小计相加，`contribution_days`只包括有已确认金额的日期。成员集合包括期间退出者；没有期末归属的股票不补期末估值。行业与成员小计应可独立对账。

`catalog.history.availability_by_scope` 按taxonomy+level分组，列出中位数PE/PB、完整资金、官方PE/PB及官方close的first_valid_date、last_valid_date、valid_industry_days、missing_industry_days。计数单位是行业×日，不是整套分类的完整天数；未采集日期单列在history.missing_dates。`days_with_metric_gaps`覆盖所有已入批日，后续只重算源码也不得把历史缺口隐藏为PUBLISHED。

查询周期截止点早于明确history_start时返回 `PERIOD_OUTSIDE_HISTORY_RANGE`，避免把不在已声明范围内的日期错误解释成整期休市。与history_start相交的第一周/月保留完整自然周期，未回补的前段仍为缺口。

## 官方行情补充

CI/THS按交易日分别调用ci_daily/ths_daily的全截面，只取ts_code、trade_date、close，正常增加2次请求。按当日输入目录中的精确代码匹配，未匹配行情不能扩充行业目录。成员未知不遮蔽独立官方close；这两个接口不提供本项目的行业官方PE/PB，相关估值保持NA。请求上限、重复、日期和数值异常不可作为完整成功。

## 运行环境闭包

`runtime.environment_identity(source_root)`返回CPython版本、macOS版本/架构和每个锁定依赖的expected/actual/matched，不记录个人目录、hostname、环境变量或Token。`require_supported_environment`用于正式Pipeline更新和回补，阻止CLI绕过平台/依赖门；发生在provider构造与writer锁之前。批次`source.runtime`记录真实运行环境。开发证据亦记录环境，但临时测试fixture不强制具有安装锁。历史取数source未曾记录的环境不补造。

## 请求审计与日历来源

每次实际返回的Tushare响应由store.put_raw写入内容对象，并在request_journal.ndjson追加无Token的请求元数据、attempt_count和raw引用；重复得到相同字节仍保留每次调用凭据。完整五年交易日历原始响应由manifest.calendar_source_refs绑定，不能只保存规范化日历。已是最新的返回不创建新批次，但其作业结果仍保留本次calendar_source_refs，可证明只检查日历。日志发生中断时保留异常尾部供诊断，不把日志行数冒充已发布日数；正式查询继续依据批次引用闭包。
