# 接口契约：音乐下载插件 MusicDownloader（v0.5.9）

> 本文是**给其他 Agent / APP 接入的唯一权威接口文档**。以本文件为准，不要自行发明接口或字段。

## 0. 基础约定

- Base URL：`http://<MovipNote-Host>:3000/api/v1/plugin/MusicDownloader`
- 鉴权（两种任选，推荐插件级）：
  - `X-Music-Token: <webhook_token>`（插件设置「音乐APP调用Token」）
  - `X-API-KEY: <系统API_TOKEN>`
- 响应统一：`{"success": true|false, "message": "...", "data": {...}}`；**以 `success` 为准**，HTTP 200 ≠ 业务成功。
- 插件为**按需设计**：不轮询下载器；完成判定用 `/tasks`（按 hash 精准查询，毫秒级）或 qBittorrent 完成回调 `/on_complete`。
- 站点 Cookie / UA / 代理、下载器连接全部由 MoviePilot 管理，APP/Agent **不接触 Cookie**。

## 1. 端点总表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/test` | 测试插件（启用/目录/站点/下载器/元数据服务），供「测试按钮」 |
| POST | `/search` | 搜索+筛查+可选单曲降级专辑，返回候选 |
| POST | `/download` | 按 `ref`（或 `site_id+index`/`torrent`）加入下载；支持体积闸门与曲目校验 |
| POST | `/magnet` | 磁力链下载 |
| GET | `/tasks?status=` | 实时任务（含下载器 state/progress；按历史 hash 精准查询） |
| GET | `/sites` | 生效搜索站点 |
| GET | `/status` | 插件状态（目录校验/站点/筛查/体积上限配置） |
| POST | `/notify/test` | 测试结果推送 |
| GET | `/history` | 下载历史（含实时状态） |
| POST | `/history/clear` | 清空全部历史 |
| POST | `/history/remove` | 移除单条历史 `{hash}` |
| POST | `/history/clean` | 按条件清理 `{status?, keep?, orphans?}` |
| GET | `/on_complete?hash=%I&name=%N` | qBittorrent 完成回调（内部用，APP 无需调） |

## 2. 测试按钮 → `POST /test`

```json
{ "checks": [
  { "name": "插件启用",       "ok": true,  "detail": "已启用" },
  { "name": "音乐下载目录",   "ok": true,  "detail": "/media/音乐" },
  { "name": "搜索站点",       "ok": true,  "detail": "4 个" },
  { "name": "下载器连接",     "ok": true,  "detail": "可达" },
  { "name": "元数据服务(iTunes)", "ok": true, "detail": "可达" }
], "summary": "全部通过" }
```
APP 的「测试按钮」调此接口，逐项展示 `ok/detail`。

## 3. 搜索 → `POST /search`

请求：
```json
{
  "keyword": "周杰伦 魔杰座",       // 可选：完整关键词
  "artist": "周杰伦",               // 可选
  "album": "魔杰座",                // 可选；单曲场景把歌名放这里
  "album_aliases": ["Capricorn"],  // 可选：专辑英文/别名（中文专辑必传）
  "kind": "single",                // single=单曲(用单曲上限) / album=专辑合集(用合辑上限) / auto=自动
  "year": 2008,                    // 可选
  "limit": 10,
  "prefer_lossless": true,
  "min_seeders": 1
}
```

响应 `data`：
```json
{
  "keyword": "周杰伦 魔杰座 2008",
  "searched_sites": ["馒头", "PTzone"],
  "total": 4,
  "album_matched_any": true,        // 是否有条目命中专辑/歌曲
  "fallback_tried": false,          // 是否触发过单曲->专辑降级
  "fallback_resolved": null,        // 降级解析到的专辑（iTunes，即使站点没有）
  "fallback_album": null,           // 站点上实际命中的降级专辑（未命中为 null）
  "kind": "single",                 // 实际模式
  "size_limit_gb": 2.0,             // 本次生效的体积上限（单曲→单曲上限；降级合集→合辑上限）
  "size_limit_applied": true,
  "dropped_video": 3,
  "dropped_uncertain": 0,
  "results": [
    {
      "index": 3, "ref": "77598c7:3",
      "site_id": 4, "site_name": "馒头",
      "title": "Justin Bieber - Justice 2021 - FLAC 分轨",
      "category": "未知",
      "music": true, "confidence": "high",
      "audio_format": "FLAC",
      "quality": 90, "quality_label": "无损",
      "relevance": 70, "album_matched": true,
      "size": 317860175, "size_text": "303.2 MB",
      "seeders": 34, "pubdate": "...", "enclosure": "https://..."
    }
  ]
}
```

字段语义：
- `music`：true 音乐 / false 影视 / null 不确定；`quality`：100 无损-高规格 / 90 无损 / 60 有损-高质量 / 40 有损 / 30 未知；
- `relevance`：艺人+30、专辑/别名+40（标点容错），0-100；
- `album_matched`：专辑名/别名/歌名是否命中标题或副标题；
- `size`/`size_text`：体积（字节/可读）；
- `ref`：下载引用 `hash:id`，**仅本批搜索缓存内有效**。

