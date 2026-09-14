# SWIVD v2 项目维护规则

本文件只能收紧上级 `/Users/chenyipeng/Desktop/Codex/Stock/AGENTS.md`，不能放宽其治理要求。

1. 修改前先沿 `run_dashboard.py → v2_jobs/v2_pipeline → v2_provider → tushare_client` 与 `v2_server → v2_validator → ui` 识别调用链。
2. 行业官方估值、点时成员、个股横截面估值是三个独立领域。禁止以 `daily_basic` 聚合替代 `sw_daily`，禁止把当前成员倒灌历史。
3. 公式、成员边界、数据 schema、latest、ledger、manifest、端口与 token 来源是受保护合同；语义变更须先获用户精确批准。
4. 测试只能写系统临时目录。不得让测试、doctor、validate 或 serve 启动过程更新正式 latest。
5. 新接口默认失败关闭；不得静默前值填充、日期回退、选择重叠行业或吞掉行限/字段漂移。
6. Token 不得出现在参数、网页、URL、配置、日志、进度、manifest、原始响应和测试夹具。测试凭据只能使用明确的金丝雀假值。
7. 维护一个能力只改其最小层：纯计算在 `v2_domain.py`，网络在 `v2_provider.py`，不可变状态在 `v2_storage.py`，作业在 `v2_jobs.py`，HTTP 在 `v2_server.py`，UI 在 `web/`。
8. Windows 在真实主机完成前始终标记 `WINDOWS_E2E_UNVERIFIED`；不得把静态路径测试表述为真实 E2E。
