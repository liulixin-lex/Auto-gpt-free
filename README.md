# Auto-gpt-free

Auto-gpt-free 是一个专注 ChatGPT 账号注册、检测和本地管理的 Web 面板。项目只加载 ChatGPT 平台，不包含自动探测、自动补号、Kiro、Grok、Cursor 或支付流程。

## 保留模块

### 1. ChatGPT 注册

- 协议、Camoufox 无头、Camoufox 有头三种严格独立模式
- 批量数量、并发和代理参数
- 邮箱验证码读取、Session 校验与 OAuth PKCE 凭证获取
- 同一模式内的阶段重试；失败时不会自动切换执行方式

### 2. 验证码与过盾

- Cloudflare Turnstile 本地求解器
- YesCaptcha、2Captcha 等远程验证码 provider
- FlareSolverr clearance 与手动 Cookie 模式
- 静态代理、动态代理和代理连通性检查
- 挑战分类、代理/指纹绑定 clearance 与 provider 独立容量

### 3. 账号检测

- 单账号和批量检测任务
- Token、套餐、订阅与账号有效性状态
- 检测记录、状态回写和任务日志

### 4. 账号管理

- ChatGPT 账号列表、详情、导入、导出和删除
- 凭证、邮箱信息和套餐状态展示
- JSON、Sub2API 和 CPA 格式导出

### 5. 配置与基础设施

- 邮箱 provider、验证码 provider 和注册策略配置
- 代理、FlareSolverr 与 clearance 运行配置
- FastAPI 接口、SQLite 持久化和任务运行时
- React + Vite 管理界面与访问密码

### 6. 任务与可观测性

- RegistrationAttempt 阶段状态机与统一错误码
- 代理、邮箱、验证码和浏览器资源租约
- SQLite WAL、单 writer 批量事件、SSE 与轮询兜底
- 任务摘要、阶段漏斗、attempt 表、错误排行和筛选日志
- 失败时保存脱敏截图、DOM 摘要与诊断信息

## 三种注册模式

| 模式 | 运行方式 | 默认并发 | 默认上限 |
|---|---|---:|---:|
| 协议 | 动态线程许可；隔离 JS Runtime 计算 Sentinel；注册请求保持同一 HTTP Session | 1 | 20 |
| 无头 | 每账号一个 Camoufox 子进程；Windows 原生 headless，Linux 默认虚拟显示 | 1 | 4 |
| 有头 | 与无头共享状态机，仅窗口可见；Windows 桌面或 Linux Xvfb/noVNC | 1 | 2 |

用户未填写时默认并发为 1。实际并发取用户请求、模式容量、代理出口、邮箱 provider、验证码容量、机器内存和全局任务容量的最小值；健康控制器会在连续成功后逐级放量，在 CF、429 或身份错配时自动降档。静态代理同一时刻只分配给一个 attempt；动态网关默认按一个真实出口计算。

统一阶段：

```text
prepare -> preflight -> auth_begin -> email_submit -> otp_trigger
-> otp_wait -> otp_submit -> profile_create -> callback
-> session_validate -> persist -> done
```

协议注册会发现 Sentinel SDK、校验兼容 hook 并缓存内容 hash。检测到接口漂移后，协议槽位进入短时熔断并返回 `SENTINEL_SDK_DRIFT`。OTP、创建账号和 OAuth token exchange 等有副作用请求不会被通用重试器盲目重放。

浏览器注册的父进程负责 heartbeat、总预算和完整进程树回收。未知页面会结束当前 attempt，并将脱敏证据登记到 `data/registration-artifacts`；主链不调用实时 AI 操作页面。

## 已移除模块

- 自动探测、定时探测和启动探测
- 自动补号、补号阈值和自动注册后台循环
- 自动上传、远端同步和 token 保活
- 通用平台动作与动作结果界面
- Kiro、Grok、Cursor 平台代码
- PayPal、Stripe、GoPay 支付实验脚本与启动入口

## 快速启动

环境要求：Python 3.11+、Node.js 22+、npm。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

cd frontend
npm ci
npm run build
cd ..

python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

启动后访问 `http://localhost:8000`。Docker 环境可运行：

```bash
docker compose up -d --build
```

发布标签会通过 GitHub Actions 生成 Windows 与 macOS 桌面安装包，并同步发布 GHCR Docker 镜像。服务器镜像预装 Playwright Chromium 与 Camoufox；桌面端在首次使用对应模式时下载运行时并持久缓存，后续升级继续复用。数据库、浏览器缓存和失败证据保存在系统用户数据目录，应用更新不会覆盖账号数据。

## 项目结构

```text
.
├── api/                    # FastAPI 路由
├── application/            # 注册、检测、账号和任务应用逻辑
├── core/                   # 配置、平台基类、代理与过盾运行时
├── domain/                 # 账号与任务领域模型
├── infrastructure/         # SQLite 仓储和平台运行时
├── platforms/chatgpt/      # ChatGPT 注册、检测与凭证处理
├── providers/              # 邮箱、验证码和代理 provider
├── frontend/               # React + Vite 管理界面
├── services/               # 任务、浏览器运行时与本地打码服务
├── electron/               # 桌面主进程与发布配置
├── scripts/                # 运维与冒烟检查脚本
├── tests/                  # 自动化测试
└── main.py                 # 应用入口
```

## 观测接口

```text
GET  /api/registration/capabilities
POST /api/registration/capabilities/test
GET  /api/tasks/{task_id}/summary
GET  /api/tasks/{task_id}/attempts
GET  /api/tasks/{task_id}/events
GET  /api/tasks/{task_id}/artifacts
```

能力检测只在用户调用测试接口时执行，不创建定时探测任务。事件使用 `schema_version=2` 的结构化字段，同时保留旧版 `line/message/detail` 兼容读取。普通日志会脱敏 Token、Cookie、OTP、密码、代理口令、授权 code 与 PKCE verifier。

## 重构后模块

1. ChatGPT 注册编排。
2. 协议注册引擎与 OAuth PKCE。
3. Camoufox 无头注册引擎。
4. Camoufox 有头注册引擎。
5. 邮箱与 OTP Provider。
6. 代理、网络与资源租约。
7. Turnstile、Sentinel 与 clearance。
8. Session 与凭证处理。
9. ChatGPT 账号检测。
10. 账号管理、导入与导出。
11. 任务调度、并发、取消和进程监督。
12. 结构化日志、统计与失败证据。
13. 配置、访问认证与持久化仓储。
14. React 管理界面与 Windows/Linux/Docker 部署。
15. 离线失败页面诊断工具。

## 验证

```bash
python -m pytest -q

cd frontend
npm run lint
npm run build
```

## 许可

AGPL-3.0
