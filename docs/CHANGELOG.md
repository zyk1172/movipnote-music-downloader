# CHANGELOG — 音乐下载插件（MusicDownloader）

## v0.5.9（2026-08-12）
- 新增本 CHANGELOG；同步审计修复对照文档。
- 版本号/文档版本统一。

## v0.5.8（2026-08-12）— 审计修复版
依据第三方审计（MP 智能助手）修复：

| 级别 | 问题 | 修复 |
| --- | --- | --- |
| P0 | `_get_instance()` 读 `plugins`（存类）导致 MCP 工具恒返回「插件未启用」 | 改用 `running_plugins`（实例容器），music_search/music_download 恢复 |
| P1-1 | 完成状态无主动推送 | 新增低频状态对账服务（默认 30 分钟，0=关）：下载中→已完成自动推送 `download_completed` |
| P1-2 | `/on_complete` 回调与鉴权矛盾（qb 回调无法带 header） | 鉴权支持 `token` 查询参数（=webhook_token），回调命令用 `&token=` |
| P1-3 | `track_verify` 配置键三处重复 | 去重 |
| P1-4 | `searched_sites` 返回配置站点而非实际站点 | 改为返回实际返回结果的站点 |
| P2 | 曲目校验每次下载前重复拉种子 | 按 enclosure hash 缓存结果（TTL 600s） |
| P2 | `_last_fallback_*` 动态属性残留旧值 | `init_dynamic_state()` 在 `init_plugin` 显式初始化 |
| P2 | `_live_torrents` 每次新建线程池 | 模块级 `_LIVE_POOL` 复用 |
| P2 | `label`（种子标签）无表单项 | 设置页新增「种子标签」输入框 |
| P2 | 鉴权 header 传错会覆盖正确的 query apikey | header 与 query 独立校验 |
| P2 | 文件头版本注释过期 | 同步至当前版本 |

## v0.5.7（2026-08-12）
- 新增 `POST /history/clean`：按状态清理（`status`）、只保留最近 N 条（`keep`）、清理孤儿（`orphans`）。
- `/history/clear`（全清）、`/history/remove`（单条）保留。

## v0.5.6（2026-08-12）
- 修复「100% 仍显示下载中」：状态对账改为 `进度≥99.9%` 或 `state==completed` 即完成（stalledDL/queuedDL 等归一态也生效）。
- 对账逻辑统一用于 `/tasks`、`/history`、详情页。

## v0.5.5（2026-08-12）
- 实时查询改为按历史 hash 精准查（14 条而非全量 994 条，毫秒级）；超时提至 8s。
- `/test` 用探测 hash 做真实下载器连通性检查。
- 新增 `POST /history/remove`。

## v0.5.4（2026-08-12）
- 修复：单曲降级到合集后，下载上限改用合集体积上限（`album_max_size_gb`），`size_limit_gb` 随之更新。

## v0.5.3（2026-08-12）
- 修复 3 个实测 bug：`album_matched_any` 判断（此前降级恒触发、`fallback_album` 恒空）；匹配标点归一（`I, II & III`）；iTunes 跳过精选合集。

## v0.5.2（2026-08-12）
- 曲目级内容校验：下载前解析种子文件清单确认含目标歌曲；不含直接拒绝；整轨单文件放行并标注 `content_verified=null`。

## v0.5.1（2026-08-12）
- 单曲/合集分开体积上限：`max_size_gb`（单曲）+ `album_max_size_gb`（合集）；单曲降级合集走合集体积上限；search 返回 `size_limit_gb`。

## v0.5.0（2026-08-12）
- 下载前二次体积闸门（`max_size_gb`）；search 新增 `fallback_tried`/`fallback_resolved`。

## v0.4.9（2026-08-08）
- `/tasks`、`/history` 新增 `live_available`；下载器查询失败时明确提示用 `/on_complete` 判断完成。

## v0.4.8（2026-08-08）
- 新增 `/test` 测试接口（APP 测试按钮）；`max_size_gb` 仅约束单曲；接口全面返回体积信息。

## v0.4.7（2026-08-08）
- 单曲降级专辑改用 iTunes Search API 解析（MusicBrainz 限流且解析不准）。

## v0.4.6（2026-08-08）
- 单曲降级专辑：单曲未命中经元数据服务解析所属专辑并重搜。

## v0.4.5（2026-08-08）
- 修复下载器查询阻塞事件循环（线程+超时）；为 MP 智能助手声明用法（工具描述、`/音乐下载` 斜杠命令、Agent Skills）。

## v0.4.4（2026-08-08）
- 新增下载历史页面与 `/history`、`/history/clear`。

## v0.4.3（2026-08-08）
- 结果推送模型：`no_resource`/`search_ready`/`download_added`/`download_completed`/`download_failed`；`/on_complete` 回调；Agent 使用指南。

## v0.4.2（2026-08-08）
- `album_matched`/`album_aliases`；专辑未命中禁止自动下载（防下错专辑）。

## v0.4.1（2026-08-08）
- 移除周期轮询（修复 `.get()` 访问 Pydantic 报错）；插件改为纯按需。

## v0.4.0（2026-08-08）
- 搜索与筛选强化：中文紧贴格式识别（FLAC分轨）、相关度排序、单曲退艺人搜索。

## v0.3.x（2026-08-08）
- 0.3.5 下载防御+最小 MediaInfo；0.3.4 目录动态校验；0.3.3 X-Music-Token 鉴权落地；0.3.2 修正 V2 市场结构；0.3.1 修正作者/图标；0.3.0 接入官方 hash:id 引用机制 + screener + 校准。

## v0.2.0（2026-08-08）
- 全站点搜索 + 音乐/影视判别 + 无损优先排序；移除音乐站限制。

## v0.1.0（2026-08-08）
- 初版：关键词搜索 + 下载到音乐目录 + 通知。
