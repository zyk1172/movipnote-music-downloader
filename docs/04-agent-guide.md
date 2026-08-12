# Agent 使用指南：音乐下载插件（v0.4.3）

本文面向「音乐 APP 内嵌小 Agent」的开发者，讲清楚：怎么调用、怎么决策、结果怎么回来。

## 0. MoviePilot 内置智能助手怎么用本插件

- **工具**：插件注册的 `music_search` / `music_download` 会自动出现在智能助手工具列表，描述内已内置决策规则；
- **斜杠命令**：`/音乐下载 <艺人> <专辑>`，助手可用 `run_slash_command` 直接触发整条流程（搜索→决策→下载→回复）；
- **Agent Skills（推荐）**：把插件仓库 `skill/music-downloader/` 目录复制到 MoviePilot 的 skills 目录
  （Docker 内 `/app/skills/music-downloader/`，或智能助手用户 skills 目录），助手即会加载《音乐下载插件使用说明》并严格按决策规则执行。

## 1. 两种接入方式（二选一或都接）

| 方式 | 说明 | 适合 |
| --- | --- | --- |
| **REST**（推荐，简单） | 直接 `POST/GET /api/v1/plugin/MusicDownloader/*`，鉴权 `X-Music-Token`（插件级）或 `X-API-KEY`（系统级） | 音乐 APP 自己的小 Agent |
| **MCP / 智能体工具**（v2） | 若 Agent 跑在 MoviePilot 内或走 MCP，工具 `music_search` / `music_download` 会自动出现在 `tools/list` | 与 MoviePilot 内置 Agent 集成 |

Base URL：`http://<MovipNote-Host>:3000/api/v1/plugin/MusicDownloader`

## 2. 状态模型（插件 → Agent 的“结果”）

插件会把所有结果以 `music.result` 事件推送到你配置的 **Webhook（notify_url）**，也可随时用 `/tasks` 拉取。状态共 5 种：

| type | 含义 | 何时推送 |
| --- | --- | --- |
| `no_resource` | **没有资源** | `/search` 无结果（且开启「搜索后推送结果」） |
| `search_ready` | **候选就绪** | `/search` 有结果（附 top5 摘要 + album_matched_any） |
| `download_added` | **下载中** | `/download`/`/magnet` 加入下载器成功 |
| `download_completed` | **下载成功** | `/tasks` 检测到完成 或 qBittorrent 回调 `/on_complete` |
| `download_failed` | **下载失败** | 加入失败 / 取种失败 / 异常 |

推送格式（POST 到 notify_url，`Authorization: Bearer <notify_token>`）：
```json
{
  "event": "music.result",
  "type": "download_completed",
  "payload": {
    "hash": "9f121b25...", "title": "Justin Bieber - Justice 2021 - FLAC 分轨",
    "save_path": "/media/音乐"
  }
}
```

## 3. 标准工作流（Agent 侧）

```
用户: 帮我下载 周杰伦 魔杰座
  │
  ├─ 1. POST /search {"artist":"周杰伦","album":"魔杰座","album_aliases":["Capricorn"]}
  │     └─ 返回 results[]（每条含 music/quality/relevance/album_matched/ref）
  │
  ├─ 2. 决策（核心规则）
  │     ├─ 若 album_matched_any == false：
  │     │     → 不自动下载！把候选列表展示给用户选（或让用户补英文别名再搜）
  │     │       因为中文专辑名常以英文建种（魔杰座=Capricorn），标题对不上=大概率下错
  │     ├─ 若 album_matched_any == true：
  │     │     → 选 album_matched=true 里 quality 最高者（同质则 relevance/seeders）
  │     │     → 只用所选条目的 ref 调下载
  │     └─ 若 total == 0：回应用户“没有资源”，可建议换关键词/艺人名
  │
  ├─ 3. POST /download {"ref":"<hash:id>"}
  │     └─ 返回 {hash, save_path, status:"downloading"}（= 下载中）
  │         失败则返回 {success:false, message}（= 下载失败，可重试下一候选）
  │
  └─ 4. 结果回传
        ├─ 主动推送：插件 Webhook 会在 完成/失败 时发 music.result
        └─ 被动拉取：Agent 定时 GET /tasks 拿实时 state/progress
             状态流转：downloading → completed（成功）| paused | failed
```

## 4. 决策细则（避免下错专辑的硬规则）

1. **专辑未命中禁止自动下载**：`album_matched_any=false` 时只展示候选。
2. **选型优先级**：`album_matched=true` → `quality`（无损>有损）→ `relevance` → `seeders`。
3. **失败重试**：`/download` 返回 `下载种子内容为空`/失败时，换该查询的**下一个候选 ref** 重试 1-2 次。
4. **中文专辑**：优先传 `album_aliases`（英文名），如 `魔杰座→Capricorn`、`第二天堂→The Second Heaven`。
5. **单曲**：PT 站按专辑建种，单曲名通常搜不到 → 插件会自动「退艺人搜索」，并尝试**经 iTunes 解析所属专辑后按专辑重搜**（`single_fallback_album`，默认开）；重搜后的专辑命中结果**受 `max_size_gb` 大小上限约束**，超限不下载；仍无命中则展示候选由用户选择。

## 5. 即时“下载成功”推送（可选，推荐）

插件不做周期轮询（按需设计），要**即时**收到下载完成有两种方式：

- **A. 轮询 /tasks**（零配置）：Agent 每 30-60s `GET /tasks`，插件实时查下载器并在完成时推送 `download_completed`。
- **B. qBittorrent 完成回调**（即时、免轮询）：
  1. qBittorrent → 设置 → 下载 → 完成后运行外部程序：
  ```
  curl -s -H "X-Music-Token: zyk" "http://<MP-HOST>:3000/api/v1/plugin/MusicDownloader/on_complete?hash=%I&name=%N"
  ```
  2. 下载完成 → qB 调用该 URL → 插件标记完成并推送 `music.result(type=download_completed)` 到 notify_url。

## 6. 接口速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/test` | 测试插件（启用/目录/站点/下载器/元数据服务），供 APP 测试按钮 |
| POST | `/search` | `{keyword?, artist?, album?, album_aliases?, kind?, year?, limit?, prefer_lossless?, min_seeders?}` |
| POST | `/download` | `{ref}` 或 `{site_id,index}` 或 `{torrent:{...}}` |
| POST | `/magnet` | `{magnet, title?}` |
| GET | `/tasks?status=` | 实时任务（含下载器 state/progress） |
| GET | `/history` | 下载历史（含实时状态） |
| POST | `/history/clear` | 清空下载历史 |
| GET | `/sites` | 生效搜索站点 |
| GET | `/status` | 插件状态（目录校验、站点、筛查配置） |
| POST | `/notify/test` | 测试结果推送 |
| GET | `/on_complete?hash=%I&name=%N` | qB 完成回调 |

## 7. 失败模式与处理

| 现象 | 原因 | Agent 处理 |
| --- | --- | --- |
| 401 apikey 校验不通过 | Token 错 / 未配置 webhook_token | 检查 `X-Music-Token` |
| 音乐下载目录未通过校验 | music_dir 不在 MoviePilot 下载目录内 | 提示用户修目录（见 02-design §2.1） |
| 引用已失效，请重新搜索 | 搜索缓存被覆盖/过期 | 重新 `/search` 再下载 |
| 加入下载失败: 下载种子内容为空 | 站点取种失败（偶发） | 重试下一个候选 ref |
| 没有可搜索的站点 | 未配置索引站点 | 提示用户到 MoviePilot 站点设置 |
