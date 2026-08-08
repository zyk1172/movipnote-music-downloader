"""
screener 回归测试（独立运行，不依赖 MoviePilot）
用法：
    cd plugin && python -m pytest tests -v
或：
    python tests/test_screener.py
"""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def load_screener():
    """绕过 MusicDownloader/__init__.py，直接加载纯筛查模块"""
    path = HERE.parent / "plugins.v2" / "musicdownloader" / "screener.py"
    spec = importlib.util.spec_from_file_location("screener", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


screener = load_screener()


def load_fixtures() -> list:
    items = []
    for f in sorted(FIXTURES.glob("*.json")):
        items.extend(json.loads(f.read_text(encoding="utf-8")))
    return items


def expected_bool(item: dict):
    return {"music": True, "video": False, "uncertain": None}[item["expected"]]


def test_each_fixture_classifies_correctly():
    items = load_fixtures()
    assert items, "夹具为空"
    failures = []
    for item in items:
        got, confidence, audio_format, quality = screener.classify(item)
        exp = expected_bool(item)
        if got != exp:
            failures.append((item, got, exp, confidence, audio_format, quality))
    assert not failures, "\n".join(
        f"标题: {it.get('title')!r} | 期望={e} 实际={g} ({c}/{a}/{q})"
        for it, g, e, c, a, q in failures
    )


def test_evaluate_metrics_above_threshold():
    items = load_fixtures()
    metrics = screener.evaluate(items)
    print("\n===== 筛查准确率 =====")
    print(f"音乐   : {metrics['music'][0]}/{metrics['music'][1]} = {metrics['music'][2]:.2%}")
    print(f"影视   : {metrics['video'][0]}/{metrics['video'][1]} = {metrics['video'][2]:.2%}")
    print(f"不确定 : {metrics['uncertain'][0]}/{metrics['uncertain'][1]} = {metrics['uncertain'][2]:.2%}")
    print(f"总准确率: {metrics['correct']}/{metrics['total']} = {metrics['overall_accuracy']:.2%}")
    assert metrics["music"][2] >= 0.9, "音乐识别准确率 < 90%"
    assert metrics["video"][2] >= 0.9, "影视识别准确率 < 90%"


def test_screen_orders_by_quality_when_lossless_preferred():
    items = [
        {"title": "A FLAC", "category": "音乐", "size": 1, "seeders": 10, "grabs": 1},
        {"title": "B FLAC 24bit 96kHz", "category": "音乐", "size": 1, "seeders": 5, "grabs": 1},
        {"title": "C MP3", "category": "音乐", "size": 1, "seeders": 99, "grabs": 1},
    ]
    out = screener.screen(items, {"require_music": True, "prefer_lossless": True})
    qualities = [r["quality"] for r in out["results"]]
    assert qualities == sorted(qualities, reverse=True), "无损优先排序失败"
    assert out["results"][0]["title"] == "B FLAC 24bit 96kHz"
    assert out["results"][-1]["title"] == "C MP3"


def test_screen_filters_video_and_excluded():
    items = [
        {"title": "盗梦空间 2010 1080p", "category": "电影", "seeders": 9},
        {"title": "周杰伦 - 叶惠美 FLAC", "category": "音乐", "seeders": 9},
        {"title": "周杰伦 演唱会 1080p", "category": "演唱会", "seeders": 9},
    ]
    out = screener.screen(items, {"require_music": True, "prefer_lossless": True,
                                  "exclude_keywords": ["演唱会"]})
    titles = [r["title"] for r in out["results"]]
    assert titles == ["周杰伦 - 叶惠美 FLAC"], f"筛选结果异常: {titles}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_cjk_boundary_formats():
    """中文紧贴格式（FLAC分轨/WAV整轨）应正确识别为无损"""
    r = screener.classify({"title": "周杰伦 - 叶惠美 FLAC分轨", "category": "音乐"})
    assert r[0] is True and r[2] == "FLAC" and r[3] == 90, r
    r = screener.classify({"title": "陈奕迅 - 富士山下 WAV整轨", "category": "音乐"})
    assert r[0] is True and r[2] == "WAV" and r[3] == 90, r


def test_high_spec_variant_24_96():
    r = screener.classify({"title": "Taylor Swift - Red 24bit/96kHz FLAC", "category": "音乐"})
    assert r[0] is True and r[3] == 100, r


def test_relevance_ranking():
    items = [
        {"title": "Various Artists - 合辑 FLAC", "category": "音乐",
         "size": 1, "seeders": 99, "grabs": 1},
        {"title": "周杰伦 - 叶惠美 FLAC", "category": "音乐",
         "size": 1, "seeders": 5, "grabs": 1},
    ]
    out = screener.screen(items, {"require_music": True, "prefer_lossless": True},
                          artist="周杰伦", album="叶惠美")
    assert out["results"][0]["title"].startswith("周杰伦"), "艺人/专辑相关度应优先"
    assert out["results"][0]["relevance"] > out["results"][1]["relevance"]


def test_album_aliases_and_matched():
    """中文专辑名 + 英文别名：别名命中应计入 album_matched/relevance"""
    items = [
        {"title": "Jay Chou - Capricorn 2008 - FLAC分轨", "category": "音乐",
         "size": 1, "seeders": 5},
        {"title": "Jay Chou - Fantasy 2001 - FLAC分轨", "category": "音乐",
         "size": 1, "seeders": 99},
    ]
    out = screener.screen(items, {"require_music": True, "prefer_lossless": True},
                          artist="Jay Chou", album="魔杰座",
                          album_aliases=["Capricorn"])
    assert out["results"][0]["title"].startswith("Jay Chou - Capricorn"), "别名命中应优先"
    assert out["results"][0]["album_matched"] is True
    assert out["results"][0]["relevance"] >= 70
    assert out["results"][1]["album_matched"] is False
    assert out["results"][1]["relevance"] < 70  # 仅艺人命中(30)
