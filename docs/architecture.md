# Pod Hub Architecture

## 文档目标

本文档定义 Pod Hub MVP 的推荐项目结构与运行架构，用于直接指导仓库初始化、模块拆分和依赖边界控制。

## 运行形态

- 交付方式：Docker 单容器、单应用。
- Web 栈：`FastAPI` + `Jinja2` 模板。
- 数据库：`SQLite`。
- 调度：`APScheduler` 进程内定时任务。
- 抓取与下载：通过子进程调用固定版本 `yt-dlp`。
- 媒体存储：本地文件系统挂载目录。
- 并发模型：单进程串行同步任务，同一时刻只运行一个同步任务。

## 模块边界

### `web`

- 提供管理后台页面和表单处理。
- 提供公开 RSS 路由与媒体文件下载路由。
- 不直接操作 `yt-dlp` 或文件系统，统一调用服务层。

### `feed`

- 负责将频道与节目数据渲染为 RSS XML。
- 只读取 `audio_ready` 节目。
- 不负责调度和下载。

### `sync`

- 编排首次接入、定时轮询、历史补抓和失败重试。
- 负责同步状态流转、批量策略、结果汇总。
- 统一协调 `downloader`、`storage`、`db`。

### `downloader`

- 封装 `yt-dlp` 列表提取、元数据提取、音频下载。
- 负责命令参数、超时、退出码与 stderr 解析。
- 只返回结构化结果，不直接写数据库。

### `storage`

- 管理媒体目录、临时文件、原子落盘、文件存在性检查。
- 对外暴露“为某个 episode 分配目标路径”和“发布音频文件”的接口。

### `db`

- 维护 ORM 模型、会话、迁移与查询封装。
- 提供按频道读取节目、写入同步结果、更新状态的基础能力。

### `scheduler`

- 注册启动后的周期任务。
- 只负责触发 `sync` 协调器，不承载业务判断。
- 在已有任务运行时跳过本轮触发并记录日志。

### `common`

- 放置配置加载、日志、时间工具、slug 工具、异常定义等横切能力。

## 推荐目录结构

```text
pod-hub/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── common/
│   │   ├── logging.py
│   │   ├── slug.py
│   │   ├── time.py
│   │   └── errors.py
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── models/
│   │   │   ├── channel.py
│   │   │   ├── episode.py
│   │   │   ├── sync_run.py
│   │   │   └── app_setting.py
│   │   ├── repositories/
│   │   │   ├── channels.py
│   │   │   ├── episodes.py
│   │   │   ├── sync_runs.py
│   │   │   └── settings.py
│   │   └── migrations/
│   ├── downloader/
│   │   ├── client.py
│   │   └── schemas.py
│   ├── storage/
│   │   └── media_store.py
│   ├── feed/
│   │   └── service.py
│   ├── sync/
│   │   ├── coordinator.py
│   │   ├── planner.py
│   │   └── service.py
│   ├── scheduler/
│   │   └── jobs.py
│   └── web/
│       ├── routes/
│       │   ├── admin.py
│       │   ├── feeds.py
│       │   └── media.py
│       ├── templates/
│       │   ├── channels_new.html
│       │   ├── channels_list.html
│       │   └── channels_detail.html
│       └── viewmodels/
│           └── channels.py
├── docs/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## 依赖方向

- `web` 可以依赖 `feed`、`sync`、`db.repositories`、`common`。
- `scheduler` 只能依赖 `sync` 与 `common`。
- `sync` 可以依赖 `downloader`、`storage`、`db.repositories`、`common`。
- `feed` 可以依赖 `db.repositories` 与 `common`。
- `downloader`、`storage`、`db` 不依赖 `web`。
- 禁止跨层回调和循环依赖；页面模板不得直接访问 ORM 会话。

## 启动与运行流程

1. `app.main` 加载配置、初始化日志、数据库和 FastAPI。
2. 应用启动钩子创建数据库连接并注册 APScheduler 周期任务。
3. 周期任务进入 `sync.coordinator`，先检查全局串行锁。
4. 若无任务运行，协调器按频道顺序执行同步。
5. `feed` 路由按请求实时生成 XML，不单独落地静态文件。
6. `media` 路由按 `episode_id` 查库并返回本地文件。

## 管理后台信息架构

- 频道新增页：录入频道 URL、`@handle` 或频道 ID。
- 频道列表页：展示频道基础信息、状态、最近同步时间、最近错误摘要。
- 频道详情页：展示频道状态、最近同步结果、节目列表及节目处理状态。

后台不提供以下动作：

- 手动触发同步。
- 编辑频道标识。
- 删除、停用、重新启用频道。
- 直接在页面操作节目重试。

## 容器内目录布局

- 应用代码：`/app`
- 数据根目录：`/data`
- SQLite 文件：`/data/db/podhub.db`
- 音频文件目录：`/data/media`
- 临时下载目录：`/data/media/_tmp`

## 初始化顺序建议

1. 先建立 `config`、`db`、`common` 基础设施。
2. 再实现 `downloader`、`storage`、`sync`。
3. 然后实现 `feed` 路由和后台页面。
4. 最后接入 `scheduler` 与 Docker 打包。