## 4. 下载 → `POST /download`

```json
{ "ref": "77598c7:3",
  "max_size_gb": 2.0,               // 必传：把 search 返回的 size_limit_gb 原样带回
  "verify_song": "Poker Face",      // 单曲场景必传：下载前曲目内容校验
  "verify_artist": "Lady Gaga" }    // 配合校验
```
（也支持 `{site_id, index}` 或 `{torrent:{title,enclosure}}`；磁力走 `/magnet`。）

成功响应：
```json
{ "success": true, "data": {
  "hash": "d058e52c...", "save_path": "/media/音乐",
  "size": 380154480, "size_text": "362.5 MB",
  "content_verified": true,          // true=文件清单含目标歌 / false=不含已拒绝 / null=整轨等无法校验(放行)
  "matched_files": ["03. Like A Virgin.flac"],
  "label": "音乐,musicdownloader", "status": "downloading" } }
```

失败响应：`{ "success": false, "message": "<原因>" }`，常见：
- `资源 <size> 超过大小上限 <N>GB，已拒绝下载`（体积闸门）
- `资源不含目标歌曲（内容不匹配），已拒绝下载`（曲目校验）
- `引用已失效，请重新搜索` / `音乐下载目录未通过校验` / `加入下载失败: 下载种子内容为空`

## 5. 状态 → `GET /tasks`

```json
{ "success": true, "data": {
  "live_available": true,           // false=下载器查询失败，完成判定改用 /on_complete
  "tasks": [ { "hash": "...", "title": "...", "site": "馒头",
               "status": "completed",   // 下载中/已完成/失败/暂停
               "state": "completed", "progress": 100.0,
               "dlspeed": "...", "save_path": "/media/音乐" } ] } }
```
状态对账规则：**`progress ≥ 99.9%` 或 `state == completed` → 已完成**；完成时推送 `music.result(type=download_completed)`。

## 6. 历史 → `/history`、`/history/clear`、`/history/remove`、`/history/clean`

- `GET /history`：`{live_available, tasks:[{title,site,status,progress,size,size_text,save_path,create_time,finish_time,hash}]}`
- `POST /history/remove`：`{"hash":"..."}`
- `POST /history/clean`：`{"status":"completed|failed|paused|downloading", "keep": 20, "orphans": true}`（可组合；只动记录不动文件）

## 7. 结果推送（插件 → Agent Webhook）

统一 `music.result` 事件 POST 到 `notify_url`（`Authorization: Bearer <Webhook Token>` 可选）：
```json
{ "event": "music.result", "type": "download_completed",
  "payload": { "hash": "...", "title": "...", "save_path": "/media/音乐", "size": 123, "size_text": "..." } }
```

| type | 含义 | 触发 |
| --- | --- | --- |
| `no_resource` | 没有资源 | search 无结果 |
| `search_ready` | 候选就绪（含 top5+album_matched_any） | search 有结果 |
| `download_added` | 下载中 | 加入下载器成功 |
| `download_completed` | 下载成功 | /tasks 对账到完成 或 qb 回调 |
| `download_failed` | 下载失败 | 加入失败/超限/内容不匹配/异常 |

## 8. 决策规则（防止下错专辑/误下大包）

1. `kind=single`（单曲）：搜索用**单曲体积上限**；单曲未命中 → iTunes 解析专辑 → 按专辑重搜（**降级后自动切到合辑体积上限**）；
2. `album_matched_any=false` → **禁止自动下载**，只展示候选；
3. 自动下载选型：`album_matched=true` → `quality` → `relevance` → `seeders`；
4. 自动下载必须传 `max_size_gb`（=search 的 `size_limit_gb`）+ 单曲场景传 `verify_song`/`verify_artist`；
5. 失败重试：`下载种子内容为空` 时换下一个候选 ref 重试 1-2 次。

## 9. 完成判定（按可靠性排序）

- **A. qBittorrent 完成回调（推荐，即时可靠）**：qb 设置 → 下载 → 完成后运行外部程序：
  `curl -s "http://<MP>:3000/api/v1/plugin/MusicDownloader/on_complete?hash=%I&name=%N&token=<token>"`
- **B. `/tasks` 轮询**：`live_available=true` 时按 `state/progress` 判定完成（毫秒级，按历史 hash 精准查）。
- **C. 低频对账（内置）**：插件默认每 30 分钟自动对账一次（设置「状态对账间隔(分钟)」，0=关），下载中→已完成时主动推送 `download_completed`，客户端不轮询也能收到。

## 10. 错误码速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `apikey 校验不通过` | Token 错/未配置 | 检查 X-Music-Token |
| `音乐下载目录未通过校验` | 目录不在 MoviePilot 下载目录内 | MP 目录设置修复 |
| `引用已失效` | 搜索缓存被覆盖 | 重新 /search |
| `live_available=false` | 下载器查询失败/超时 | 查 qb 连接；完成判定用 /on_complete |
| `超过大小上限` | 体积闸门 | 换小资源或调高对应上限 |
| `资源不含目标歌曲` | 曲目校验未过 | 换候选 |
