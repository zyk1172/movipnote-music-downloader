# MusicDownloader —— MovipNote（MoviePilot V2）音乐下载插件

在所有已启用索引站点上搜索并筛查音乐资源，用 MoviePilot 下载器下载到「音乐下载目录」。
**仅使用下载能力**：不刮削、不整理、不订阅；站点 Cookie / 搜索 / 下载器调用全部复用 MoviePilot 自身。

- 搜索：`SearchChain.async_search_by_title`（关键词跨全分类，普通站点也有音乐）
- 筛查：`screener.py` 纯 Python 引擎（音乐/影视判别 + 无损优先 + 质量排序）
- 下载：`DownloadChain.download_single(mediainfo=None, save_path=音乐目录)`
- 引用：与 MoviePilot 内置 Agent 工具共用 `__search_result__` 缓存，使用官方 `hash:id` 引用
- 通知：Webhook 直推音乐 APP + 可选 MoviePilot 原生渠道

## 目录

```
plugins.v2/musicdownloader/
├── __init__.py        # 插件主体（_PluginBase，REST API + Agent 工具 + 服务 + 通知）
├── screener.py        # 纯 Python 筛查引擎（无 MoviePilot 依赖，可独立测试）
└── icon.png
package.v2.json        # MoviePilot V2 市场索引（version 需与 __init__.py plugin_version 一致）
calibrate.py           # 筛查准确率校准（offline 夹具 / live 真实站点）
tests/
├── fixtures/          # 带标注的真实风格标题（music/video/uncertain）
└── test_screener.py   # pytest 回归测试
```

## 安装

### 方式一：插件市场（推荐）

1. 本仓库已推送至 **https://github.com/zyk1172/movipnote-music-downloader**（公开，`main` 分支）；
   已按 MoviePilot **V2 市场规范**组织：`package.v2.json` 索引 + `plugins.v2/musicdownloader/` 代码目录；
2. MoviePilot 后台 → 设置 → 插件市场 → 加入本仓库地址；
3. 安装「音乐下载」，按配置页填写：
   - 音乐下载目录（MoviePilot 已配置下载目录或其子目录）
   - 搜索站点范围（默认全部启用站点；可 include/exclude）
   - 筛查项（仅保留音乐 / 无损优先 / 最低做种 / 体积上限 / 排除关键词）
   - 音乐 APP Webhook URL（通知回调）
4. 保存启用后查看日志：`目录校验通过 / 搜索站点=N`。

### 方式二：本地开发

```bash
# 把 plugins.v2/musicdownloader/ 的内容放到 MoviePilot 的 app/plugins/musicdownloader/ 下（或插件市场指向本仓库）
# 设置环境变量 PLUGIN_AUTO_RELOAD=true 可热加载
```

## 测试与校准

```bash
pip install pytest
python -m pytest tests -v          # 回归测试（无需 MoviePilot）
python calibrate.py                # 离线：夹具集准确率（阈值可 --threshold 0.9）
python calibrate.py --mode live \
  --url http://<MP-HOST>:3000 --api-key <API_TOKEN> \
  --query "周杰伦 叶惠美" --query "Adele 30" --dump live_dump.json
```

`calibrate.py --mode live` 请求插件 `/search` 接口，用真实站点结果检查判别效果；把 `live_dump.json`
里的新样本按 `expected: music/video/uncertain` 标注后放进 `tests/fixtures/`，即可持续校准特征库
（`screener.py` 顶部 `AUDIO_PATTERNS / VIDEO_PATTERNS / MUSIC_CATEGORY / VIDEO_CATEGORY`）。

## 接口

见仓库根目录 `docs/03-agent-contract.md`（交付包内）：`POST /api/v1/plugin/MusicDownloader/search|download|magnet`、
`GET tasks|sites|status`、`POST notify/test`；搜索返回 `hash:id` 引用 `ref`，下载直接用 `ref`。
