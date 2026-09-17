# 行业估值与资金流工作台实施规则

继承 Stock 全局治理规则。用户已于 2026-09-17 批准 2026-09-16 合并施工方案，见 GOV-20260917-INDUSTRY-INTEGRATION-001。

- 本目录是新集成项目，保留原 swivd 模块与旧规格作为只读兼容基线；新增统计、分类、调度和发布遵循 PROJECT_INTEGRATION_SPEC.json，不放宽旧快照验证。
- 原项目目录与旧运行实例不得修改。不得自动 commit、push、发布或交易。
- 模块单向依赖：纯领域规则 → provider/store → application jobs/query → HTTP → UI；领域层无网络、文件或系统时钟，provider 不计算行业指标，UI 不计算金融指标。
- Tushare 凭据仅环境或已配置本机来源，不能进入日志、前端、文件或异常。
- 测试只写临时目录；真实开发证据只写 .local/evidence，不能冒充 clean committed 正式结果。
- 真实历史成员缺失保留空缺；THS同日采集不得倒签；官方估值与中位数分列。
- 每项数据均绑定批次/日期/行业身份/来源；共享单写者与原子发布。
- 浏览器验收仅 Chrome，不自动切前台。
