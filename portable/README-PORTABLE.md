# 申万行业估值仪表盘 · 便携版

内置 Python 3.14.6、全部锁定依赖、原项目源码，以及截至 **2026-09-04** 的冻结数据。解压完整文件夹即可运行；联网更新只需要你自己的 Tushare Pro token，仍受该账号的接口权限、频次和数据发布时间限制。不需要安装 Python、pip、Node 或开发工具。

仅供研究：`RESEARCH_ONLY / decision_eligible=false / production_approved=false`。页面的百分位不是投资建议，不能据此宣称可交易、生产可用或产生新回测结论。

## 选哪个包

| 文件名后缀 | 目标电脑 | 验收边界 |
| --- | --- | --- |
| `macos-arm64.zip` | Apple Silicon（M 系列），macOS 11 或更新 | 原生 Mac 验收结果以包旁验收报告为准；不代表每个旧系统版本实测 |
| `macos-x86_64.zip` | Intel Mac，macOS 10.15 或更新 | 依赖与架构检查不等于 Intel 真机验收 |
| `windows-x86_64.zip` | Windows 10/11，64 位 x64 | `WINDOWS_E2E_UNVERIFIED`，在真实 Windows 完成下述验收前不得视为最终通过 |

系统下限来自运行时编译目标和依赖 wheel 标签，不承诺任意旧电脑。Windows ARM64 原生、32 位 Windows、Windows 7/8.1 不在本版范围。选择与电脑 CPU 对应的包，不要将 Mac 的 runtime 复制到 Windows。

## 第一次使用

1. 将 ZIP **完整解压**到自己的可写目录。不要在压缩包预览中启动，不要放在系统安装目录、云盘同步目录或只读移动盘。Windows 建议短路径，例如 `C:\SWIVD`，避免深层目录的系统路径长度限制。中文和空格路径有专项测试。
2. Mac 双击 `Start-Mac.command`；Windows 双击 `Start-Windows.cmd`。保留打开的终端窗口。校验完整性和首次导入数据可能需要数十秒，请等待明确的服务启动提示。
3. 若已有 `TUSHARE_TOKEN` 环境变量或本机 Tushare SDK token，自动使用；否则在终端提示后粘贴 token 并回车。**输入不显示字符，这是正常的。** Token 仅保存在此次进程内，不写配置、文件或网页，也不保存到系统；重启时需要重新输入。
4. 暂时没有 token 可直接回车，仅浏览已带数据。页面更新没有可用 token 时会失败，不表示已有数据损坏。
5. 看到 `SERVING` 后，自己在 Chrome 打开 **http://127.0.0.1:8765**。程序不会自动打开、切换浏览器，也不监听局域网。

