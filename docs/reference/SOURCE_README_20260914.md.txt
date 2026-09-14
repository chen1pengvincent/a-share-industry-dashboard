# 申万行业估值全景仪表盘 v2.3

这是一个只在本机浏览器运行的研究工具。当前轴为 SW2021 L1/L2/L3；行业 PE/PB 只来自 Tushare `sw_daily`，成分关系来自 `index_member_all`，个股估值来自目标日 `daily_basic`。三条数据链互不替代。

项目始终是 `RESEARCH_ONLY`，`decision_eligible=false`，`production_approved=false`。页面中的百分位是统计位置，不是低估、高估、评分或交易建议。

v2.2 将行业业务身份与 Tushare 各端点代码分开：稳定主键为
`SW2021:{level}:{industry_code}`，分类目录、`sw_daily` 和 `index_member_all` 的代码分别
保留。当前唯一获准的端点别名是 `SW2021:L3:230501`（特钢Ⅲ）的
`850401.SI/850412.SI`；每次运行都重新核验，不按中文名称自动匹配。页面会披露实际使用
的目录、行情、成员代码和解析状态；项目不声称已知上游内部原因。

## 快速开始

要求 CPython 3.11–3.14。在项目目录内为新电脑创建独立 `.venv`，按完整锁文件安装依赖；不要复制另一台电脑的虚拟环境，也不要只安装两个顶层包。

macOS（示例选择 3.14，也可换成已安装的 3.11、3.12 或 3.13）：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -B run_dashboard.py doctor
./start_macos.command
```

Windows 命令提示符：

```bat
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -B run_dashboard.py doctor
start_windows.cmd
```

两个启动脚本优先使用项目 `.venv`；不存在时再查找受支持的解释器。已有 `.venv` 不可用时会明确报错，需按上述步骤在新位置重建。脚本只启动服务，不自动安装依赖。直接启动命令为 `.venv/bin/python -B run_dashboard.py serve`，Windows 将解释器换成 `.venv\Scripts\python.exe`。

doctor 的 `dependencies_locked` 检查所有冻结包版本，`timezone_available` 检查 `Asia/Shanghai` 可用性，`legacy_archive_available` 检查新快照所需的 SW2014 源档案。逐包检查只输出布尔结果，不显示版本原文、本机路径或凭据。Token 只允许来自环境变量 `TUSHARE_TOKEN`，其次为 Tushare 本机配置；网页不接收 token。新电脑需单独配置，`token_configured=false` 时仍可浏览已有有效快照。

服务只监听 `127.0.0.1:8765`，不会自动打开浏览器。手动访问 `http://127.0.0.1:8765`。`web/index.html` 是源码，不是可直接双击或预览的交付页面；若误用 `file://` 打开，页面只显示启动说明，不连接 API。首次没有 v2 快照时，可在页面点击更新，或执行：

```bash
.venv/bin/python -B run_dashboard.py update
.venv/bin/python -B run_dashboard.py materialize-date --as-of 20211213
```

`UPDATE_LATEST` 以北京时间为准：开市日 18:30 前选择上一交易日，18:30 起才选择当天；
休市日选择最近交易日。页面和 CLI 会同时显示实际数据截止日与选择原因。目标日一经选定，
如果官方数据仍为空、不完整或存在分类代码冲突，系统会失败关闭，不会继续向前回退。
如果所选目标日已经是 current，作业返回“已是最新”，不会重抓行业、成员或个股数据，也不
创建新 run；为了用官方交易日历完成本次选日，仍会发生一次最小 `trade_cal` 探针。

发布使用可恢复本地事务。进程若在提交点前中断会恢复旧 pointer；若在提交点后中断会保留
新 pointer 并幂等补齐唯一成功账本。存在未恢复事务时页面不会读取半发布快照，服务或 CLI
只能在取得单写者锁后恢复；不要手工删除 `transactions/publication.json`。

若身份门发现同日双码、交易日缺口、第二别名、代码碰撞、名称/父路径漂移，或行情与成员
代码不同步，本次更新会失败且不会改变 current。不要通过改名匹配、忽略缺行或聚合个股
估值绕过失败。

迁移需复制源码、`requirements.lock`、项目规格和 `output/runs/SWIVD-RUN-20260828-004/` 封存源档案，并在目标电脑重建 `.venv`。源档案是构建新快照的必要输入，不能只复制普通源码。保留 v2 历史时另行完整复制数据目录，逐 run 只读验证并核对 pointer 后再使用；已有 v2 快照中的内嵌档案不依赖源档案目录。默认数据目录：macOS 为 `~/Library/Application Support/swivd`，Windows 为 `%LOCALAPPDATA%\swivd`；`doctor/serve/update/materialize-date` 支持 `--data-dir`，验证与重建使用显式 run 路径。具体步骤见[运行与迁移手册](docs/runbook.md)。Windows 真机验收状态仍为 `WINDOWS_E2E_UNVERIFIED`。

页面保留原压缩包的 GitHub-dark 视觉语言和六类全景视图（总览、估值热力图、历史走势、涨跌排行、PE-PB 象限、数据明细），并新增三级行业成分与 SW2014 档案入口。图表由本地原生 SVG/CSS 绘制，不加载 ECharts CDN、远程字体或其他网络资源。

## 维护入口

- v2 合同：[PROJECT_CONTRACT_V2.md](PROJECT_CONTRACT_V2.md)
- v1 冻结合同：[PROJECT_CONTRACT.md](PROJECT_CONTRACT.md)
- 机器规格：[PROJECT_SPEC_V4.json](PROJECT_SPEC_V4.json)
- 架构：[docs/architecture.md](docs/architecture.md)
- 数据字典：[docs/data_dictionary.md](docs/data_dictionary.md)
- 运行与迁移：[docs/runbook.md](docs/runbook.md)
- 故障排查：[docs/troubleshooting.md](docs/troubleshooting.md)
- 本项目 Agent 规则：[AGENTS.md](AGENTS.md)

旧 `PROJECT_SPEC.json`、旧 CLI `run/rebuild` 与 `output/runs/SWIVD-RUN-20260828-004` 只用于验证和保留 SW2014 L1 历史档案，不属于 v2 日更链路。
