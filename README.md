# A股估值看板

申万行业估值全景仪表盘（SWIVD）：在本机浏览器中查看 SW2021 一级、二级、三级行业的官方 PE/PB、历史统计位置、价格走势与点时成分股估值，并独立保留 SW2014 一级行业历史档案。

这是研究工具，不提供交易信号、投资评分、自动交易或盈利保证。项目保持 `RESEARCH_ONLY`、`decision_eligible=false`、`production_approved=false`；没有可交付的新回测结论。

## 先读哪份文档

- 初次使用、Mac ARM 启动、Token、迁移与排错：[使用引导手册](docs/handbook/使用引导手册.md)。主要面向**另行取得的便携包**。
- 修改代码、理解数据契约、验证与打包：[工程交接文档](docs/handbook/工程交接文档.md)。
- 本仓库与原始维护目录的关系、未上传材料：[发布范围说明](docs/PUBLICATION_SCOPE.md)。

## 仓库里有什么

完整维护源码包括 Python 后端、原生 HTML/CSS/JavaScript 前端、核心与便携层测试、启动脚本、便携包构建和验收工具、机器规格、领域合同、依赖锁和两份手册。项目历史审查资料作为历史证据保留，不是当前测试通过的保证。

**源码齐全不等于数据齐全。** 不包含 Tushare 凭据、真实行情数据、实例数据目录、封存 `output/`、固定 seed、私有 Python 运行时、虚拟环境或已构建便携 ZIP。GitHub 的 **Download ZIP 是源码包，不是双击即用的便携包**；不要双击 `portable/Start-Mac.command`，它是构建模板。

