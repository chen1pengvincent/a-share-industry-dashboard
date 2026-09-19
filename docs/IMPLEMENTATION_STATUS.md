> 此文保留集成时的历史验收记录。当前版本已移除 Excel/CSV 下载，并修复排名、历史指标状态与检查点校验；现行范围、未解决问题及本轮验证见 [当前状态](CURRENT_STATUS.md)。下文旧导出验收不代表当前功能。

# 实施与验收状态

记录于2026-09-18，本地提交前验收快照。代码、真实数据和最终Chrome验收已完成；用户已批准本地提交及验收后正式启用。随后实际commit和正式首更记录在本机 `.local/evidence/formal-first-update-001/receipt.json`，运行状态以正式current/manifest和Stock项目登记为准。

- 新项目：`a_share_industry_dashboard`；批准 `GOV-20260917-INDUSTRY-INTEGRATION-001`。
- 最终执行源码树：`a3c762aaec8d342d61d786a3a7b635f39622144ad30eec12206517bc7afda64f`，66文件；Git基线 `e4518d433673fc76fdbae29fa4fb0271ca58ea52`。已批准仅在新项目本地提交55文件，无push/Release。
- 统一来源与批次、三个页面、四分类、日/自然周/自然月、三态排序、导出、应用内调度、日级历史失败证据、只读旧档案与恢复均已实现。
- 最终新工作台 **211/211** Python测试通过；**30/30** 前端行为检查通过。最终Python复验首次210通过、1项仅因沙箱禁止回环端口而受阻，该项限定权限重跑通过；最后两文件仅前端文案与UI测试，后端源码/测试/锁文件哈希均未变。先前指定旧SW2014/身份回归33项通过，相关原文件未改；不代表旧项目全部测试或所有平台通过。
- 实际环境：macOS arm64、CPython3.14.6，24个哈希锁定依赖匹配，pip check通过。

## 可用预览