macOS 可能对未经公证的下载程序提示安全确认。先核对包旁的 SHA-256 和来源，再按照 [Apple 官方安全说明](https://support.apple.com/zh-cn/102445)批准具体项目。不要关闭 Gatekeeper，不要批量删除下载隔离标记。组织安全策略可能禁止运行，此时请联系管理员；本包不会绕过策略。本版本不是已签名/公证的安装器。

如果解压工具丢失 Mac 执行权限，可在终端进入解压目录后运行：

```sh
chmod u+x Start-Mac.command runtime/bin/python3.14
./Start-Mac.command
```

## 使用、更新与停止

- 切换申万 L1/L2/L3、搜索行业、查看行业历史和成员估值。保留 SW2014 L1 历史档案，但不向其倒灌当前成员。
- 每次更新由页面按钮明确触发。日期按上海时区 18:30 截止规则选择；未到时间使用上一可更新交易日并说明原因。数据不齐仍会失败，不偷偷补前值。
- 后台更新不替换当前正在浏览的快照。成功显示新数据就绪后，由你点击切换。历史日期需要明确点击载入，不能改写当前日更指针。
- 更新失败时保留原数据；权限不足、分类冲突、行数截断、网络中断等需依错误码排查，不要通过修改 manifest 或关闭校验绕过。
- 退出前等待更新达到成功或失败终态，再在服务终端按 **Ctrl+C**。不要在更新途中直接拔盘或复制文件夹；意外退出后下一次正常启动会按项目既有事务机制恢复。
- 默认端口占用时，先关闭你此前启动的同一服务。也可进入包目录运行 `./Start-Mac.command serve --port 8766`（Windows 为 `Start-Windows.cmd serve --port 8766`），再访问相应端口。不要终止电脑上全部 Python 进程。

## 数据放在哪里、怎样再迁移

```text
解压目录/
  Start-*.command 或 Start-*.cmd   本平台入口
  launcher.py                     安全启动与校验
  runtime/                        私有 Python（不可跨平台混用）
  packages/                       锁定依赖和许可证
  app/                            原字节源码、网页和完整 RUN-004 档案
  seed-data/                      不可变的 9 月 4 日初始数据
  data/state/                     首次启动后生成，本实例后续更新只写这里
  package-manifest.json            静态文件的版本与 SHA-256 清单
  runtime-manifest.json            下载来源、运行时和依赖取证
```

首次运行只会从已验证的 `seed-data` 初始化全新的 `data/state`。已有 `data/state` 会验证后继续使用，不被种子覆盖；已有无效目录会报错，不进行猜测性合并。不会读取或修改源电脑正式 `Application Support/swivd` 数据。

同平台迁移：等待作业终态、停止服务，将整个文件夹复制到新电脑，再启动并重新提供 token。跨平台迁移：解压目标平台新包，在首次启动前将旧包的整个 `data` 目录复制到新包旁；**不要覆盖已运行的新包 data，也不要混合两个 runs/ledger 目录**。启动会重新校验数据。保留原副本直到新电脑验收通过。

旧不可变 manifest、合同或冻结源内保留原电脑的历史路径，这是来源档案，不是新版服务的运行路径依赖。不要为了清理姓名/路径而修改这些历史字节，否则哈希校验必然失败。分发给他人前应考虑历史路径隐私和 Tushare 数据许可；本包供你在自有或已获授权的电脑间迁移，并非公开数据再分发授权。

不要编辑或向 `seed-data/runs`、`app/output/runs` 内部添加文件；Finder 在封存目录内产生 `.DS_Store` 也会改变其完整库存。如果出现 `SNAPSHOT_METADATA_UNEXPECTED`，保留运行数据，从原 ZIP 重新解压到新目录，不要修改快照清单。

## 校验与目标机验收

包旁的 `.zip.sha256` 用于传输完整性校验，不是数字签名；应通过可信来源取得校验值。Mac 可用 `shasum -a 256 文件.zip`；Windows PowerShell 可用 `Get-FileHash 文件.zip -Algorithm SHA256`，与提供的值比对。

进入已解压目录，只读检查不会提示 token，不初始化 data，也不联网：

```sh
# Mac
./runtime/bin/python3.14 -I -B -X utf8 launcher.py verify
./runtime/bin/python3.14 -I -B -X utf8 launcher.py doctor
```

```bat
rem Windows
runtime\python.exe -I -B -X utf8 launcher.py verify
runtime\python.exe -I -B -X utf8 launcher.py doctor
```

依赖、时区、旧档案和数据校验应通过；未输入 token 时 `token_configured=false` 正常。完整独立验收在系统临时目录复制一份数据和程序，验证离线重建、中文空格迁移与本地 HTTP，不读取真实 token、不调用真实数据接口：

```sh
# Mac；Windows 改为 runtime\python.exe
./runtime/bin/python3.14 -I -B -X utf8 verify_delivery.py --package .
```

Windows 接收者还需双击启动器，手动无回显输入 token，在 Chrome 打开页面并完成一次真实更新和历史载入，检查进度、旧页面继续浏览、成功后切换，再安全停止。把**不含 token**的安全结果及错误码返回给维护者；不能仅凭“压缩包解压成功”关闭 Windows 验收。

如果发现文件损坏，请保留 `data` 和错误码，从可信来源重新解压到新的目录；不要编辑清单、停用校验或覆盖旧数据。若系统路径过长、无写权限、架构错误、终端不能隐藏输入，先解决提示的环境问题。

## 维护边界

新便携层不修改原项目公式、成员身份规则、领域模块或校验器；原维护文档在 `app/docs`。已有 9 月 4 日 seed 的 source 闭包与 `app` 原字节绑定，修改已有源码/README 后不能继续假称旧 seed 与新代码匹配。后续迭代应遵守项目批准、冻结和重建流程，不删校验门。

构建源码位于维护项目的 `portable/`；`prepare_runtime.py` 从固定官方来源获取运行时/锁定 wheel，`build.py` 只读组装新目录和 ZIP，`tests/` 测试启动与打包边界。用户机不调用构建器，不联网下载可执行代码。外层启动器只支持显式本地运行，不新增计划任务、云部署、账户、评分或交易。
