# Pod Hub Implementation Rules

## 文档目标

本文档收拢实现前必须统一的工程约束，避免不同模块在配置、错误处理、文件存储和任务执行规则上各自做决定。

## 环境变量契约

| 变量名 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `BASE_URL` | 是 | 无 | 对外公开访问基地址，必须包含协议与主机 |
| `FETCH_BATCH_SIZE` | 否 | `3` | 每个频道单轮抓取上限，轮询时按先新后旧使用 |
| `SYNC_INTERVAL` | 否 | `30` | 轮询周期，单位为分钟 |
| `MEDIA_DIR` | 否 | `/data/media` | 音频文件根目录 |
| `TMP_MEDIA_DIR` | 否 | `/tmp/pod-hub` | 下载中间文件临时目录 |
| `DATABASE_PATH` | 否 | `/data/db/podhub.db` | SQLite 文件路径 |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `YTDLP_BIN` | 否 | `yt-dlp` | `yt-dlp` 可执行文件路径 |
| `YTDLP_PROXY` | 否 | 无 | 传给 `yt-dlp --proxy` 的代理配置 |
| `YTDLP_JS_RUNTIMES` | 否 | 无 | 传给 `yt-dlp --js-runtimes` 的值（例如 `node` 或 `deno`） |
| `YTDLP_SOCKET_TIMEOUT` | 否 | `30` | `yt-dlp --socket-timeout`，缓解缩略图/TLS 握手超时 |
| `YTDLP_RETRIES` | 否 | `5` | `yt-dlp --retries` |
| `YTDLP_FRAGMENT_RETRIES` | 否 | `5` | `yt-dlp --fragment-retries` |
| `ALARM_URL` | 否 | 无 | 抓取异常通知接口 URL |
| `ALARM_CUSTOM_PSK` | 否 | 无 | 抓取异常通知接口 `X-Custom-PSK` 头 |
| `ALARM_TG_USER_ID` | 否 | 无 | 抓取异常通知接口 `X-TGUser-ID` 头 |
| `ALARM_TIMEOUT_SECONDS` | 否 | `10` | 通知接口请求超时时间 |
| `CLOUDFLARE_AI_GATEWAY_URL` | 否 | 无 | Cloudflare AI Gateway URL，可填写 `.../compat/chat/completions` 等 Gateway 基础 URL |
| `CLOUDFLARE_API_TOKEN` | 否 | 无 | 调用 Workers AI Whisper 的 Cloudflare API Token |

## 配置规则

- 所有环境变量由单一 `config` 模块读取和校验。
- `BASE_URL` 不允许以 `/` 结尾，入库和渲染前统一规范化。
- `FETCH_BATCH_SIZE` 和 `SYNC_INTERVAL` 必须为正整数。
- 若 `MEDIA_DIR`、`TMP_MEDIA_DIR` 或数据库父目录不存在，由应用启动时创建。
- `ALARM_URL`、`ALARM_CUSTOM_PSK`、`ALARM_TG_USER_ID` 必须同时配置；三者均为空时关闭通知。
- `CLOUDFLARE_AI_GATEWAY_URL` 与 `CLOUDFLARE_API_TOKEN` 必须同时配置；均为空时关闭字幕生成。
- 字幕模型固定为 `@cf/openai/whisper-large-v3-turbo`，由应用内部把 Gateway URL 归一化为 `.../workers-ai/@cf/openai/whisper-large-v3-turbo`，不提供模型或 Whisper 参数配置。

## `yt-dlp` 调用边界

- 频道扫描阶段只允许执行“列表提取”命令，不下载媒体。
- 节目抓取阶段允许执行元数据提取和音频下载命令。
- 所有 `yt-dlp` 调用都通过统一客户端封装，不允许在业务代码中直接拼命令。
- 若来源配置了 cookies.txt 内容，仅在调用 `yt-dlp` 时写入运行期临时文件，并通过 `--cookies` 传入；不得在页面回显或日志输出 cookie 原文。
- 标准错误输出应被截断后保存到日志和 `last_error` 摘要中。
- 镜像中固定 `yt-dlp` 版本，不使用滚动 `latest`。

## 文件存储规则

- 最终音频文件路径固定为 `MEDIA_DIR/{channel_slug}/{video_id}.m4a`（无法提取视频 ID 时回退为 URL 编码文件名）。
- 下载中间文件写入 `TMP_MEDIA_DIR/{episode_id}/{video_id}.m4a`，文件名与最终发布一致。
- 下载成功后将中间文件移动到正式路径；若跨文件系统则使用 copy + 原子替换发布。
- 数据库中的 `audio_path` 保存正式发布路径。
- 字幕文件路径固定为 `MEDIA_DIR/{channel_slug}/{video_id}.vtt`，数据库中的 `transcript_path` 保存正式发布路径。
- 若正式文件已存在，重复抓取前先校验数据库状态；首版不主动覆盖有效音频文件。

## 串行执行规则

- 同一进程中仅允许一个同步协调器实例处于运行状态。
- 调度器触发时若发现已有同步在执行，本轮直接跳过，不排队。
- 同步任务内部按频道串行执行，不开启频道级并发。
- 单频道同步内部按节目串行抓取，不开启节目级并发。
- 字幕生成由独立调度任务异步处理，每轮只处理一条 pending 字幕，避免阻塞频道同步。
- 跳过事件需要写日志；是否写入 `sync_runs` 可实现时固定为“写入一条 `skipped` 记录”。

## 错误记录规则

- 频道级错误写入 `channels.last_error`。
- 节目级错误写入 `episodes.last_error`。
- 字幕生成错误写入 `episodes.transcript_error`，不影响音频抓取成功状态。
- 任务级错误写入 `sync_runs.error_message`。
- `last_error` 与 `error_message` 建议保存截断摘要，长度上限统一为 `4096` 字符。
- 完整异常堆栈仅进入日志，不写入数据库字段。
- 自动调度同步发生抓取异常时，每个频道每轮最多发送一条通知；通知中的错误列表从本轮 `yt-dlp` 返回内容提取最简错误信息，去重后保留首次出现顺序。
- 用户在后台新增频道时触发的来源解析失败不发送通知，只按页面错误处理。

## 日志规则

- 默认输出结构化文本日志，至少包含时间、级别、模块、消息。
- 每次 `sync_run` 开始和结束都必须记录日志。
- 每次 `yt-dlp` 调用都记录目标对象、命令类型、耗时和结果。
- 媒体文件缺失、RSS 请求 404、同步跳过都需要明确日志。

## RSS 规则

- feed 中仅包含 `audio_ready` 节目。
- enclosure 的 `url`、`length`、`type` 必须来自本地已发布音频。
- 若节目字幕状态为 `ready`，feed item 输出 Podcasting 2.0 `<podcast:transcript>`，指向本地 VTT URL。
- `guid` 使用稳定值，推荐 `podhub:episode:{episode_id}`。
- 频道暂无音频时仍返回合法 RSS，避免订阅 URL 在冷启动阶段失效。

## 验收前自检清单

- 环境变量缺失或格式错误时，应用启动应快速失败。
- 后台页面中不存在任何手动同步入口。
- 数据库状态与页面展示一致。
- 同一时刻不会出现两个同步任务并发运行。
- feed 不会输出没有本地音频文件的条目。
