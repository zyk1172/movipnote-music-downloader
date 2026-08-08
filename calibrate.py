#!/usr/bin/env python3
"""
calibrate.py —— MusicDownloader 筛查引擎校准工具（独立运行，不依赖 MoviePilot）

两种模式：
  1) offline（默认）：对 tests/fixtures 标注样本集跑判别，打印混淆矩阵与准确率；
     音乐/影视准确率低于 --threshold 时以非零退出码失败，便于 CI 回归。
  2) live：请求运行中的 MoviePilot 插件 /search 接口，用真实搜索结果检查判别效果，
     并把原始响应 dump 到 JSON，供后续离线校准。

用法：
  # 离线评估夹具集
  python calibrate.py
  python calibrate.py --threshold 0.9

  # 在线检查真实站点搜索结果
  python calibrate.py --mode live \
      --url http://192.168.1.10:3000 --api-key <API_TOKEN> \
      --query "周杰伦 叶惠美" --query "Adele 30" \
      --dump ./live_dump.json
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_screener():
    """绕过 MusicDownloader/__init__.py，直接加载纯筛查模块"""
    import importlib.util
    path = HERE / "MusicDownloader" / "screener.py"
    spec = importlib.util.spec_from_file_location("screener", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fixtures(fixtures_dir: Path) -> list:
    items = []
    for f in sorted(fixtures_dir.glob("*.json")):
        items.extend(json.loads(f.read_text(encoding="utf-8")))
    return items


def run_offline(screener, fixtures_dir: Path, threshold: float) -> int:
    items = load_fixtures(fixtures_dir)
    if not items:
        print(f"[!] 夹具目录为空: {fixtures_dir}")
        return 2
    metrics = screener.evaluate(items)

    print("===== 筛查准确率（离线夹具） =====")
    print(f"音乐   : {metrics['music'][0]}/{metrics['music'][1]} = {metrics['music'][2]:.2%}")
    print(f"影视   : {metrics['video'][0]}/{metrics['video'][1]} = {metrics['video'][2]:.2%}")
    print(f"不确定 : {metrics['uncertain'][0]}/{metrics['uncertain'][1]} = {metrics['uncertain'][2]:.2%}")
    print(f"总准确率: {metrics['correct']}/{metrics['total']} = {metrics['overall_accuracy']:.2%}")
    print("混淆矩阵（期望行 / 实际列）:")
    m = metrics["matrix"]
    print(f"  {'':10}{'music':>10}{'video':>10}{'uncertain':>10}")
    for row in ("music", "video", "uncertain"):
        print(f"  {row:10}{m[row]['music']:>10}{m[row]['video']:>10}{m[row]['uncertain']:>10}")

    ok = metrics["music"][2] >= threshold and metrics["video"][2] >= threshold
    print(f"\n阈值 {threshold:.0%}：{'通过' if ok else '未通过'}")
    return 0 if ok else 1


def http_post_json(url: str, payload: dict, api_key: str = None, token: str = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Music-Token"] = token
    elif api_key:
        headers["X-API-KEY"] = api_key
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_live(screener, args) -> int:
    base = args.url.rstrip("/")
    search_url = f"{base}/api/v1/plugin/MusicDownloader/search"
    print(f"[*] 请求 {search_url}")
    dump = {"queries": []}
    for q in args.query:
        payload = {"keyword": q, "limit": args.limit}
        try:
            resp = http_post_json(search_url, payload,
                                  api_key=args.api_key, token=args.token)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "ignore")
            print(f"[!] 查询 {q!r} 失败: HTTP {err.code} {body[:300]}")
            dump["queries"].append({"query": q, "error": f"HTTP {err.code}: {body[:300]}"})
            continue
        except Exception as err:
            print(f"[!] 查询 {q!r} 失败: {err}")
            dump["queries"].append({"query": q, "error": str(err)})
            continue

        data = (resp or {}).get("data") or {}
        results = data.get("results") or []
        print(f"\n===== 查询: {q} | 命中 {len(results)} 条 / 丢弃 {data.get('dropped_video')} =====")
        for r in results:
            mark = "MUSIC" if r.get("music") is True else (
                "??" if r.get("music") is None else "VIDEO")
            print(f"  [{mark}][{r.get('quality_label')}] {r.get('site_name')} | "
                  f"{r.get('title')} | {r.get('size_text')}")
        dump["queries"].append({"query": q, "response": data})

    if args.dump:
        Path(args.dump).write_text(json.dumps(dump, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n[*] 已保存原始响应到 {args.dump}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MusicDownloader 筛查引擎校准")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--fixtures", default=str(HERE / "tests" / "fixtures"))
    parser.add_argument("--threshold", type=float, default=0.9, help="音乐/影视准确率阈值(0-1)")
    parser.add_argument("--url", help="MoviePilot 地址，如 http://192.168.1.10:3000")
    parser.add_argument("--api-key", help="X-API-KEY（系统 API_TOKEN）")
    parser.add_argument("--token", help="X-Music-Token（插件级 token，优先于 api-key）")
    parser.add_argument("--query", action="append", default=[], help="搜索关键词，可重复")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dump", help="live 模式下保存原始响应的 JSON 路径")
    args = parser.parse_args()

    screener = load_screener()
    if args.mode == "offline":
        return run_offline(screener, Path(args.fixtures), args.threshold)

    if not args.query:
        print("[!] live 模式需要至少一个 --query")
        return 2
    if not args.url:
        print("[!] live 模式需要 --url")
        return 2
    return run_live(screener, args)


if __name__ == "__main__":
    sys.exit(main())
