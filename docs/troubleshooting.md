# 故障排查

- `CONCURRENT_UPDATE`：已有 CLI 或网页作业持锁；等待其终态，不要删除锁文件。
- `PUBLICATION_RECOVERY_REQUIRED`：检测到未完成或无法验证的发布事务，pointer 读取已失败关闭。正常重启本地服务或再次运行 CLI 会先在单写者锁内恢复；若仍出现该码，保留 `transactions/publication.json`、pointer、ledger 和对应 job/run 取证，不要手工删改。
- `PUBLICATION_COMMITTED_RECOVERY_REQUIRED`：提交点已持久化但成功账本尚未能安全闭合；新 pointer 不得被重标为失败，也不得追加互斥终态。保留现场并在同一数据目录重启恢复。
- `PREVIOUS_OPEN_DAY_BEFORE_SW_DAILY_CUTOFF`：今天开市，但北京时间尚未到 18:30；系统在
  联网获取目标日行情前已把目标日选择为上一交易日，并在页面显示实际截止日。
- `CURRENT_OPEN_DAY_AT_OR_AFTER_SW_DAILY_CUTOFF`：今天开市且北京时间已到 18:30；系统选择
  今天作为目标日，但若官方数据仍不完整，仍会失败而不会回退。
- `LATEST_OPEN_DAY_NON_TRADING_DATE`：今天休市；系统选择交易日历声明的最近交易日。
- `ALREADY_UP_TO_DATE`：所选目标日已经是经验证 current；除运行前一次 `trade_cal` 选日探针
  外，不重抓行业/成员/个股业务数据，不创建 run，也不改写 latest。
- `CURRENT_INDUSTRY_ROW_MISSING`：已经选定的目标日仍缺少官方行业行，可能是数据延迟或
  分类—行情代码冲突；系统不会在请求失败后继续回退。v2.2 只允许
  `SW2021:L3:230501` 的 `850401.SI/850412.SI` 进入版本化身份证据门；其他代码仍失败。
  先核对失败 run 的 `sw_daily` 行数和缺失代码，不能仅凭同名行业自动改代码。
- `IDENTITY_CATALOG_*` / `IDENTITY_NAME_MISMATCH`：候选目录不唯一、候选代码被其他行业
  占用，或特钢三级的名称/父路径发生漂移；保持失败并核对最新分类，不按名称改码。
- `IDENTITY_TARGET_DAY_SINGLETON_REQUIRED` / `IDENTITY_HISTORY_OVERLAP` /
  `IDENTITY_HISTORY_GAP` / `IDENTITY_HISTORY_OSCILLATION`：目标日双码或缺码、历史重叠、开市日
  缺口，或候选代码切换超过一次；这些都不能拼成官方连续历史。
- `IDENTITY_MEMBER_*`：候选成员查询缺少双码、Y/N 或两轮，主 L1 分批与直接探针不一致，
  两码同时有当前成员，或当前成员代码与行情代码不同步；保持失败，不任意选一路成员。
- 其他 `IDENTITY_*`：查看失败报告中的安全码和
  `inputs/normalized/industry_identity_resolution.json`（若已生成），再核对身份专用 raw 的
  request hash。证据不完整是预期的失败关闭；不要人工改 raw、按中文名 join 或忽略缺失行。
- `EVIDENCE_GATED_ALIAS`：不是错误。当前特钢Ⅲ的目录代码为 `850401.SI`，行情与成员代码
  为 `850412.SI`；页面必须同时披露两者及规则版本，并说明上游内部原因未知。
- `EVIDENCE_GATED_TRANSITION`：不是自动改码。只有两个候选的官方 `sw_daily` 区段在相邻
  开市日无重叠、无缺口，且当前成员端同步，才允许生成；任一门失败则不发布。
- `DIRECT`：本次分类、当前行情和成员代码一致；仍不允许用父 run 的状态跳过本次核验。
- `TARGET_BEFORE_CURRENT`：所选目标日早于 current；系统拒绝倒退 current。
- `INJECTED_CLIENT_DATA_DIR_FORBIDDEN`：显式注入的测试 client 试图写入 Python 系统临时目录之外。不要通过改标记把 fake 当成 live；将测试改用 `TemporaryDirectory`。
- `PARENT_PROVIDER_KIND_MISMATCH`：父快照与新运行分别来自真实/注入测试 provider。该血缘不可混接，必须分开数据目录重建。
- manifest/audit/request 的 `provider_kind`、`transport`、`live_validation_state/reason` 不一致：保持失败关闭。`TEST_INJECTED_CLIENT` 即使离线验证通过也必须是 `NOT_LIVE_TEST_PROVIDER`，不是 live PASS。
- 页面进度看似停留但 worker 仍在运行：同一阶段中来自旧子批次的单位倒退回调会被丢弃；以阶段序和全局单调 `percent` 为准，不要手工改 job JSON。
- `ENDPOINT_ROW_LIMIT` / `MEMBER_ROW_LIMIT`：接口可能截断；无法继续拆分时失败关闭。
- `MEMBERSHIP_ROUNDS_DRIFT`：两轮成员规范化不一致，不产生快照。
- `MEMBERSHIP_BOUNDARY_UNKNOWN`：纳入/调出边界缺乏权威语义；候选可展示，但同组百分位不可计算。
- `CLASSIFICATION_CHANGED_REQUIRES_BOOTSTRAP`：分类与父快照不同，不能接续旧血缘。
- `POINTER_HASH_MISMATCH`：pointer 目标被移动或修改，停止展示并只读取证。
- `SNAPSHOT_ARTIFACT_DRIFT`：run 首次验证后，待服务的 catalog、行业分片或档案字节发生变化；服务拒绝返回该文件，不重新信任或静默刷新。
- manifest v3 身份证据路径或 SHA-256 不一致：停止展示该 run，不从旧 manifest、文件名或
  页面缓存推断身份；以验证器实际返回的安全码为准。
- `ARCHIVE_METADATA_INVALID/ARCHIVE_RESPONSE_IDENTITY_MISMATCH`：SW2014 档案身份元数据与已验证 RUN-004 响应不一致；保持分轴封存，不回退其他档案。
- `INVALID_HOST/ORIGIN/NONCE`：只通过 `127.0.0.1` 或 `localhost` 直接访问。
- `token_configured=false`：配置环境变量或 Tushare 本机 token，勿传入网页、argv 或项目配置。
- 页面无 current：尚无 v2 基线；旧 RUN-004 不会冒充 v2 latest。
- 页面白底、默认按钮、隐藏区同时展开：CSS/JavaScript 未加载，通常是直接打开了 `web/index.html`。关闭该 `file://` 页面，运行 `python3 run_dashboard.py serve`，只访问 `http://127.0.0.1:8765`。
- 页面显示“请通过本地服务打开仪表盘”：这是预期的失败关闭提示，不是数据损坏；启动本地服务后重新访问 loopback 地址。
- 深色壳可见但图表为空：先看页面顶部是否有已验证 v2 快照。无快照时不要把 RUN-004 当 current；有快照时检查 `/bootstrap.js`、`/app.js`、`/api/v1/snapshots/<run_id>/catalog` 是否均为 200。
- 特钢Ⅲ代码看似“变化”：先比较 catalog v3 与 shard v2 的 `industry_uid`、目录/行情/成员/
  当前解析代码、状态和规则版本。代码角色不同但状态为 `EVIDENCE_GATED_ALIAS` 是当前已知
  端点形态；字段缺失、互相矛盾或 UI 未披露则是产物或页面回归，不能解释为上游修复。
