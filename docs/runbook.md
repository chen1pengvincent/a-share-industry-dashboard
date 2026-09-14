# 运行、迁移与验收手册

先按 [README 安装步骤](../README.md#快速开始)创建 `.venv` 并安装 `requirements.lock`。以下命令使用 macOS 解释器路径，Windows 将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。不要混用全局 Python 与虚拟环境的 pip。

1. `.venv/bin/python -B run_dashboard.py doctor`，确认 Python、冻结依赖、上海时区、token、端口与数据目录状态。
2. `./start_macos.command` 或 Windows 的 `start_windows.cmd`，再手动用 Chrome 访问 `http://127.0.0.1:8765`。不要双击、预览或收藏 `web/index.html` 的 `file://` 地址；它只是源码。
3. 页面更新成功后继续保留旧快照，直到用户点击“切换到新快照”。

每次更新都会重新验证行业身份。当前特钢三级行业应显示稳定身份
`SW2021:L3:230501`、目录代码 `850401.SI`、行情/成员/当前解析代码 `850412.SI` 和状态
`EVIDENCE_GATED_ALIAS`；这是当前端点证据，不是永久保证，也不代表已知上游内部原因。

CLI 日更为 `.venv/bin/python -B run_dashboard.py update`；历史快照为 `.venv/bin/python -B run_dashboard.py materialize-date --as-of YYYYMMDD`。两者共用非阻塞单写者锁。

服务或 CLI 启动时会在单写者锁内检查 `transactions/publication.json`。若上次进程在 `COMMITTED` 前退出，系统恢复旧 pointer 并将对应作业记为 `INTERRUPTED`；若已持久化 `COMMITTED`，系统保留新 pointer、幂等补齐唯一成功账本并把对应作业恢复为 `SUCCEEDED`。不要删除或手工编辑事务文件、pointer、ledger 或 job JSON；未完成恢复时页面会拒绝读取 current。

日更目标日按 `Asia/Shanghai` 固定判断：SSE 开市日 18:30 前使用交易日历中的
`pretrade_date`，18:30 起使用当天；休市日使用当天日历行的 `pretrade_date`。页面和 CLI
必须显示实际目标日及原因。该规则发生在目标日联网取数之前；目标确定后，缺数或身份冲突
仍失败关闭，不会再次回退。若目标日已是经验证 current，则返回 `ALREADY_UP_TO_DATE`，
不创建 run、不重抓行业/成员/个股业务数据、不改写 pointer；运行前仍只执行一次
`trade_cal` 选日探针。

身份验证发生在派生行业估值和成员百分位之前。只有本次 raw 同时证明分类路径、候选代码、
目标日唯一行情、官方历史连续区段和两轮 Y/N 成员稳定，才会继续构建。未来三个端点直接一致
时状态回到 `DIRECT`；出现官方代码无缝切换时，只有相邻开市日无重叠、无缺口且成员同步才
进入 `EVIDENCE_GATED_TRANSITION`。不允许人工编辑失败 run 后重试发布。

```bash
.venv/bin/python -B run_dashboard.py validate-spec --spec PROJECT_SPEC_V4.json
.venv/bin/python -B run_dashboard.py validate-run --run-dir /path/to/runs/SWIVD2-RUN-YYYYMMDD-NNN
.venv/bin/python -B run_dashboard.py rebuild-v2 --run-dir /path/to/run --output-dir /tmp/rebuild
```

验收新 run 时还要核对：

1. `inputs/normalized/industry_identity_resolution.json` 的 schema、规则版本、目标日和各代码角色；
2. manifest v3 的 `identity_resolution.evidence_path/evidence_sha256` 与文件字节一致；
3. audit v2 的安全摘要和身份专用 raw request 哈希都在文件闭包内；
4. catalog v3 与对应 shard v2 的身份披露逐字段一致，包括 `current_index_code` 和 `identity_reason_disclosure`；
5. manifest、audit 和每条 request audit 的 `provider_kind` 一致；只有 `LIVE_SECURE_TUSHARE/HTTPS_NO_REDIRECT` 可为 live `PASS`，`TEST_INJECTED_CLIENT/INJECTED_TEST_CLIENT` 必须为 `NOT_LIVE_TEST_PROVIDER`；
6. raw 分类、行情和成员响应仍保留供应商原值，且文件中没有 token。

迁移按以下顺序进行：

1. 在源电脑停止服务和更新作业，确认没有未恢复事务后再复制，避免复制出一半的新快照。
2. 复制项目源码、`web/`、合同、规格、锁文件、启动脚本和 `output/runs/SWIVD-RUN-20260828-004/`。该封存档案是新快照构建输入，保持相对位置和原字节；不要复制 `.venv`、缓存或本机 token 配置。只迁移现有 v2 快照用于浏览时，其内嵌 SW2014 档案已自包含。
3. 如需历史数据，完整复制数据目录到新位置，保持 `runs/`、pointer、ledger、jobs 和事务记录的相对布局，不合并两个已有数据目录，不编辑 run ID、manifest 或哈希。
4. 在目标电脑使用 CPython 3.11–3.14 创建新的 `.venv`，执行 `pip install --require-hashes -r requirements.lock` 和 `pip check`。锁文件同时冻结传递依赖及平台安装包哈希，包含直接时区依赖 `tzdata`，无需依赖 Windows 系统提供 IANA 时区库。
5. 对数据目录中每个 v2 run 执行 `validate-run --run-dir`。检查 current 和历史 pointer 的目标路径、run ID、日期、purpose 与 manifest SHA-256，确认验证结果来自复制后的目标目录；失败时停止使用该副本，返回源目录核对并重新复制，不手工修补历史记录。
6. 执行 `doctor --data-dir "目标数据目录"`。`dependencies_locked=false` 时按锁重新安装；`timezone_available=false` 时修复当前虚拟环境中的 `tzdata`；`legacy_archive_available=false` 时核对封存源档案。最后使用同一个 `--data-dir` 启动服务，手动打开 Chrome。token 在目标电脑另行配置，不随数据迁移。

doctor 对 legacy 的布尔检查只确认新快照必需的固定 manifest 与八项源文件闭包可用，不把复制后的 v1 档案重新封存或晋级。没有源档案时，已有有效 v2 快照仍可只读浏览，但不能据此宣称新快照构建环境完整。

启动脚本和锁文件的跨平台检查不等于真实 Windows 主机验收；完成 Windows 安装、启动和页面交互前始终保留 `WINDOWS_E2E_UNVERIFIED`。

run 不原地修改或删除。若需回滚 current，必须在新的治理批准下写 successor pointer 并追加 ledger，不能改写历史 manifest。身份规则回滚必须同时恢复合同、规格、源码与验证器；不能只删解析字段后继续使用已经发布的新快照。旧 schema 快照保持只读，不重封为 manifest v3。

视觉验收要在 HTTP 页面完成：确认深色背景、顶部标签、摘要卡和图表均已出现，浏览器网络面板中的页面资源只来自当前 `127.0.0.1/localhost`。若只看到白底默认按钮、所有隐藏区同时展开或行业数据为空，先按故障排查检查打开方式和静态资源，不要触发数据更新来掩盖前端故障。
