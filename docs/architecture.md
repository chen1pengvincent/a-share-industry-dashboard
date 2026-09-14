# 架构与调用链

系统只解决三件事：取得官方行业指数估值、恢复目标日成员候选、描述个股在同级行业中的横截面位置。三者的事实源、缺失状态与失败规则必须分离。

```text
浏览器壳 web/（GitHub-dark 六类全景 + 成分股 + SW2014 档案）
  │ GET 已验证快照；POST 作业（Host/Origin/nonce）
  ▼
v2_server ── v2_jobs ── 单写者锁
                    │
                    ▼
v2_pipeline ── v2_provider ── tushare_client ── HTTPS Tushare
     │              │
     │              └─ 冻结原始响应与请求审计（无 token）
     ├─ v2_identity：稳定 industry_uid、端点代码证据门、官方行情区段
     ├─ v2_domain：分类、点时成员、个股百分位
     ├─ core：官方行业历史百分位
     ├─ v2_storage：不可变 run、可恢复发布事务、pointer、ledger、哈希
     ├─ v2_lineage：扁平祖先证据复制与原manifest归属校验
     ├─ runtime_environment：无凭据、无机器身份的依赖与时区诊断
     └─ v2_validator：独立重算与失败关闭
```

浏览器固定使用打开时的 `run_id`。后台成功只产生“新数据已就绪”，用户点击后才替换页面快照，因此 worker 阻塞或失败不影响旧页面浏览。

`ui/catalog.json` 只复制行业汇总表中已经计算的展示摘要，支持按层级渲染总览、热力、排行、散点和明细；公式仍只在 Python 领域层计算。行业历史与成分股保留为单行业分片，避免一次读取 511 个分片。SW2014 通过固定白名单的只读 CSV 接口访问封存 RUN-004，不执行档案 HTML。

服务首次识别 run 时由独立验证器取得 manifest 的完整 artifact 哈希表；此后每个 catalog、行业分片和档案 CSV 在解析前仍按该冻结字节数与 SHA-256 复核，避免“验证一次后继续服务已漂移磁盘文件”的时间差。旧 `swivd-ui-catalog-v1` 只在内存中用同一快照内已验证的分类和行业分片投影为 v2 HTTP 响应，不写回 run，也不改变数据公式。

六层职责依次为：领域计算、唯一 Tushare provider、不可变快照存储、更新作业、本地服务、无远程依赖的 UI。先创建 run，再构建、哈希并独立验证；只有全部通过才进入 `PREPARED → pointer replace → COMMITTED → unique terminal` 发布事务。`COMMITTED` 是唯一提交点；其前崩溃回滚 pointer 前像，其后崩溃保留 pointer 并补齐成功账本。未恢复事务会阻断 pointer 读取，JobManager 与 CLI 只在取得同一单写者锁后恢复。

正常 CLI/网页链路是 `LIVE_SECURE_TUSHARE`，request transport 只能记为
`HTTPS_NO_REDIRECT`。显式 client 注入则整个 run 标记为
`TEST_INJECTED_CLIENT/INJECTED_TEST_CLIENT`，只能写 Python 系统临时目录的严格子目录，
且 `live_validation_state=NOT_LIVE_TEST_PROVIDER`。provider 种类必须贯穿 request、
audit、manifest 和父子血缘，两种 provider 不得混接。

作业进度以阶段序为第一单调轴：过期阶段回调丢弃；同一阶段内
`completed_units/total_units` 均不得倒退；进入后续阶段时它们按新阶段重置；
全局 `percent` 始终不减，终态忽略迟到回调。

## 行业身份层

稳定主键是 `industry_uid=SW2021:{level}:{industry_code}`。原始分类目录代码、行情代码、
成员代码和当前解析代码是四个可审计角色，不能再用一个 `index_code` 隐式兼任；规范化行情
与成员行另以 `source_ts_code/source_l3_code` 保留端点原值。普通
行业走 `DIRECT`；唯一受控规则 `swivd-special-steel-identity-v1` 只处理
`SW2021:L3:230501` 和候选 `850401.SI/850412.SI`。

当前证据形态是目录 `850401.SI`、行情/成员 `850412.SI`，因此投影状态为
`EVIDENCE_GATED_ALIAS`。未来若三个端点直接一致则为 `DIRECT`；若官方 `sw_daily` 在相邻
开市日无重叠、无缺口地切换代码，且成员端同步，才允许
`EVIDENCE_GATED_TRANSITION`。任一双码、缺口、第二别名、碰撞、名称或父路径漂移、成员不同步
都会在派生数据前失败。

身份计算只连接本次冻结 raw，不改写原始响应。规范化结果在
`inputs/normalized/industry_identity_resolution.json`；身份专用行情与两轮 Y/N 成员请求分别
位于 `inputs/raw/identity_resolution/sw_daily/` 和
`inputs/raw/identity_resolution/index_member_all/round_{1,2}/{Y,N}/`。manifest v3 绑定证据
相对路径及 SHA-256，audit v2 再绑定安全摘要和 raw request 哈希；父 run 的解析结论不能替代
本次重新验证。

新产物版本为 manifest v4、audit v2、catalog v3、行业 shard v2。旧 manifest v2/v3、audit
v1、catalog v1/v2 和 shard v1 只读兼容，服务不得为旧快照补造新身份事实或原地升级。
新 catalog/shard 同时显式保存 `current_index_code` 和 `identity_reason_disclosure`；
非 `DIRECT` 的原因披露固定为 `UNKNOWN_UPSTREAM_INTERNAL_CAUSE`，不得从名称或代码差异猜测上游内部原因。

## v2.3 运行边界

`lineage/<ancestor_run_id>/manifest.json` 与同层 `inputs/raw/` 保留每个祖先的原始字节，
不再生成 `lineage/A/lineage/B`。祖先链按 parent manifest 哈希逐层核对，每份raw还必须在
其所属祖先自身的 artifacts 中出现；孤儿证据、循环和同路径冲突均拒绝。扁平化解决路径
深度问题，不消除保存多个完整自包含快照的累计磁盘成本。

首次快照验证采用进程内单次并发合并；该阶段仍做完整离线重放，不能声称首屏已变成轻量
校验。浏览时继续核对manifest身份及实际响应字节哈希。JobManager遇其他写者时允许只读
HTTP就绪，但不恢复或重标该写者的job；pending发布事务继续阻断pointer读取。

只读API `/api/v1/jobs/active` 恢复安全进度，`/api/v1/snapshots/current` 返回已解引用的
current定位。页面不自动重发POST；暂时断线与任务失败分别展示。首次验证、图表渲染、
job轮询和快照切换是不同状态，不用一条loading状态相互覆盖。
