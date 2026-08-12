# Changelog

## 2.0.0 - 2026-08-12

### Changed

- 收敛为 ChatGPT 注册、检测和账号管理内核，移除 Kiro、Cursor、BitBrowser、自动探测、自动补号、远端同步和支付实验模块。
- 协议、无头和有头模式使用独立执行路径，不再跨模式自动 fallback。
- 重构协议状态机、Camoufox 浏览器状态机、验证码分类、Session/OAuth 与成功判定。
- 默认并发统一为 1，并增加出口级自适应容量、节奏控制、冷却和熔断。
- 新增 RegistrationAttempt、资源租约、结构化事件、任务摘要和失败证据。
- 完善邮箱隔离、OTP 去重、任务取消和浏览器进程回收。
- 重构任务详情、日志筛选、错误统计和中英文界面。

### Release

- Docker 镜像发布到 `ghcr.io/liulixin-lex/auto-gpt-free`。
- Windows 与 macOS 安装包内置 Chromium 和 Camoufox 运行时。
- 桌面数据库与失败证据保存在系统用户数据目录，升级不会覆盖账号数据。
