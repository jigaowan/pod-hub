# Pod Hub Data Model

## 文档目标

本文档定义 MVP 的持久化结构、状态枚举和关键约束，目标是让实现者可以直接据此创建 ORM 模型、数据库迁移和查询接口。

## 设计原则

- 首版仅支持 YouTube 频道，但保留最小 source 扩展空间。
- 公开 URL 必须稳定：频道 feed 依赖不可变 `slug`，媒体 URL 依赖不可变 `episode.id`。
- 数据库负责保存发现结果、处理状态和最近错误，不保存复杂任务编排状态机。
- 历史节目可以长期停留在未抓音频状态，但不进入 RSS。

## 实体总览

- `Channel`：一个被纳管的 YouTube 频道。
- `Episode`：频道下发现的一期视频节目。
- `SyncRun`：一次频道同步执行记录。
- `AppSetting`：少量系统级键值配置或运行标记。

## `channels`

### 字段

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PK | 内部主键 |
| `source_kind` | TEXT | NOT NULL | 固定为 `youtube_channel`；为未来 source 扩展预留 |
| `external_channel_id` | TEXT | NOT NULL, UNIQUE | YouTube 频道稳定 ID |
| `source_input` | TEXT | NOT NULL | 管理员原始输入值，可能是 URL、`@handle` 或频道 ID |
| `source_url` | TEXT | NOT NULL | 归一化后的频道 URL |
| `display_name` | TEXT | NOT NULL | 展示名称 |
| `slug` | TEXT | NOT NULL, UNIQUE | feed 路径标识；创建后不自动变更 |
| `status` | TEXT | NOT NULL | 见下方枚举 |
| `last_synced_at` | DATETIME | NULL | 最近一次成功完成同步的时间 |
| `last_error` | TEXT | NULL | 最近一次同步错误摘要 |
| `created_at` | DATETIME | NOT NULL | 创建时间 |
| `updated_at` | DATETIME | NOT NULL | 更新时间 |

### 状态枚举

| 值 | 含义 |
| --- | --- |
| `pending_initial_sync` | 已创建，尚未完成首次同步 |
| `active` | 可正常参与定时轮询 |
| `syncing` | 当前正在同步 |
| `error` | 最近一次同步失败，但下轮仍可继续重试 |

### 索引

- `UNIQUE(slug)`
- `UNIQUE(external_channel_id)`
- `INDEX(status, last_synced_at)`

## `episodes`

### 字段

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PK | 内部主键，也是媒体公开 URL 标识 |
| `channel_id` | INTEGER | NOT NULL, FK -> channels.id | 所属频道 |
| `source_video_id` | TEXT | NOT NULL | YouTube 视频 ID |
| `webpage_url` | TEXT | NOT NULL | 视频页面 URL |
| `title` | TEXT | NOT NULL | 节目标题 |
| `description` | TEXT | NULL | 节目描述 |
| `upload_date` | DATE | NULL | 上游上传日期 |
| `published_at` | DATETIME | NULL | 对外展示时间，优先使用可解析发布时间 |
| `duration_seconds` | INTEGER | NULL | 音频时长 |
| `thumbnail_url` | TEXT | NULL | 缩略图地址 |
| `audio_path` | TEXT | NULL | 本地音频绝对或相对存储路径 |
| `audio_mime_type` | TEXT | NULL | 默认 `audio/mpeg` |
| `audio_bytes` | INTEGER | NULL | 文件大小，供 RSS enclosure length 使用 |
| `audio_fetched_at` | DATETIME | NULL | 音频准备完成时间 |
| `listed_at` | DATETIME | NOT NULL | 首次被发现并入库时间 |
| `status` | TEXT | NOT NULL | 见下方枚举 |
| `last_error` | TEXT | NULL | 最近一次抓取失败摘要 |
| `created_at` | DATETIME | NOT NULL | 创建时间 |
| `updated_at` | DATETIME | NOT NULL | 更新时间 |