[开发工作台 http://127.0.0.1:8766](http://127.0.0.1:8766)，自动调度关闭。原应用的8765实例保持不动。正式运行默认端口仍为8765；本机旧应用占用该端口时，新工作台可显式使用 `serve --port 8766`，先停止本次新开发预览以释放8766。

[交付审阅](FINAL_REVIEW.md)说明需求落实、模块职责、审查修复、提交范围与回滚。[安装使用手册](handbook/工作台安装使用手册.md)给出可复用命令。

## 已核实的真实数据

真实数据验收批次 `BATCH-20260917T155223-c207f40457ef` 含2026年8月全部21个交易日、20260916及20260917，共23日。此前22日固定批次 `BATCH-20260917T144736-70883475fd1d` 独立周期审查覆盖30个日/周/月周期、36,030个行业期次、6,044条成员样本，4,425,592次字段检查，0差异。8月存在84个成员集合变化行业，其中8个真实反例能识别错误的期末篮子回算。

单独真实CLI首次更新批次 `BATCH-20260917T144948-571b8ca6ac22`：491个响应/491次HTTP尝试，238秒成功；两次字面verify均VERIFIED，闭包1701对象；同日第二次更新仅一次trade_cal并保持批次。该验收固定于此前5c73源码。

最终bec7源码的8次有界数据命令与实际CLI verify全部通过，共440请求/440次HTTP尝试：三个失败日各12请求，复跑各仅一次日历；新增20260916正常日400请求，无THS历史成员请求，随后同日更新仍仅一次日历。新增日399原始对象、98,914金融值、986,167次独立Fraction检查通过，覆盖1,201行业与41,736成员。最终23日批次verify闭包6,619对象；原22日输入引用与旧不可变文件未变。

bec7读取源码与23日批次的真实HTTP验收 **42/42** 通过，包含四分类各层级日/周/月、成员/历史/质量、三项历史失败，以及CSV/XLSX下载回读；CSV父行业筛选排序8行一致，且日频记录数列名正确；Excel六Sheet合计1,201行业。HTTP验收未调用Tushare。

CI/THS官方close补充25个日期、50次真实响应、63,268行逐值核对通过。最新日CI416/416、THS90/90具有官方close，官方PE/PB仍为空；成员/资金门槛未放宽。

## Chrome 与迁移

已通过原生Google Chrome完成三个页面、四分类、层级/父级筛选、日周月、六估值视图、成员贡献、主表/成员/旧档案/质量表三态排序、键盘焦点和回补重试开关验收。实测发现零值CSS类名使整表失败，修复后CI二级108/108行业及电子父级5/108实际重测通过。截图和AX证据在本任务CUA工具会话，另有本机文字回执；未声称保存本地截图文件。

本机迁移8,535文件、439,952,972字节完整复制，独立inode；源/目标在复制与读取前后逐文件哈希完全相等。旧冻结读取源码5c73下，6,180对象闭包、27周期查询/81三页投影、旧归档、原有CSV/Excel及三个进程拒网守卫全部通过。最终bec7读取源码后继复核同样通过：27查询/81投影及成员、历史、质量、catalog指纹0差异，CLI verify闭包6,180对象，两个进程拒网守卫实际尝试0；旧迁移目录、运行文件和源码前后完全不变。

用户解锁后，最终原生Chrome复验全部通过：实际导出并保存CSV/XLSX、31行及六表1,201行业逐字节/结构回读；三失败日及原因、日期三态排序、默认不重试；迁移副本三个页面31/31行和值一致。迁移服务停止后8,535文件不变，拒网守卫实际出站0。最后前端提示收口实际刷新复验：同日更新显示“已是最新（数据至2026-09-17）”，成功导出显示“已完成”、100%而无过期计数。

前端第一轮文案修复后，以d693源码重算原23日发布开发后继 `BATCH-20260917T161948-b47d03dd0d42`，保留捕获血缘，未新增日期；Chrome统一切换通过。同日再更新仅一次trade_cal、批次不变。最后成功态隐藏阶段计数仅改前端和UI测试，真实数据与后端证据不被重标为新的捕获源码。最终Chrome回执明确区分这些阶段。
另有一项已知P2：若安装了完整旧档案，服务进程首次current读取同步执行旧严格校验，迁移副本首读超过30秒超时；随后重启主预览测得首次完整返回50.798秒，旧档案状态AVAILABLE，校验缓存建立后同类请求0.1974秒。未导入旧档案不经过该全量校验。未为降低耗时放宽验证；后续可把旧档案校验放在其独立入口，主页面只读取明确的待校验状态。

## 数据与启用边界

1. 五年历史尚未全部回填。20210917、20211213、20220104底表探针不完整，涉及300114/302132代码变更及000670/002260暂停上市证据缺口，失败日没有进入成功分片。失败表示当前证据未通过，不等于永久不可恢复；没有硬编码身份映射或放宽门槛。
2. THS缺少当天成员证据的历史留空，不能取今天成员倒灌。实际有效日数不足252，历史百分位边界已通过独立离线用例，真实长历史边界尚未满足。
3. 原两个项目和原运行实例未改；362个受保护文件逐一核对无变化。继承的src/swivd、旧tests与requirements.lock未改。
4. 本验收快照时，正式数据根 `~/Library/Application Support/ashare-industry` 尚未创建。用户已按准确范围批准本地commit及Chrome通过后启用；正式首更须由干净提交树真实获取，不复制开发批次。push/Release仍不在批准范围内。

## 证据导航

- [最终后端211项验证](../.local/evidence/final-commit-gate-001/receipt.json) · [最后前端30项与源码后继](../.local/evidence/job-status-success-final/receipt.json)
- [最终原生Chrome复验](../.local/evidence/chrome-final-002/REPORT.md)
- [前一版源码与测试回执](../.local/evidence/final-handoff-002/tests-receipt.json)
- [最终Python日志](../.local/evidence/final-handoff-002/unittest-final.log) · [UI检查日志](../.local/evidence/final-handoff-002/node.log)
- [真实周期独立报告](../.local/evidence/period-audit-final-001/REVIEW.md)
- [真实CLI首次更新验收](../.local/evidence/live-cli-001/acceptance/REPORT.md)
- [最终真实历史隔离与继续回补](../.local/evidence/history-failure-cli-001/REPORT.md)
- [最终HTTP与导出回读](../.local/evidence/http-final-002/http-results.json)
- [Chrome原生交互回执](../.local/evidence/chrome-native-001/REPORT.md)
- [迁移和离线回读](../.local/evidence/migration-001/REPORT.md) · [最终源码兼容回读](../.local/evidence/migration-final-reader/REPORT.md)
- [最终读取源码HTTP与冷启动限制](../.local/evidence/migration-chrome-final/http-read.json)
- [历史失败隔离修复](../.local/evidence/history-failure-implementation-001/REVIEW.md)
- [首次回补日期边界修复](../.local/evidence/history-range-bootstrap-001/after.json)
- [导出计数口径修复](../.local/evidence/export-period-count-labels/after.json)
- [历史底表诊断](../.local/evidence/history-universe-diagnostic/REPORT.md)
- [受保护文件复核](../.local/evidence/final-handoff-002/protected-check.json)

开发数据和审计证据仅保存在本机隔离目录，不纳入源码提交。数据展示不构成新回测或交易结论。