首次联网更新必须先另行取得并验证 `output/runs/SWIVD-RUN-20260828-004/` 完整历史源档案；完整核心测试中的历史档案用例还依赖 RUN-001/002/003。构建便携包还需要固定 seed 和各平台运行时资产。缺件时停止相应步骤，不删除校验、伪造文件或跳过测试后宣称全部通过。详见[交接文档第 2 节](docs/handbook/工程交接文档.md#2-完整交接必须交哪些材料)。数据的获取、使用和再分发须符合提供方和账户权限。

## 功能与数据边界

| 数据 | 用途 | 不能替代什么 |
| --- | --- | --- |
| Tushare `sw_daily` | 行业指数官方 PE/PB 与价格序列 | 不能用个股估值聚合补缺 |
| `index_member_all` | 按指定日期还原成分关系 | 不能把当前成员倒灌历史 |
| `daily_basic` | 目标交易日的个股横截面估值 | 不是个股历史估值百分位 |
| SW2014 封存档案 | 独立历史查询 | 不与 SW2021 拼接计算百分位 |

页面包括总览、估值热力图、历史走势、涨跌排行、PE–PB 象限、数据明细、行业成分和历史档案。图表使用本地 SVG/CSS，不依赖远程图表 CDN。百分位只表示统计位置，不等于“低估”或“高估”。

## 快速开始

### 1. 获取源码和建立环境

需要此私有仓库访问权限。应用支持 CPython 3.11–3.14；完整便携层维护建议使用 **CPython 3.14**。Node.js 用于前端行为测试，应用运行不需要 Node，也没有 npm 构建步骤。

```bash
git clone https://github.com/chen1pengvincent/a-share-valuation-dashboard.git
cd a-share-valuation-dashboard
```

macOS：已安装 Python 3.14，当前目录为新克隆目录时执行。若已有 `.venv`，先确认它属于当前电脑和项目，不覆盖旧环境。

```bash
test ! -e .venv &&
python3.14 -m venv .venv &&
.venv/bin/python -m pip install --require-hashes -r requirements.lock &&
.venv/bin/python -m pip check
```

Windows 命令提示符（不是 PowerShell；同样仅用于没有 `.venv` 的新目录）：

```bat
if not exist .venv (py -3.14 -m venv .venv && .venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock && .venv\Scripts\python.exe -m pip check)
```

安装失败时停止，不继续启动；不要复制另一台电脑的 `.venv`，不要只安装两个顶层依赖。`pip install -e .` 不能代替完整锁文件安装，项目按源码目录运行。

### 2. 先做离线检查

以下命令不发起行情更新；Windows 将解释器路径换为 `.venv\Scripts\python.exe`。

```bash
.venv/bin/python -B run_dashboard.py validate-spec --spec PROJECT_SPEC.json &&
.venv/bin/python -B run_dashboard.py validate-spec --spec PROJECT_SPEC_V4.json
```

环境诊断（只报告凭据是否配置，不验证其有效性）：

```bash
.venv/bin/python -B run_dashboard.py doctor --data-dir ./data/source-instance
```

纯源码克隆的 `legacy_archive_available=false` 是缺少未上传历史档案的预期结果。`token_configured=true` 只证明找到了非空凭据；终端 `SERVING` 也只证明服务已启动，**都不证明 Token 有效或能成功取数**。

### 3. 启动本机页面

首次源码体验使用本克隆专用的 `./data/source-instance`（已被 Git 忽略），不要复制旧实例数据进去。下面从仓库根直接启动；macOS 启动脚本也可传入相同 `--data-dir` 参数：

```bash
.venv/bin/python -B run_dashboard.py serve --data-dir ./data/source-instance
```

Windows 命令提示符中对应为 `.venv\Scripts\python.exe -B run_dashboard.py serve --data-dir .\data\source-instance`。不要不带参数双击源码启动器来测试克隆版，它会使用系统默认数据目录。`serve` 会创建目录、加锁并在需要时恢复事务，因此不是纯读取操作。

启动后在本机 Chrome 打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，保持终端开启。不会自动打开浏览器。不要双击 `web/index.html`，不要将服务绑定到公网。无有效快照时不能展示完整行情；首次更新前先补齐历史档案与有效账户权限。

便携版：使用另行取得、校验通过且架构匹配的包，Mac ARM 双击包根的 `Start-Mac.command`；它与源码版小写 `start_macos.command` 不是同一个入口。不要把 README 或其他新增文件塞进旧便携包内，其静态文件清单有严格校验。

### 4. Token、更新与停止

- 凭据读取顺序是进程环境变量 `TUSHARE_TOKEN`、Tushare 本机配置；便携启动器找不到时另有终端隐藏输入提示。源码启动器没有该提示。
- 使用本机安全的凭据配置方式，不把真实值写进 Git、README、命令行参数、截图或日志，也不要从网页输入。
- 准备好数据材料、有效凭据与接口权限后，可在页面手动更新。日更按北京时间 18:30 截止规则选择目标交易日；选定后缺数据会失败关闭，不会静默向前找“能成功的一天”。
- 等待更新任务结束，再在终端按 `Ctrl+C`。不要手动删除事务文件、锁、ledger 或 latest pointer 来绕过故障。
- 源码版默认实例目录为 macOS `~/Library/Application Support/swivd`、Windows `%LOCALAPPDATA%\swivd`；便携版在包内 `data/state`。不要混用。

## 开发、测试与维护

```text
run_dashboard.py       显式 CLI 入口
src/swivd/             计算、身份解析、取数、快照、事务、服务
web/                   原生前端，无 npm 构建依赖
tests/                 核心、数据、恢复、安全与前端行为测试
portable/              自包含运行包的准备、构建、启动与验收
portable/tests/        独立便携层测试
docs/handbook/         使用手册与工程交接
docs/reviews/          历史审查资料
```

修改前阅读 [Agent 维护规则](AGENTS.md)、[当前领域合同](PROJECT_CONTRACT_V2.md)、[机器规格](PROJECT_SPEC_V4.json)和[架构](docs/architecture.md)。行业官方估值、点时成员和个股横截面三个领域不得互相替代。公式、schema、身份别名、发布事务与凭据边界不是普通重构细节。

安装锁定依赖并准备好 Node 后，在源码根运行两套独立测试。测试不需要真实 Token，业务写入使用临时目录；但核心套件包含直接读取封存档案的用例，**纯源码克隆不能预期全绿**。缺少 Node 时部分 UI 检查会跳过。保留失败与跳过结果，按缺输入、环境阻断或产品回归分类。

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -B -m unittest discover -s portable/tests -p 'test_*.py' -v
```

打包先读[交接文档第 8 节](docs/handbook/工程交接文档.md#8-代码到新包当前最大的维护陷阱)。现有构建器绑定固定 seed 和源码字节，不是任意工作树的通用打包器。**本次 README 已变化，不能直接匹配旧 seed 的源码闭包。** 保留的[原始 README 字节备份](docs/reference/SOURCE_README_20260914.md.txt)仅供核对，不自动恢复绑定；备份内部链接按原项目根解释。复现旧包应在另一个全新目录恢复完整原字节闭包并验证，不在当前工作树覆盖文件。新版本发布需要新的批准与完整验收，本次不创建运行包 Release。

## 已知限制与交付证据

发布核对日期：2026-09-14。范围与验证入口见[发布说明](docs/PUBLICATION_SCOPE.md)。历史应用/便携验收详见交接文档和其引用报告。仓库提交证明文件版本，不证明行情新鲜、完整业务 E2E 或策略有效。

Intel Mac 与 Windows 的原生完整 E2E 仍未验证。上传源码不会关闭这些缺口，也不会自动将 GitHub 副本变成原系统的 canonical 数据根或授予生产权限。

仓库未授予开源许可证；私有仓库访问权不等于对外再分发许可。第三方运行时及依赖说明见 [THIRD-PARTY](portable/THIRD-PARTY.md)。
