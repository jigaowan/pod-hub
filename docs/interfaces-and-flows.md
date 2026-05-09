# Pod Hub Interfaces And Flows

## 文档目标

本文档定义 MVP 的页面接口、公开路由、关键业务流程和状态流转，保证页面行为、数据更新和公开 feed 输出在实现时保持一致。

## 页面接口

### 后台入口

- `GET /admin/channels`
- 作用：展示频道列表、频道状态、最近同步时间、最近错误摘要。
- 页面元素：
  - 新增频道入口
  - 频道名称与 feed 链接
  - 频道状态标签
  - 最近同步时间
  - 最近错误摘要

### 新增频道页

- `GET /admin/channels/new`
- `POST /admin/channels`
- 入参：
  - `source_input`：必填，支持频道 URL、`@handle`、频道 ID
- 提交成功：
  - 创建频道记录
  - 立即返回频道详情页
  - 首次同步不由页面触发，等待调度器执行
- 提交失败：
  - 展示字段级校验错误或解析错误

### 频道详情页

- `GET /admin/channels/{channel_id}`
- 展示内容：
  - 频道基础信息
  - 固定 feed URL
  - 当前状态
  - 最近一次同步摘要
  - 节目列表与节目处理状态
  - 最近错误信息

### 不提供的后台接口

- 不提供手动同步按钮或路由。
- 不提供编辑、删除、停用、恢复接口。
- 不提供节目级重试按钮。
- 不提供独立 JSON 管理 API 作为首版契约。

## 公开接口

### RSS Feed

- `GET /feeds/{channel_slug}.xml`
- 行为：
  - 按 `channel_slug` 查找频道
  - 只输出 `audio_ready` 节目
  - 返回有效 RSS XML
- 异常：
  - 频道不存在返回 `404`
  - 频道存在但暂无音频节目时返回合法但为空的 feed

### 音频文件

- `GET /media/{episode_id}.m4a`
- 行为：
  - 按 `episode_id` 查找节目
  - 校验节目状态为 `audio_ready`
  - 返回本地 M4A 文件
- 异常：
  - 节目不存在返回 `404`
  - 状态非 `audio_ready` 或文件丢失返回 `404`

## 首次接入流程

1. 管理员提交 `source_input`。
2. 服务端解析输入并创建 `channels` 记录，状态写为 `pending_initial_sync`。
3. 调度器在下一个周期选中该频道。
4. `yt-dlp` 进行频道列表提取，返回全量视频条目。
5. 系统为所有视频创建或更新 `episodes` 记录，状态初始为 `discovered`。
6. 系统按发布时间倒序挑选最近 `FETCH_BATCH_SIZE` 个未抓音频节目。
7. 选中节目依次进入 `queued`、`fetching`、`audio_ready` 或 `failed`。
8. 同步完成后更新频道状态、最近同步时间和 `sync_runs`。

## 定时轮询流程

1. APScheduler 按 `SYNC_INTERVAL` 触发轮询任务。
2. 协调器获取全局串行锁；若失败则写入一条 `sync_runs.status = 'skipped'` 记录并结束本轮。
3. 任务按频道顺序执行。
4. 每个频道先重新拉取列表并与现有节目做差异比较。
5. 每个频道单轮抓取总量受 `FETCH_BATCH_SIZE` 限制，新发现节目优先进入抓取队列。
6. 若本轮新节目处理后仍有余量，再用剩余额度补抓历史未完成节目。
7. 全部处理结束后释放全局锁。

## 历史补抓策略

- 使用与首次接入相同的 `FETCH_BATCH_SIZE` 作为单频道单轮抓取总上限。
- 候选范围仅包含 `status IN ('discovered', 'failed')` 且没有本地音频的节目。
- 顺序按最早未完成的节目优先，确保历史库存逐步补齐。
- 历史补抓只使用新节目处理后的剩余额度，不因某个频道积压很多内容而独占整轮任务。

## 失败重试流程

1. 节目抓取失败时，将节目状态写为 `failed`，记录 `last_error`。
2. 频道级同步失败时，将频道状态写为 `error`，并记录最近错误。
3. 下一个轮询周期中，频道仍参与同步。
4. 失败节目在历史补抓阶段按常规候选条件重新进入队列。
5. 成功后清空节目 `last_error`，状态改为 `audio_ready`。

## Feed 生成规则

- feed 生成时不读取 `discovered`、`queued`、`fetching`、`failed`、`skipped` 节目。
- enclosure URL 固定为 `BASE_URL + /media/{episode_id}.m4a`。
- 每个条目至少包含标题、描述、发布时间、guid、enclosure。
- 若频道暂无任何 `audio_ready` 节目，仍输出频道级 RSS 壳，不输出 item。

## 状态流转

### `channel.status`

```text
pending_initial_sync -> syncing -> active
active -> syncing -> active
pending_initial_sync -> syncing -> error
active -> syncing -> error
error -> syncing -> active
error -> syncing -> error
```

### `episode.status`

```text
discovered -> queued -> fetching -> audio_ready
discovered -> queued -> fetching -> failed
failed -> queued -> fetching -> audio_ready
failed -> queued -> fetching -> failed
discovered -> skipped
```

### `sync_run.status`

```text
running -> succeeded
running -> failed
running -> skipped
```

若频道列表成功但本轮入队节目存在抓取失败，本轮也必须结束为 `failed`，并通过 `failed_count` 和 `error_message` 展示具体节目失败情况。

## 页面与数据一致性要求

- 后台列表页和详情页展示的状态，必须直接来源于数据库状态字段，而不是现场推导。
- 页面上的最近错误只展示摘要；完整错误以日志为准。
- 详情页的节目列表可以展示未抓音频节目，但需要明确区分“已进入 feed”和“仅已发现未就绪”。