### 约束

- `UNIQUE(channel_id, source_video_id)`
- `FOREIGN KEY(channel_id) REFERENCES channels(id)`

### 状态枚举

| 值 | 含义 |
| --- | --- |
| `discovered` | 已发现，仅有基础列表信息，尚未进入抓取 |
| `queued` | 已被当前同步轮次选中，待抓取音频 |
| `fetching` | 正在执行元数据/音频抓取 |
| `audio_ready` | 本地音频已准备完成，可进入 feed |
| `failed` | 最近一次抓取失败，等待后续轮询重试 |
| `skipped` | 当前明确不处理，例如上游不可用或元数据缺失 |

### 索引

- `INDEX(channel_id, published_at DESC)`
- `INDEX(channel_id, status, listed_at DESC)`
- `INDEX(status, audio_fetched_at)`

## `sync_runs`

### 字段

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | INTEGER | PK | 内部主键 |
| `channel_id` | INTEGER | NOT NULL, FK -> channels.id | 同步所属频道 |
| `status` | TEXT | NOT NULL | 见下方枚举 |
| `started_at` | DATETIME | NOT NULL | 任务开始时间 |
| `finished_at` | DATETIME | NULL | 任务结束时间 |
| `discovered_count` | INTEGER | NOT NULL DEFAULT 0 | 本轮新发现节目数 |
| `queued_count` | INTEGER | NOT NULL DEFAULT 0 | 本轮进入抓取队列的节目数 |
| `fetched_count` | INTEGER | NOT NULL DEFAULT 0 | 本轮成功生成音频的节目数 |
| `failed_count` | INTEGER | NOT NULL DEFAULT 0 | 本轮失败节目数 |
| `error_message` | TEXT | NULL | 任务级错误摘要 |
| `created_at` | DATETIME | NOT NULL | 创建时间 |

### 状态枚举

| 值 | 含义 |
| --- | --- |
| `running` | 正在执行 |
| `succeeded` | 成功完成 |
| `failed` | 执行失败 |
| `skipped` | 因全局锁或无可执行工作而跳过 |

### 索引

- `INDEX(channel_id, started_at DESC)`
- `INDEX(status, started_at DESC)`

## `app_settings`

### 字段

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `key` | TEXT | PK | 设置项名称 |
| `value` | TEXT | NOT NULL | 字符串化值 |
| `updated_at` | DATETIME | NOT NULL | 最近更新时间 |

### 用途约束

- 仅用于少量全局设置和运行标记。
- 不用于保存页面表单草稿或大体量业务数据。
- 若后续引入更复杂运行控制，可替换为专用表而非继续扩展本表。

## 关键查询约定

- 频道列表页：按 `created_at DESC` 读取 `channels`。
- 频道详情页：按 `published_at DESC, id DESC` 读取该频道节目。
- RSS 生成：只查询 `status = 'audio_ready'` 的节目，并按 `published_at DESC, id DESC` 输出。
- 历史补抓：查询 `status IN ('discovered', 'failed')` 且 `audio_path IS NULL` 的最旧未完成节目，最多 `FETCH_BATCH_SIZE` 条。

## URL 稳定性规则

- `channels.slug` 一旦写入并用于 feed 发布，不因频道名变化自动重算。
- `episodes.id` 为媒体 URL 的唯一标识，媒体文件名应与该值绑定。
- 若频道新增时 slug 冲突，采用追加数字后缀的方式解决，如 `example`, `example-2`。

## 与未来扩展的兼容边界

- 当前只实现 `source_kind = 'youtube_channel'`。
- `external_channel_id`、`source_input` 和 `source_url` 保留了未来兼容 playlist/source 的空间，但本版不引入多态关联表。
- 若未来支持 playlist，应新增独立 source 表或扩展 channel/source 拆分，而不是复用 `episodes` 以外的现有状态语义。
