"""
MusicDownloader —— MovipNote（MoviePilot V2）音乐下载插件
========================================================
通过 MoviePilot 自身的「站点搜索 + 下载器下载」能力，为 iOS/macOS 音乐 APP 内嵌小 Agent 下载音乐。

定稿前提（用户已确认）：
- MoviePilot V2 已安装，下载目录已配置；
- 站点 Cookie / UA / 代理、站点搜索、下载器调用全部复用 MoviePilot 自身能力；
- 不限定音乐站点：在所有已启用索引站点上关键词搜索，由本插件做「音乐/影视判别 + 无损优先 + 质量排序」。

本版本（0.3.0）：
- 搜索结果与 MoviePilot 内置 Agent 工具共用缓存（__search_result__），使用官方 `hash:id` 引用机制下载；
- 筛查引擎抽离到 screener.py（纯 Python），可独立跑 pytest / calibrate.py 校准准确率。

设计原则：
- 只用下载能力：关键词搜站点 -> 加下载任务（mediainfo=None, save_path=音乐目录）
- 不用刮削 / 整理 / 订阅：不调用 chain.transfer()、不创建订阅、不管理站点 Cookie
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, Depends, Header, HTTPException

from app.chain.download import DownloadChain
from app.core.config import settings
from app.chain.search import SearchChain
from app.core.context import Context, MediaInfo
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.directory import validate_download_save_path
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TorrentInfo
from app.schemas.types import MediaType, NotificationType, SystemConfigKey
from app.utils.crypto import HashUtils

from .screener import (
    build_keyword, classify, evaluate, fmt_size, norm, quality_label, screen,
)

try:  # v2 智能体工具支持
    from app.agent.tools.base import MoviePilotTool
    from pydantic import BaseModel, Field
    _HAS_AGENT_TOOLS = True
except ImportError:  # pragma: no cover
    _HAS_AGENT_TOOLS = False


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
DEFAULT_LABEL = "音乐,musicdownloader"
DEFAULT_CATEGORY = "音乐"
# 与 MoviePilot 内置 Agent 工具共用的搜索缓存文件（SearchChain.__result_temp_file）
SEARCH_RESULT_CACHE_FILE = "__search_result__"
REF_PATTERN = re.compile(r"^[0-9a-f]{7}:\d+$")


def _build_ref(torrent: TorrentInfo) -> str:
    """生成官方同款 hash:id 短引用（sha1(enclosure)[:7]）"""
    return HashUtils.sha1(torrent.enclosure or "")[:7]


def _make_auth_check(plugin: "MusicDownloader"):
    """构造插件 API 鉴权依赖：
    - 配置了 webhook_token 时：X-Music-Token 匹配 或 系统 X-API-KEY/apikey 通过 即可；
    - 未配置 webhook_token 时：仅系统 X-API-KEY/apikey 通过（保持默认 apikey 鉴权）。
    """
    async def _check(
        x_apikey: Optional[str] = Header(default=None, alias="X-API-KEY"),
        x_music_token: Optional[str] = Header(default=None, alias="X-Music-Token"),
        apikey: Optional[str] = None,
    ):
        if plugin._webhook_token and x_music_token == plugin._webhook_token:
            return True
        key = (x_apikey or "").strip() or (apikey or "").strip()
        if key and key == settings.API_TOKEN:
            return True
        raise HTTPException(status_code=401, detail="apikey 校验不通过")
    return _check


# --------------------------------------------------------------------------- #
# v2 智能体工具（可选）：让 MoviePilot 内置 Agent / MCP 也能发现音乐下载能力
# --------------------------------------------------------------------------- #
if _HAS_AGENT_TOOLS:

    class MusicSearchInput(BaseModel):
        keyword: Optional[str] = Field(None, description="完整搜索关键词")
        artist: Optional[str] = Field(None, description="艺人名，如：周杰伦")
        album: Optional[str] = Field(None, description="专辑名，如：叶惠美")
        album_aliases: Optional[List[str]] = Field(None, description="专辑别名/英文名列表，如 Capricorn")
        year: Optional[int] = Field(None, description="年份（可选）")
        limit: Optional[int] = Field(10, description="返回条数上限")
        prefer_lossless: Optional[bool] = Field(True, description="无损优先")

    class MusicSearchTool(MoviePilotTool):
        name: str = "music_search"
        description: str = "在所有启用站点搜索并筛查音乐资源（艺人/专辑），返回无损优先的候选列表"
        args_schema: type = MusicSearchInput

        async def run(self, keyword: str = None, artist: str = None,
                      album: str = None, album_aliases: Optional[List[str]] = None,
                      year: int = None, limit: int = 10,
                      prefer_lossless: bool = True, **kwargs) -> str:
            import json
            inst = _get_instance()
            if not inst:
                return "插件未启用"
            data = await inst.do_search(
                keyword=keyword, artist=artist, album=album, year=year,
                limit=limit, prefer_lossless=prefer_lossless,
                album_aliases=album_aliases,
            )
            return json.dumps(data, ensure_ascii=False, indent=2)

    class MusicDownloadInput(BaseModel):
        ref: Optional[str] = Field(None, description="搜索结果引用，如 a1b2c3d:1")
        site_id: Optional[int] = Field(None, description="搜索结果中的站点ID")
        index: Optional[int] = Field(None, description="搜索结果序号（从1开始）")
        magnet: Optional[str] = Field(None, description="磁力链（与 ref/index 二选一）")
        title: Optional[str] = Field(None, description="种子标题（可选）")

    class MusicDownloadTool(MoviePilotTool):
        name: str = "music_download"
        description: str = "把音乐资源加入 MoviePilot 下载器，保存到音乐下载目录"
        args_schema: type = MusicDownloadInput

        async def run(self, ref: str = None, site_id: int = None,
                      index: int = None, magnet: str = None,
                      title: str = None, **kwargs) -> str:
            import json
            inst = _get_instance()
            if not inst:
                return "插件未启用"
            result = await inst.do_download(
                ref=ref, site_id=site_id, index=index,
                magnet=magnet, title=title)
            return json.dumps(result, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 主插件
# --------------------------------------------------------------------------- #
class MusicDownloader(_PluginBase):
    """音乐下载插件：全站点关键词搜索 -> 音乐/影视筛查 -> 下载器 -> 音乐目录 -> 通知"""

    plugin_name = "音乐下载"
    plugin_desc = "在所有启用站点搜索并筛查音乐资源，用MoviePilot下载器下载（不刮削/不整理）"
    plugin_version = "0.4.3"
    plugin_author = "zyk1172"
    plugin_icon = "https://raw.githubusercontent.com/zyk1172/movipnote-music-downloader/main/plugins.v2/musicdownloader/icon.png"

    # 运行时状态
    _enabled: bool = False
    _music_dir: str = ""
    _dir_valid: bool = False
    _dir_error: str = ""
    _downloader: str = ""
    _category: str = DEFAULT_CATEGORY
    _label: str = DEFAULT_LABEL
    _sites_mode: str = "all"          # all / include / exclude
    _sites_include: List[int] = []
    _sites_exclude: List[int] = []
    _require_music: bool = True
    _prefer_lossless: bool = True
    _min_seeders: int = 0
    _max_size_gb: float = 0.0
    _exclude_keywords: List[str] = []
    _show_uncertain: bool = True
    _fallback_artist: bool = True
    _notify_url: str = ""
    _notify_token: str = ""
    _notify_enabled: bool = False
    _notify_on_search: bool = True
    _webhook_token: str = ""

    # 最近一次搜索摘要（仅内存，用于响应与日志）
    _last_kw: str = ""
    _last_dropped: int = 0

    def init_plugin(self, config: dict = None):
        """应用/刷新配置"""
        self.stop_service()
        if not config:
            return
        self._enabled = bool(config.get("enabled", False))
        self._music_dir = str(config.get("music_dir") or "").strip()
        self._downloader = str(config.get("downloader") or "").strip()
        self._category = str(config.get("torrent_category") or DEFAULT_CATEGORY).strip()
        self._label = str(config.get("label") or DEFAULT_LABEL).strip()
        self._sites_mode = str(config.get("sites_mode") or "all").strip()
        self._sites_include = [int(s) for s in (config.get("sites_include") or []) if str(s).isdigit()]
        self._sites_exclude = [int(s) for s in (config.get("sites_exclude") or []) if str(s).isdigit()]
        self._require_music = bool(config.get("require_music", True))
        self._prefer_lossless = bool(config.get("prefer_lossless", True))
        try:
            self._min_seeders = max(0, int(config.get("min_seeders") or 0))
        except (TypeError, ValueError):
            self._min_seeders = 0
        try:
            self._max_size_gb = max(0.0, float(config.get("max_size_gb") or 0))
        except (TypeError, ValueError):
            self._max_size_gb = 0.0
        self._exclude_keywords = [
            k.strip().lower() for k in str(config.get("exclude_keywords") or "").split(",") if k.strip()
        ]
        self._show_uncertain = bool(config.get("show_uncertain", True))
        self._fallback_artist = bool(config.get("fallback_artist", True))
        self._notify_enabled = bool(config.get("notify_enabled", False))
        self._notify_on_search = bool(config.get("notify_on_search", True))
        self._notify_url = str(config.get("notify_url") or "").strip()
        self._notify_token = str(config.get("notify_token") or "").strip()
        self._webhook_token = str(config.get("webhook_token") or "").strip()

        # 校验音乐下载目录（MoviePilot 已配置下载目录或其子目录）
        self._refresh_dir_status()
        if not self._dir_valid:
            logger.error(f"【{self.plugin_name}】音乐下载目录校验失败: {self._dir_error}")

        self.update_config({
            "enabled": self._enabled, "music_dir": self._music_dir,
            "downloader": self._downloader, "torrent_category": self._category,
            "label": self._label, "sites_mode": self._sites_mode,
            "sites_include": self._sites_include, "sites_exclude": self._sites_exclude,
            "require_music": self._require_music, "prefer_lossless": self._prefer_lossless,
            "min_seeders": self._min_seeders, "max_size_gb": self._max_size_gb,
            "exclude_keywords": ",".join(self._exclude_keywords),
            "show_uncertain": self._show_uncertain,
            "fallback_artist": self._fallback_artist,
            "notify_enabled": self._notify_enabled, "notify_on_search": self._notify_on_search,
            "notify_url": self._notify_url,
            "notify_token": self._notify_token, "webhook_token": self._webhook_token,
        })

        if self._enabled:
            logger.info(
                f"【{self.plugin_name}】已启用：目录={'通过' if self._dir_valid else '失败'}，"
                f"搜索站点={len(self._resolve_site_ids())}，筛查=音乐/影视判别+无损优先"
            )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """REST API，挂载于 /api/v1/plugin/MusicDownloader/*。

        鉴权：配置了「音乐APP调用Token(webhook_token)」时，允许 X-Music-Token 调用；
        同时始终兼容系统 X-API-KEY / ?apikey=。
        """
        api_list = [
            {"path": "/search", "endpoint": self.api_search, "methods": ["POST"],
             "summary": "搜索音乐资源", "description": "全站点关键词搜索+音乐/影视筛查"},
            {"path": "/download", "endpoint": self.api_download, "methods": ["POST"],
             "summary": "下载音乐资源", "description": "按 hash:id 引用/序号加入下载器，保存到音乐目录"},
            {"path": "/magnet", "endpoint": self.api_magnet, "methods": ["POST"],
             "summary": "磁力下载", "description": "直接提交磁力链下载到音乐目录"},
            {"path": "/tasks", "endpoint": self.api_tasks, "methods": ["GET"],
             "summary": "查询下载任务"},
            {"path": "/sites", "endpoint": self.api_sites, "methods": ["GET"],
             "summary": "查询生效的搜索站点"},
            {"path": "/notify/test", "endpoint": self.api_notify_test, "methods": ["POST"],
             "summary": "测试通知"},
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "summary": "插件状态"},
            {"path": "/on_complete", "endpoint": self.api_on_complete, "methods": ["GET"],
             "summary": "下载完成回调",
             "description": "qBittorrent外部程序回调：?hash=%I&name=%N"},
        ]
        auth_dep = Depends(_make_auth_check(self))
        for item in api_list:
            item["allow_anonymous"] = True
            item["dependencies"] = [auth_dep]
        return api_list

    # ------------------------------------------------------------------ #
    # 搜索 + 筛查（核心）
    # ------------------------------------------------------------------ #
    async def _search_and_screen(self, kw: str, cfg: dict,
                                  site_ids: List[int],
                                  artist: str = None, album: str = None,
                                  keyword: str = None,
                                  album_aliases: Optional[List[str]] = None) -> dict:
        """单次搜索 + 写共享缓存 + 筛查 + 相关度排序"""
        contexts = await SearchChain().async_search_by_title(
            title=kw, sites=site_ids, page=0, cache_local=False
        ) or []
        # 写入共享缓存（后续 hash:id 引用基于该缓存解析）
        await SearchChain().async_save_cache(contexts, SEARCH_RESULT_CACHE_FILE)

        # 转成 screener 可处理的 dict，并记录在完整列表中的 1-based 序号
        items: List[Dict[str, Any]] = []
        for pos, ctx in enumerate(contexts, start=1):
            t = ctx.torrent_info
            if not t:
                continue
            ref = f"{_build_ref(t)}:{pos}"
            items.append({
                "index": pos,
                "ref": ref,
                "site_id": t.site,
                "site_name": t.site_name,
                "title": t.title,
                "description": t.description,
                "category": t.category,
                "labels": t.labels or [],
                "size": t.size,
                "seeders": t.seeders,
                "grabs": t.grabs,
                "pubdate": t.pubdate,
                "enclosure": t.enclosure,
            })
        return screen(items, cfg, artist=artist, album=album,
                      keyword=keyword, album_aliases=album_aliases)

    async def do_search(self, keyword: str = None, artist: str = None,
                        album: str = None, year: int = None,
                        limit: int = 10,
                        prefer_lossless: Optional[bool] = None,
                        min_seeders: Optional[int] = None,
                        album_aliases: Optional[List[str]] = None) -> dict:
        """全站点关键词搜索 -> 音乐/影视判别 -> 无损优先+相关度 -> 排序

        结果写入 MoviePilot 共享缓存（__search_result__），与官方 Agent 工具
        get_search_results / add_download_tasks 互通；每项带 hash:id 引用。
        单曲关键词无结果时，可自动退回「艺人名」再搜一轮（fallback_artist）。
        """
        kw = build_keyword(keyword=keyword, artist=artist, album=album, year=year)
        if not kw:
            return {"keyword": "", "total": 0, "results": []}
        site_ids = self._resolve_site_ids()
        if not site_ids:
            logger.warn("【%s】没有可搜索的站点", self.plugin_name)
            return {"keyword": kw, "total": 0, "results": []}

        cfg = {
            "require_music": self._require_music,
            "prefer_lossless": self._prefer_lossless if prefer_lossless is None else bool(prefer_lossless),
            "min_seeders": self._min_seeders if min_seeders is None else max(0, int(min_seeders)),
            "max_size_gb": self._max_size_gb,
            "exclude_keywords": self._exclude_keywords,
            "show_uncertain": self._show_uncertain,
        }
        result = await self._search_and_screen(
            kw, cfg, site_ids, artist=artist, album=album, keyword=keyword,
            album_aliases=album_aliases)

        # 单曲/专辑关键词无结果 -> 退回艺人名搜索（PT 站常按专辑/艺人建种）
        artist_only = build_keyword(keyword=artist)
        if (not result["results"] and self._fallback_artist
                and artist and artist_only and artist_only != kw):
            logger.info("【%s】关键词 %s 无结果，退回艺人搜索：%s",
                        self.plugin_name, kw, artist_only)
            result = await self._search_and_screen(
                artist_only, cfg, site_ids, artist=artist, album=album,
                keyword=keyword, album_aliases=album_aliases)

        self._last_kw = kw
        self._last_dropped = result["dropped_video"] + result["dropped_uncertain"]

        results = []
        for item in result["results"][:limit]:
            results.append({
                "index": item["index"],
                "ref": item["ref"],
                "site_id": item["site_id"],
                "site_name": item["site_name"],
                "title": item["title"],
                "category": item["category"],
                "music": item["music"],
                "confidence": item["confidence"],
                "audio_format": item["audio_format"],
                "quality": item["quality"],
                "quality_label": item["quality_label"],
                "relevance": item["relevance"],
                "album_matched": item.get("album_matched", False),
                "size_text": fmt_size(item.get("size")),
                "seeders": item["seeders"],
                "grabs": item["grabs"],
                "pubdate": item["pubdate"],
                "enclosure": item["enclosure"],
            })

        return {
            "keyword": kw,
            "searched_sites": [s.get("name") for s in self.api_site_list()],
            "total": len(results),
            "album_matched_any": any(r.get("album_matched") for r in results),
            "dropped_video": result["dropped_video"],
            "dropped_uncertain": result["dropped_uncertain"],
            "results": results,
        }

    async def _resolve_ref(self, ref: str) -> Optional[TorrentInfo]:
        """按官方 hash:id 引用从共享缓存解析种子信息（含 hash 校验）"""
        if not ref or not REF_PATTERN.match(ref):
            return None
        ref_hash, ref_index = ref.split(":", 1)
        try:
            index = int(ref_index)
        except (TypeError, ValueError):
            return None
        if index < 1:
            return None
        try:
            results = await SearchChain().async_last_search_results() or []
        except Exception as exc:
            logger.error(f"【{self.plugin_name}】读取搜索缓存失败: {exc}")
            return None
        if index > len(results):
            return None
        context = results[index - 1]
        if not context.torrent_info:
            return None
        if _build_ref(context.torrent_info) != ref_hash:
            return None
        return context.torrent_info

    async def _resolve_by_index(self, site_id: Optional[int], index: int) -> Optional[TorrentInfo]:
        """按 1-based 序号从共享缓存解析（site_id 可作二次校验）"""
        if index < 1:
            return None
        results = await SearchChain().async_last_search_results() or []
        if index > len(results):
            return None
        t = results[index - 1].torrent_info
        if not t:
            return None
        if site_id is not None and t.site != int(site_id):
            return None
        return t

    async def do_download(self, ref: str = None, site_id: int = None,
                          index: int = None, magnet: str = None,
                          title: str = None, torrent_obj: dict = None) -> dict:
        """统一下载入口：ref(hash:id) / site_id+index / torrent 对象 / magnet"""
        self._refresh_dir_status()
        if not self._dir_valid:
            return {"success": False,
                    "message": f"音乐下载目录未通过校验: {self._dir_error}"}

        torrent: Optional[TorrentInfo] = None
        if ref:
            torrent = await self._resolve_ref(ref)
            if not torrent:
                return {"success": False, "message": "搜索结果引用已失效，请重新搜索"}
        elif index is not None:
            torrent = await self._resolve_by_index(site_id, int(index))
            if not torrent:
                return {"success": False, "message": "搜索结果序号已失效，请重新搜索"}
        elif torrent_obj:
            torrent = TorrentInfo(
                title=str(torrent_obj.get("title") or ""),
                enclosure=str(torrent_obj.get("enclosure") or ""),
                site=torrent_obj.get("site_id"),
                site_name=torrent_obj.get("site_name") or "",
                size=torrent_obj.get("size"),
                seeders=torrent_obj.get("seeders"),
                site_cookie=str(torrent_obj.get("site_cookie") or ""),
            )
            if not torrent.title or not torrent.enclosure:
                return {"success": False, "message": "缺少 title/enclosure"}

        if torrent:
            # 音乐无影视媒体信息：构造最小 MediaInfo（type=未知），避免 download_single
            # 在登记下载历史等环节因 mediainfo=None 崩溃
            media = MediaInfo()
            media.title = torrent.title or ""
            media.type = MediaType.UNKNOWN
            try:
                did, err = await asyncio.to_thread(
                    DownloadChain().download_single,
                    context=Context(meta_info=MetaInfo(title=torrent.title),
                                    media_info=media,
                                    torrent_info=torrent),
                    save_path=self._music_dir,
                    downloader=self._downloader or None,
                    label=self._label,
                    username=self.plugin_name,
                    return_detail=True,
                )
            except Exception as exc:
                logger.error(f"【{self.plugin_name}】下载异常: {exc}",
                             exc_info=True)
                self._push_result("download_failed", {
                    "title": torrent.title or title or "", "reason": str(exc),
                }, f"下载失败：{torrent.title or title or ''}", f"异常：{exc}")
                return {"success": False,
                        "message": f"下载异常 {type(exc).__name__}: {exc}"}
            if not did:
                self._push_result("download_failed", {
                    "title": torrent.title or title or "", "reason": err,
                }, f"下载失败：{torrent.title or title or ''}", f"原因：{err}")
                return {"success": False, "message": f"加入下载失败: {err}"}
            self._record(did=did, title=torrent.title or title or "",
                         site=torrent.site_name or "", save_path=self._music_dir,
                         status="downloading")
            self._push_result("download_added", {
                "hash": did,
                "title": torrent.title or title or "音乐下载",
                "site": torrent.site_name or "",
                "save_path": self._music_dir,
                "status": "downloading",
            }, f"已加入下载：{torrent.title or title or '音乐下载'}",
               f"{torrent.site_name or '-'} | 保存到 {self._music_dir}")
            return {"success": True, "data": {"hash": did,
                                              "save_path": self._music_dir,
                                              "label": self._label,
                                              "status": "downloading"}}

        # magnet 直链
        if magnet and str(magnet).startswith("magnet:"):
            result = await asyncio.to_thread(
                DownloadChain().download,
                content=str(magnet),
                download_dir=Path(self._music_dir),
                cookie=None,
                category=self._category,
                label=self._label,
                downloader=self._downloader or None,
            )
            if not result:
                return {"success": False, "message": "未找到可用下载器"}
            _, did, _, err = result
            if not did:
                self._push_result("download_failed", {
                    "title": title or "磁力下载", "reason": err,
                }, f"下载失败：{title or '磁力下载'}", f"原因：{err}")
                return {"success": False, "message": f"加入下载失败: {err}"}
            self._record(did=did, title=title or "磁力下载", site="magnet",
                         save_path=self._music_dir, status="downloading")
            self._push_result("download_added", {
                "hash": did, "title": title or "磁力下载", "site": "magnet",
                "save_path": self._music_dir, "status": "downloading",
            }, f"已加入下载：{title or '磁力下载'}", f"保存到 {self._music_dir}")
            return {"success": True, "data": {"hash": did,
                                              "save_path": self._music_dir}}

        return {"success": False, "message": "缺少 ref/index/torrent/magnet 任一参数"}

    # ------------------------------------------------------------------ #
    # API 实现
    # ------------------------------------------------------------------ #
    async def api_search(self, payload: dict = Body(default_factory=dict)) -> dict:
        data = await self.do_search(
            keyword=payload.get("keyword"), artist=payload.get("artist"),
            album=payload.get("album"), year=payload.get("year"),
            limit=payload.get("limit") or 10,
            prefer_lossless=payload.get("prefer_lossless"),
            min_seeders=payload.get("min_seeders"),
            album_aliases=payload.get("album_aliases"),
        )
        if self._notify_on_search:
            if data.get("results"):
                self._push_result("search_ready", {
                    "keyword": data.get("keyword"),
                    "total": len(data["results"]),
                    "album_matched_any": data.get("album_matched_any"),
                    "top": [{"ref": r["ref"], "title": r["title"],
                             "site_name": r["site_name"],
                             "quality_label": r["quality_label"],
                             "relevance": r["relevance"],
                             "album_matched": r["album_matched"]}
                            for r in data["results"][:5]],
                }, f"搜索音乐：{data.get('keyword')}",
                   f"筛选出 {len(data['results'])} 条音乐资源")
            else:
                self._push_result("no_resource", {
                    "keyword": data.get("keyword"),
                    "searched_sites": data.get("searched_sites"),
                }, f"没有找到 {data.get('keyword')} 的音乐资源",
                   "可尝试更换关键词，或启用「无结果退艺人搜索」")
        return {"success": True, "data": data}

    async def api_download(self, payload: dict = Body(default_factory=dict)) -> dict:
        result = await self.do_download(
            ref=payload.get("ref"), site_id=payload.get("site_id"),
            index=payload.get("index"), magnet=None,
            title=payload.get("title"), torrent_obj=payload.get("torrent"),
        )
        return result

    async def api_magnet(self, payload: dict = Body(default_factory=dict)) -> dict:
        return await self.do_download(
            magnet=payload.get("magnet"), title=payload.get("title"))

    async def api_tasks(self, status: Optional[str] = None) -> dict:
        """查询任务：合并插件历史 + 下载器实时状态；检测到完成/暂停时更新并推送结果"""
        history = self.get_data("downloads") or []
        live: Dict[str, dict] = {}
        try:
            torrents = DownloadChain().list_torrents(include_all_tags=True) or []
            live = {t.hash: self._torrent_to_dict(t)
                    for t in torrents if t.hash}
        except Exception as err:
            logger.error(f"【{self.plugin_name}】查询下载器任务失败: {err}")

        changed = False
        for item in history:
            if item.get("status") != "downloading":
                continue
            lt = live.get(item["hash"])
            if not lt:
                continue
            state = lt.get("state")
            if state == "completed":
                item["status"] = "completed"
                item["finish_time"] = datetime.now().isoformat()
                changed = True
                self._push_result("download_completed", {
                    "hash": item["hash"], "title": item.get("title"),
                    "save_path": item.get("save_path"),
                }, f"下载成功：{item.get('title')}", f"保存到 {item.get('save_path')}")
            elif state == "paused":
                item["status"] = "paused"
                changed = True
        if changed:
            self.save_data("downloads", history)

        tasks = []
        for item in history:
            lt = live.get(item["hash"]) or {}
            tasks.append({
                "hash": item["hash"],
                "title": item.get("title"),
                "site": item.get("site"),
                "status": item["status"],
                "state": lt.get("state"),
                "progress": lt.get("progress"),
                "dlspeed": lt.get("dlspeed"),
                "save_path": lt.get("save_path") or item.get("save_path"),
            })
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        return {"success": True, "data": {"tasks": tasks}}

    async def api_on_complete(self, hash: str = None, name: str = None) -> dict:
        """下载完成回调（qBittorrent 外部程序）：GET ?hash=%I&name=%N"""
        if not hash:
            return {"success": False, "message": "缺少 hash 参数"}
        history = self.get_data("downloads") or []
        changed = False
        for item in history:
            if item.get("hash") == hash and item.get("status") == "downloading":
                item["status"] = "completed"
                item["finish_time"] = datetime.now().isoformat()
                changed = True
                self._push_result("download_completed", {
                    "hash": item["hash"], "title": item.get("title") or name,
                    "save_path": item.get("save_path"),
                }, f"下载成功：{item.get('title') or name}",
                   f"保存到 {item.get('save_path')}")
        if changed:
            self.save_data("downloads", history)
        return {"success": True, "message": "ok"}

    async def api_sites(self) -> dict:
        return {"success": True, "data": {
            "mode": self._sites_mode,
            "sites": self.api_site_list(),
        }}

    def api_site_list(self) -> List[dict]:
        site_ids = self._resolve_site_ids()
        return [{"id": s.id, "name": s.name}
                for s in SiteOper().list() or [] if s.id in site_ids]

    async def api_notify_test(self, payload: dict = Body(default_factory=dict)) -> dict:
        ok = self._push_result("notify_test", {"message": "这是一条测试通知"},
                               "音乐下载插件测试", "这是一条测试通知")
        return {"success": bool(ok), "message": "通知已发送" if ok else "通知发送失败"}

    async def api_status(self) -> dict:
        self._refresh_dir_status()
        return {"success": True, "data": {
            "enabled": self._enabled,
            "music_dir": self._music_dir,
            "dir_valid": self._dir_valid,
            "dir_error": self._dir_error,
            "sites_mode": self._sites_mode,
            "sites": self.api_site_list(),
            "require_music": self._require_music,
            "prefer_lossless": self._prefer_lossless,
            "min_seeders": self._min_seeders,
            "max_size_gb": self._max_size_gb,
            "exclude_keywords": self._exclude_keywords,
            "show_uncertain": self._show_uncertain,
            "fallback_artist": self._fallback_artist,
            "notify_enabled": self._notify_enabled,
            "notify_on_search": self._notify_on_search,
            "notify_url": self._notify_url,
        }}

    def _refresh_dir_status(self):
        """每次请求时动态校验音乐下载目录（目录配置可能后加/修改，不依赖保存插件配置时的快照）"""
        self._dir_valid = False
        self._dir_error = ""
        if not self._music_dir:
            self._dir_error = "未配置音乐下载目录"
            return
        try:
            validate_download_save_path(self._music_dir)
            self._dir_valid = True
        except ValueError as err:
            self._dir_error = str(err)

    def _resolve_site_ids(self) -> List[int]:
        """生效的搜索站点：全部启用索引站点 或 include/exclude 交集"""
        enabled = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
        if self._sites_mode == "include":
            return [s for s in enabled if s in self._sites_include]
        if self._sites_mode == "exclude":
            return [s for s in enabled if s not in self._sites_exclude]
        return enabled

    # ------------------------------------------------------------------ #
    # Agent 工具（v2+）
    # ------------------------------------------------------------------ #
    def get_agent_tools(self) -> List[type]:
        if _HAS_AGENT_TOOLS:
            return [MusicSearchTool, MusicDownloadTool]
        return []

    # ------------------------------------------------------------------ #
    # 后台服务：按需使用，不做周期轮询
    # ------------------------------------------------------------------ #
    def get_service(self) -> List[Dict[str, Any]]:
        """按需下载，不注册周期检测服务。

        下载完成通知如需开启，后续可改为「下载器完成回调/Webhook」驱动，
        避免定时轮询 DownloaderTorrent 造成日志噪音。
        """
        return []

    # ------------------------------------------------------------------ #
    # 结果推送（结构化状态 -> Agent/音乐APP）
    #   类型：no_resource / search_ready / download_added / download_completed / download_failed / notify_test
    # ------------------------------------------------------------------ #
    @staticmethod
    def _torrent_to_dict(t) -> dict:
        """DownloaderTorrent 是 Pydantic 模型（属性访问），统一转 dict"""
        if hasattr(t, "model_dump"):
            try:
                return t.model_dump()
            except Exception:
                pass
        keys = ("hash", "title", "name", "state", "progress", "save_path",
                "tags", "category", "downloader", "size", "dlspeed", "upspeed")
        return {k: getattr(t, k, None) for k in keys}

    def _push_result(self, rtype: str, payload: dict,
                     title: str, text: str) -> bool:
        """推送结构化结果给 Agent/音乐APP（Webhook JSON）+ 原生渠道"""
        sent = False
        body = {"event": "music.result", "type": rtype, "payload": payload or {}}
        if self._notify_enabled and self._notify_url:
            try:
                import requests
                headers = {}
                if self._notify_token:
                    headers["Authorization"] = f"Bearer {self._notify_token}"
                resp = requests.post(self._notify_url, json=body,
                                     headers=headers, timeout=10)
                sent = resp.ok
            except Exception as err:
                logger.error(f"【{self.plugin_name}】结果推送失败: {err}")
        try:
            self.post_message(mtype=NotificationType.Download,
                              title=title, text=text)
        except Exception:
            pass
        return sent

    def _record(self, did: str, title: str, site: str,
                save_path: str, status: str):
        history = self.get_data("downloads") or []
        history = [h for h in history if h.get("hash") != did]
        history.append({
            "hash": did, "title": title, "site": site,
            "save_path": save_path, "status": status,
            "create_time": datetime.now().isoformat(),
        })
        self.save_data("downloads", history[-200:])  # 保留最近 200 条

    # ------------------------------------------------------------------ #
    # 配置页（Vuetify）
    # ------------------------------------------------------------------ #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        all_sites = [{"title": s.name, "value": s.id} for s in SiteOper().list() or []]
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "notify_enabled", "label": "启用通知"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 8},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "music_dir", "label": "音乐下载目录",
                                                "hint": "MoviePilot已配置下载目录或其子目录，如 /downloads/Music",
                                                "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "downloader", "label": "下载器（留空=默认）"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VSelect",
                                      "props": {"model": "sites_mode", "label": "搜索站点范围",
                                                "items": [
                                                    {"title": "全部启用站点（默认）", "value": "all"},
                                                    {"title": "仅以下站点", "value": "include"},
                                                    {"title": "排除以下站点", "value": "exclude"},
                                                ]}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 8},
                         "content": [{"component": "VSelect",
                                      "props": {"model": "sites_include", "multiple": True,
                                                "chips": True, "label": "仅搜索这些站点",
                                                "items": all_sites}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 12},
                         "content": [{"component": "VSelect",
                                      "props": {"model": "sites_exclude", "multiple": True,
                                                "chips": True, "label": "排除这些站点",
                                                "items": all_sites}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 3},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "require_music", "label": "仅保留音乐"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "prefer_lossless", "label": "无损优先"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "show_uncertain", "label": "展示不确定项"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 3},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "fallback_artist", "label": "无结果退艺人搜索",
                                                "hint": "单曲搜不到时按艺人名再搜一轮",
                                                "persistent-hint": True}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "min_seeders", "label": "最低做种数"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "max_size_gb", "label": "单资源上限(GB)"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "exclude_keywords", "label": "排除关键词",
                                                "hint": "逗号分隔，如 MV,演唱会,合集",
                                                "persistent-hint": True}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "notify_url", "label": "音乐APP Webhook URL",
                                                "hint": "结果推送地址（POST JSON）",
                                                "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "notify_token", "label": "Webhook Token（可选）"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4},
                         "content": [{"component": "VSwitch",
                                      "props": {"model": "notify_on_search", "label": "搜索后推送结果",
                                                "hint": "把 没有资源/候选就绪 状态推给Agent",
                                                "persistent-hint": True}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "webhook_token", "label": "音乐APP调用Token",
                                                "hint": "X-Music-Token；留空则仅允许系统API_TOKEN",
                                                "persistent-hint": True}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6},
                         "content": [{"component": "VTextField",
                                      "props": {"model": "torrent_category", "label": "下载器分类",
                                                "hint": "默认 音乐"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": self._enabled, "music_dir": self._music_dir,
            "downloader": self._downloader, "torrent_category": self._category,
            "label": self._label, "sites_mode": self._sites_mode,
            "sites_include": self._sites_include, "sites_exclude": self._sites_exclude,
            "require_music": self._require_music, "prefer_lossless": self._prefer_lossless,
            "min_seeders": self._min_seeders, "max_size_gb": self._max_size_gb,
            "exclude_keywords": ",".join(self._exclude_keywords),
            "show_uncertain": self._show_uncertain,
            "fallback_artist": self._fallback_artist,
            "notify_enabled": self._notify_enabled, "notify_on_search": self._notify_on_search,
            "notify_url": self._notify_url,
            "notify_token": self._notify_token, "webhook_token": self._webhook_token,
        }

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        """停止插件（框架调用）"""
        pass


# --------------------------------------------------------------------------- #
# 实例访问辅助（供 Agent 工具使用）
# --------------------------------------------------------------------------- #
def _get_instance() -> Optional[MusicDownloader]:
    try:
        from app.core.plugin import PluginManager
        # _plugins 以插件ID(=类名)为键保存插件实例
        plugin = PluginManager().plugins.get(MusicDownloader.__name__)
        return plugin if isinstance(plugin, MusicDownloader) else None
    except Exception:
        return None
