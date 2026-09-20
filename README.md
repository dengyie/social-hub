# social-hub

[![CI](https://github.com/dengyie/social-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/dengyie/social-hub/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

自研多平台社媒自动发布系统——一次创作，多平台分发。

双通道架构：

- **API 通道**：平台官方 API，跑在无头服务器上（已支持：微信公众号、B 站 biliup-rs、掘金 Cookie REST）
- **CDP 通道**：附着到按账号隔离的真实 Chrome Profile，为没有稳定 API 的平台准备（小红书等，M1+）

当前状态：**13 个真实平台适配器代码完备**（公众号/B站/掘金/小红书/知乎/抖音/视频号/快手/百家号/头条/CSDN/微博/支付宝生活号）+ CDP 舰队 + 一键全平台扇出，101 测试全绿。CDP 平台的选择器需真机校准（`shub doctor`），校准 + 真实账号登录后才可实际发布；微博/支付宝生活号为预置待校准，默认 fan-out 跳过（显式 `shub publish` 仍可试）。[已有平台的真实发布状态见下方矩阵。]

## 平台支持矩阵

| 平台 | 通道 | 类型 | 幂等 | 上线前置 |
|---|---|---|---|---|
| 微信公众号 `gzh` | API | 图文（HTML） | 草稿箱+freepublish 两段式 | app_id/secret + IP 白名单 |
| B站 `bili` | API | 视频 | bvid 断点（绝不重传） | `biliup-rs login` 扫码（仅个人使用） |
| 掘金 `juejin` | API | Markdown | draft_id→article_id 三段式 | 浏览器 Cookie |
| 小红书 `xhs` | CDP | 图文/视频（媒体必需，按 cover_kind 分流） | 回执断点 | Chrome + 扫码 + 选择器校准 |
| 知乎 `zhihu` | CDP | 专栏文章（纯文本） | 回执断点 | Chrome + 登录 + 选择器校准 |
| 抖音 `douyin` | CDP | 图文/视频/纯文案 | 回执断点 | Chrome + 扫码 + 选择器校准 |
| 视频号 `channels` | CDP | 视频/图片（先进首页再点「发表视频」，不直开 create） | 回执断点 | Chrome + 微信扫码 + 选择器校准 |
| 快手 `kuaishou` | CDP | 图文/视频（媒体必需，按 cover_kind 分流） | 回执断点 + 确认弹窗 | Chrome + 扫码 + 选择器校准 |
| 百家号 `baijiahao` | CDP | 视频（必需；标题区等上传后挂载） | 回执断点 | Chrome + 百度登录 + 选择器校准 |
| 今日头条 `toutiao` | CDP | 图文/视频（按媒体 kind 分流发布页） | 回执断点 | Chrome + 登录 + 选择器校准（SAU 无头条，全量预置待校准） |
| CSDN `csdn` | CDP | 博客（Markdown） | 回执断点 | Chrome + 登录 + 选择器校准 |
| 微博 `weibo` | CDP | 图文/视频/纯文字 | 回执断点 | Chrome + 登录 + 选择器校准（**calibrate-only**：fan-out 跳过） |
| 支付宝生活号 `alipay` | CDP | 视频/图片（必需） | 回执断点 | Chrome + 登录 + 选择器校准（**calibrate-only**：fan-out 跳过） |
| `mock` | API | 测试假平台 | 全链路 | 无（演示/验收用） |

选择器来源分级：**小红书** = XiaohongshuSkills（2026-03 真机验证）图文+视频 tab + SAU `target=image|video`；**抖音/快手/百家号/视频号** = social-auto-upload 同源流程（抖音「发布图文」tab；快手 role=tab「图文」；视频号先进 `/platform` 再点「发表视频」；百家号标题区 180s 等待）；**头条/CSDN** = 预置 + 部分真机校准 + SPA `first_has`；**微博/支付宝生活号** = 预置待校准（支付宝 URL 已对齐 SAU `c.alipay.com` + `_appScene=CONTENT`，仍 calibrate-only）。CDP 红线：独立端口一律 ≥9300 按账号独立 Profile；**9222 为共享浏览器 attach-only**（绝不代启/代关/无域清 cookie，见「设计要点」）；**绝不杀 Chrome 进程**（只 disconnect）；验证码 → `captcha_wait` 冻结等人。媒体 kind 由 `media_kinds` 在提交前强校验（图片不会误投视频平台）。

## 特性

- **持久队列**：SQLite 即队列（零外部依赖）——`BEGIN IMMEDIATE` 认领 + 租约心跳 + 到期回收 + 单账号限频（每日上限 / 最小间隔）
- **幂等发布**：幂等键 + 部分唯一索引防重；两段式发布证据逐阶段落盘，重试 / 崩溃恢复**绝不重复发布**
- **适配器契约**：统一 `check_login / publish / verify / engage` 接口；错误分类驱动状态机（瞬时→退避重排 / 凭据失效→needs_login / 验证码→captcha_wait / 永久→failed）
- **可观测性**：`/healthz` `/readyz` `/metrics` 三探针、结构化 JSON 日志（自动脱敏）、任务时间线 `task_events`、SSE 进度回放
- **凭据安全**：Fernet 加密入库，明文不落盘
- **性能**：认领热路径走覆盖索引 O(log n)，2k 行历史认领 ~1ms（基准：`scripts/bench.py`）

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# POSIX: .venv/bin/python -m pip install -e ".[dev]"

.venv/Scripts/shub serve                            # http://127.0.0.1:8767
```

### 端到端演示（无需真实凭据，内置 mock 平台）

```bash
shub account add mock --alias demo --var token=demo
shub draft create --title "你好 social-hub" --content-file ./demo.html --platform mock --account demo
shub publish --draft 1 --platform mock --account demo --wait
shub task show 1        # status=done, result_ref=https://mock.example/note/...
```

### 接入微信公众号

```bash
shub account add gzh --alias main --var app_id=wxXXXX --var app_secret=XXXX
# 注意：服务器出口 IP 必须先加入公众号后台「IP 白名单」
shub media add ./cover.jpg
shub draft create --title "标题(≤64字)" --content-file ./article.html \
  --author your-name --cover-media 1 --platform gzh --account main
shub publish --draft 1 --platform gzh --account main --wait
```

公众号发布链路：上传封面素材 → 草稿箱 → `freepublish` 发布 → 轮询核验 → 返回文章链接。

### 远程服务器 / Docker

```bash
docker run -d --name social-hub -p 127.0.0.1:8767:8767 \
  -v ~/.social-hub:/data \
  -e SOCIAL_HUB_API_TOKEN=change-me \
  ghcr.io/dengyie/social-hub:latest

> **⚠️ 必须设置 `SOCIAL_HUB_API_TOKEN`**：容器绑定 `0.0.0.0`，未设置 token 时 `shub serve`
> 会拒绝启动（deny-by-default）。端口映射/隧道会把请求转成"来自 127.0.0.1"，
> 无 token 的 loopback 放行逻辑会误放行公网请求。

# 或本地构建
docker compose -f deploy/docker-compose.yml up -d   # 监听 127.0.0.1:8767
# 公网暴露建议走 Cloudflare Tunnel 等隧道，容器不直接裸端口
# 设置 SOCIAL_HUB_API_TOKEN 后 API 强制 Bearer 鉴权；未设置仅放行 loopback
```

## 开发与发布

```bash
python -m pytest -q          # 101 测试（CI 矩阵：py3.10/3.11/3.12 + Windows）
python scripts/bench.py      # 性能基准
```

- **CI**：push / PR 自动跑 pytest 矩阵（Linux py3.10–3.12 + Windows）
- **打包发布**：推 tag（`git tag v0.1.0 && git push --tags`）→ 测试通过后自动构建 sdist/wheel 上传 GitHub Release，并构建 Docker 镜像推到 `ghcr.io/dengyie/social-hub`（`v` 前缀剥离 + `latest`）

## 目录结构

```
server/social_hub/   # FastAPI daemon（模块化单体：core / adapters / content / media / vault / web）
cli/                 # shub 命令行（typer）
tests/               # 队列语义 / 状态机 / 适配器契约 / gzh mock / API / CLI
scripts/             # 性能基准 bench.py
deploy/              # Dockerfile / docker-compose
docs/                # ADR 架构决策记录
```

## Roadmap

| 里程碑 | 内容 |
|---|---|
| M0 ✅ | 核心引擎 + 公众号 API 通道 |
| M1 ✅ 代码 | CDP 舰队管理（9300+ / 独立 Profile）+ 小红书/知乎/抖音/视频号适配器 |
| M2 ✅ 代码 | B站 biliup-rs 投稿（定时发布 M0 已内建；Web UI 待做） |
| M3 ✅ 代码 | 扇出（一键全平台）+ 掘金 + 微博/支付宝生活号；AI 内容工坊 / MCP 接口待做 |
| M4 | 生产化（选择器真机校准 + 数据回收 + 告警 + 金丝雀排程） |
| M5 | 互动引擎（自动评论 / 点赞 / 收藏，复用 AutomationTask 动作泛化） |

## 设计要点

- **双通道**：`lane: api | cdp` 是适配器一等属性；API 通道零浏览器依赖，CDP 通道只为没有稳定 API 的平台存在
- **动作泛化**：发布只是 `AutomationTask.action_type` 的第一个动作（publish/comment/like/favorite/collect_metrics），评论点赞等互动功能复用同一底座
- **安全红线**：CDP Chrome 独立实例端口一律 **9300+** 且按账号独立 Profile（`min 9300` 起递增）；**9222 为共享浏览器（daily-checkin 专用 profile）只看不动——attach-only，绝不代启、绝不代关、绝不无域清 cookie**；其余 <9300 一律拒绝。Cookie 清除必须带域名过滤；daemon 崩溃恢复后中断任务标 failed，**绝不自动重发**

## License

[AGPL-3.0](LICENSE)
