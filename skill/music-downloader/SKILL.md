---
name: 音乐下载插件使用说明
description: 通过 MoviePilot「音乐下载」插件搜索并下载音乐。适用于用户要求下载某艺人/某专辑/某首歌曲的场景。覆盖搜索、音乐/影视筛查、专辑命中判断、自动下载规则、结果推送与失败处理。
version: 1
license: MIT
compatibility: MoviePilot V2 + MusicDownloader 插件 >= 0.4.5
metadata:
  allowed-tools:
    - music_search
    - music_download
    - run_slash_command
    - query_plugin_capabilities
    - get_search_results
---

# 音乐下载（MusicDownloader）使用说明

## 可用工具

- `music_search`：搜索+筛查音乐资源（艺人/专辑/单曲），返回候选（含 `quality`/`relevance`/`album_matched`/`size`/`ref`）；`kind=single/album/auto` 控制大小上限是否生效。
- `music_download`：按 `ref` 把音乐加入 MoviePilot 下载器，保存到已配置的音乐下载目录。
- 斜杠命令：`/音乐下载 <艺人> <专辑>`（可经 `run_slash_command` 触发整条流程）。

## 标准流程

1. `music_search` 搜索（优先给 `artist`+`album`；中文专辑务必给 `album_aliases` 英文名，如 魔杰座→Capricorn）。
2. 按决策规则处理结果（见下）。
3. `music_download` 用所选条目的 `ref` 下载。
4. 结果状态由插件以 `music.result` 推送（`no_resource`/`search_ready`/`download_added`/`download_completed`/`download_failed`），也可用 `run_slash_command` 里的 `/tasks` 或插件 `/tasks` 接口拉取。

## 决策规则（必须遵守，防止下错专辑）

- **`album_matched_any=false` → 禁止自动下载**：只把候选展示给用户选择，或让用户补专辑别名再搜。中文专辑在 PT 站多以英文/别名建种，标题对不上=大概率下错。
- **`album_matched_any=true`**：选 `album_matched=true` 中 `quality` 最高者；相同则 `relevance` 高；再相同则 `seeders` 高。
- **单曲**：PT 站按专辑/艺人建种，单曲名常搜不到；插件会自动退艺人搜索，并**经 iTunes 解析所属专辑后按专辑重搜**（`single_fallback_album` 默认开）；专辑候选**受 `max_size_gb` 大小上限约束**，命中才可自动下载，否则展示候选让用户选。
- **失败重试**：`music_download` 失败（如 `下载种子内容为空`）时，换下一个候选 `ref` 重试 1-2 次。

## 常见状态

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `no_resource` | 没有资源 | 建议换关键词/艺人名 |
| `download_added` | 下载中 | 记 hash，显示“下载中” |
| `download_completed` | 下载成功 | 提示成功，触发 APP 扫描音乐目录 |
| `download_failed` | 下载失败 | 展示原因，可重试下一候选 |
| `引用已失效` | ref 过期 | 重新 `music_search` |
