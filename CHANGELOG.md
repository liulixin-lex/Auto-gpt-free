# Changelog

## 2.0.2 - 2026-08-12

### Fixed

- 修复 Docker 前端构建阶段仍引用已清理根目录 `assets/` 导致镜像构建失败的问题。
- 统一使用 `frontend/public/assets/` 提供前端静态资源，不改变注册运行时、浏览器运行时或并发策略。

### Verification

- 保留 v2.0.1 已验证的后端、前端和桌面端构建结果。
- Docker 构建上下文不再依赖重复静态资源目录。

## 2.0.1 - 2026-08-12

### Changed

- 桌面端浏览器运行时改为首次使用时下载并持久缓存，Windows 安装包从约 1.95 GiB 降至约 158 MiB。
- 保留 Docker 镜像内的 Playwright Chromium 与 Camoufox 预装方式，服务器注册路径不变。
- 移除未使用的 Patchright 路径、重复浏览器配置和生产环境中的开发依赖。
- 增加浏览器运行时单实例下载锁、缓存复用和缺失运行时回归测试。
- 收紧 Electron 打包文件白名单，避免构建缓存、日志和临时目录进入安装包。
- 规范项目忽略规则、依赖分层、发布文档和跨平台换行配置。

### Verification

- 后端全量测试 282 项通过。
- 前端 lint、生产构建和 JavaScript 依赖审计通过。
- 精简后的 Windows 后端启动与健康检查通过。

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
