# 第三方组件与再分发说明

本便携包仅将运行时、依赖、既有项目与数据组合到独立目录。没有修改 Python 解释器或第三方扩展二进制，不授予原项目或数据的额外所有权、公开再分发或商业使用许可。

- Mac：CPython 3.14.6，来自 Astral `python-build-standalone` 的 `20260804 install_only_stripped` 构建。保留 runtime 自带许可证。构建工具的 MPL-2.0 不替代其运行时各组件许可。[上游项目](https://github.com/astral-sh/python-build-standalone)
- Windows：Python.org 官方 `python-3.14.6-embed-amd64.zip`，保留其中 PSF 与相关第三方许可。仅调整私有 `python314._pth` 搜索路径指向包内依赖；不启用用户 site，不配置系统 Python。[官方内嵌分发说明](https://docs.python.org/3.14/using/windows.html#the-embeddable-package)
- CPython 版权与许可：[PSF 官方许可证](https://docs.python.org/3.14/license.html)。请同时阅读 `runtime` 中实际随包的完整许可文本。
- 20 个依赖保持 `app/requirements.lock` 精确版本。来源、wheel 文件名、SHA-256 见 `runtime-manifest.json`；完整 `.dist-info` 与许可证保留在 `packages/`。没有执行源代码构建或升级依赖。
- 启动仅需要库，不分发依赖 wheel 的开发命令行入口；相应删除/布局调整在运行时构建清单中披露。运行时内部相对链接如被解引用为同字节普通文件，仅用于便携压缩，不改变二进制内容。
- 原项目源码、冻结研究数据和历史报告仍受其原有权利与使用条件约束。Tushare 账号接口权限与数据许可不因本便携包转移，接收电脑必须使用自己的合法 token。

`package-manifest.json` 是本地完整性清单，不是代码签名、第三方安全保证或 Tushare 授权回执。包旁验收报告限定实际验证的平台、时间与范围，不能把未实测的平台写为通过。
