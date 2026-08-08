"""
screener.py —— 音乐/影视筛查引擎（纯 Python，无 MoviePilot 依赖）
=================================================================
设计目标：
- 可脱离 MoviePilot 独立运行（pytest / calibrate.py），方便「先测准确率、再上线」；
- 所有特征库集中在文件头部，按实际站点搜索结果迭代校准即可。

输入约定：每条资源为 dict，至少包含 title；可选 description / category / labels / size / seeders / grabs。
输出约定：classify() 返回 (music, confidence, audio_format, quality)。

三分类：
  music=True   命中音频特征且未命中影视特征
  music=False  命中影视特征（含 MV/演唱会等）
  music=None   两者都不命中 -> uncertain（由 show_uncertain 决定是否展示）
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 特征库（校准点）
# --------------------------------------------------------------------------- #
# 音频格式 -> 质量基础分
AUDIO_FORMATS: Dict[str, int] = {
    "flac": 90, "ape": 90, "wav": 90, "alac": 90, "aiff": 90, "tak": 90,
    "dsf": 100, "dff": 100, "dsd": 100, "wv": 90,
    "aac": 60, "m4a": 60, "mp3": 40, "ogg": 40, "opus": 60,
}

# 音频特征（命中任一 -> 倾向 music）
AUDIO_PATTERNS: List[str] = [
    r"\bflac\b", r"\bape\b", r"\bwav\b", r"\balac\b", r"\bdsf\b", r"\bdff\b",
    r"\bdsd\b", r"\btak\b", r"\baiff\b", r"\bwv\b",
    r"24\s?bit", r"96\s?khz", r"192\s?khz", r"hi-?res", r"\bmaster(?:ing)?\b",
    r"无损", r"hifi", r"\bhq\b",
    r"音乐", r"\bmusic\b", r"\balbum\b", r"专辑", r"\bost\b", r"soundtrack",
    r"原声", r"原声带", r"单曲", r"作品集", r"精选集", r"合辑",
]

# 影视特征（命中任一 -> 丢弃）
VIDEO_PATTERNS: List[str] = [
    r"2160p", r"\b4k\b", r"1080p", r"720p", r"\bblu-?ray\b", r"\bremux\b",
    r"\bweb-?dl\b", r"\bwebrip\b", r"\bhdr\b", r"\bdv\b", r"\bhevc\b",
    r"\bx26[45]\b", r"h\.?26[45]\b", r"\buhd\b", r"杜比", r"全景声",
    r"\bs0?1e\d+\b", r"第\s*\d+\s*集", r"\bepisode\b", r"全集",
    r"\bmv\b", r"music video", r"演唱会", r"concert", r"live\s+show",
]

# 站点分类关键词
MUSIC_CATEGORY: List[str] = ["音乐", "Music", "Audio", "原声", "OST", "无损"]
VIDEO_CATEGORY: List[str] = ["电影", "剧集", "电视剧", "综艺", "纪录", "动漫", "体育", "Video"]

# 高规格无损（质量 100）
HIGH_SPEC_PATTERNS: List[str] = [
    r"24\s?bit", r"96\s?khz", r"192\s?khz", r"hi-?res", r"\bds[df]\b", r"\bdsd\b",
]


def norm(text: Optional[str]) -> str:
    return (text or "").lower()


def detect_audio_format(text: Optional[str]) -> Tuple[Optional[str], int]:
    """识别音频格式，返回 (格式, 基础分)"""
    t = norm(text)
    for fmt, score in AUDIO_FORMATS.items():
        if re.search(rf"\b{re.escape(fmt)}\b", t):
            return fmt.upper(), score
    return None, 30


def classify(t: Dict[str, Any]) -> Tuple[Optional[bool], str, str, int]:
    """
    三分类判别
    :return: (music, confidence, audio_format, quality)
      music=True/False/None（None=不确定）
    """
    title_desc = " ".join([
        str(t.get("title") or ""),
        str(t.get("description") or ""),
        " ".join([str(x) for x in (t.get("labels") or [])]),
    ])
    category = str(t.get("category") or "")
    audio_format, base = detect_audio_format(title_desc)
    lower = norm(title_desc)

    hit_audio = bool(audio_format) or any(
        re.search(p, lower) for p in AUDIO_PATTERNS
    ) or any(k in category for k in MUSIC_CATEGORY)
    hit_video = any(
        re.search(p, lower) for p in VIDEO_PATTERNS
    ) or any(k in category for k in VIDEO_CATEGORY)

    if hit_audio and not hit_video:
        quality = base
        if any(re.search(p, lower) for p in HIGH_SPEC_PATTERNS):
            quality = 100
        return True, "high", audio_format or "未知", quality
    if hit_video and not hit_audio:
        return False, "high", audio_format, 0
    if hit_audio and hit_video:
        # 如 "FLAC MV" / 演唱会蓝光：归为 video（可用 exclude_keywords 再排）
        return False, "mid", audio_format, 0
    return None, "uncertain", audio_format, 30


def quality_label(quality: int) -> str:
    return {100: "无损-高规格", 90: "无损", 60: "有损-高质量",
            40: "有损", 30: "未知"}.get(quality, "未知")


def fmt_size(size: Optional[float]) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def build_keyword(keyword: Optional[str] = None, artist: Optional[str] = None,
                  album: Optional[str] = None, year: Optional[int] = None) -> str:
    """关键词构造：keyword > artist+album(+year)"""
    if keyword and str(keyword).strip():
        return str(keyword).strip()
    parts = [p for p in (artist, album) if p and str(p).strip()]
    if not parts:
        return ""
    kw = " ".join(str(p).strip() for p in parts)
    if year:
        kw = f"{kw} {year}"
    return kw


def screen(items: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """
    筛查 + 排序
    :param items: 原始资源列表（dict）
    :param config: {
        require_music: bool, prefer_lossless: bool,
        min_seeders: int, max_size_gb: float,
        exclude_keywords: List[str], show_uncertain: bool,
    }
    :return: {"results": [...], "dropped": int, "total": int}
    """
    require_music = bool(config.get("require_music", True))
    prefer_lossless = bool(config.get("prefer_lossless", True))
    min_seeders = int(config.get("min_seeders") or 0)
    max_size_gb = float(config.get("max_size_gb") or 0)
    exclude_keywords = [str(k).strip().lower() for k in (config.get("exclude_keywords") or []) if str(k).strip()]
    show_uncertain = bool(config.get("show_uncertain", True))

    results: List[Dict[str, Any]] = []
    dropped = 0
    for item in items:
        title_desc = norm(f"{item.get('title') or ''} {item.get('description') or ''}")
        if any(k in title_desc for k in exclude_keywords):
            continue
        size_gb = (float(item.get("size") or 0)) / (1024 ** 3)
        if max_size_gb and size_gb > max_size_gb:
            continue
        if int(item.get("seeders") or 0) < min_seeders:
            continue

        music, confidence, audio_format, quality = classify(item)
        if music is False:
            dropped += 1
            continue
        if music is None and require_music:
            dropped += 1
            continue
        if music is None and not show_uncertain:
            dropped += 1
            continue

        entry = dict(item)
        entry.update({
            "music": music,
            "confidence": confidence,
            "audio_format": audio_format,
            "quality": quality,
            "quality_label": quality_label(quality),
        })
        results.append(entry)

    if prefer_lossless:
        results.sort(key=lambda e: (e["quality"], int(e.get("seeders") or 0),
                                    int(e.get("grabs") or 0)), reverse=True)
    return {"results": results, "dropped": dropped, "total": len(items)}


def evaluate(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    对带 expected 标注的样本集做评估（校准用）
    :param items: 每条含 expected: "music"/"video"/"uncertain"（None 等价）
    :return: 混淆矩阵 + 精确率/召回率
    """
    matrix = {"music": {"music": 0, "video": 0, "uncertain": 0},
              "video": {"music": 0, "video": 0, "uncertain": 0},
              "uncertain": {"music": 0, "video": 0, "uncertain": 0}}
    for item in items:
        expected = item.get("expected")
        if expected == "music":
            exp = True
        elif expected == "video":
            exp = False
        else:
            exp = None
        got, _, _, _ = classify(item)
        got_key = "music" if got is True else ("video" if got is False else "uncertain")
        exp_key = "music" if exp is True else ("video" if exp is False else "uncertain")
        matrix[exp_key][got_key] += 1

    def acc(key: str) -> Tuple[int, int, float]:
        tp = matrix[key][key]
        total = sum(matrix[key].values())
        return tp, total, (tp / total if total else 0.0)

    m_acc = acc("music")
    v_acc = acc("video")
    u_acc = acc("uncertain")
    correct = sum(matrix[k][k] for k in matrix)
    overall = sum(sum(row.values()) for row in matrix.values())
    return {
        "matrix": matrix,
        "music": m_acc, "video": v_acc, "uncertain": u_acc,
        "correct": correct, "total": overall,
        "overall_accuracy": correct / overall if overall else 0.0,
    }
