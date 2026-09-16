"""压缩换模型的可行性验证：弱模型吐 <DIGEST> JSON 还准不准？

压缩只占全部 token 的约 2%，所以「换便宜模型」省不了多少钱 ——
真正的判据是**它会不会把结构化字段吐坏**（promises / affinity 全靠这段 JSON）。
吐坏了承诺账本和好感度结算一起失灵，省那点额度不值。

跑法：../.venv/bin/python probe_digest_model.py
"""
import asyncio
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import config  # noqa: E402
import digest  # noqa: E402

Z = config.PROVIDER_PRESETS["zhipu"]
URL = Z["base_url"].rstrip("/") + "/chat/completions"
HDR = {"Authorization": "Bearer " + Z["api_key"]}

MODELS = ["glm-4.7", "glm-4.5-air", "glm-4-flash"]


def load_entries():
    d = os.path.join(config.BASE_DIR, "archive")
    rows = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(d, name), encoding="utf-8") as f:
            for ln in f:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
    return rows[-200:]


def make_ask(model, stats):
    async def ask(system, user):
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": config.DIGEST_MAX_TOKENS,
            "temperature": config.AI_TEMPERATURE,
            "thinking": {"type": "disabled"},
        }
        r = httpx.post(URL, headers=HDR, json=body, timeout=120, verify=False)
        if r.status_code != 200:
            stats["err"] = f"HTTP {r.status_code}: {r.text[:90]}"
            return None
        d = r.json()
        u = d.get("usage") or {}
        stats["tok"] = (stats.get("tok") or 0) + (u.get("total_tokens") or 0)
        stats["cached"] = (stats.get("cached") or 0) + (
            (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return d["choices"][0]["message"].get("content") or ""

    return ask


async def run(model, entries):
    stats = {}
    store = digest.GroupDigest()
    out = await digest.compress_group(store, "PROBE_G", entries, make_ask(model, stats))
    if not out:
        return None, stats
    return out, stats


async def check_json(model, entries):
    """单独再问一次，看 <DIGEST> 段是否是合法 JSON（compress_group 会吞掉解析失败）。"""
    payload = "[这段时间的新内容]\n" + digest.render_transcript(entries, max_chars=10 ** 9)
    stats = {}
    raw = await make_ask(model, stats)(digest.SYSTEM_REDUCE, payload)
    if raw is None:
        return None, stats
    m = digest.DIGEST_TAG.search(raw)
    if not m:
        return {"has_tag": False}, stats
    try:
        data = json.loads(m.group(1))
    except Exception as e:
        return {"has_tag": True, "parses": False, "err": str(e)[:60]}, stats
    return {"has_tag": True, "parses": True, "keys": sorted(data.keys())}, stats


async def main_async():
    entries = load_entries()
    chars = sum(len(e.get("text") or "") for e in entries)
    print(f"样本：{len(entries)} 条消息 / {chars} 字\n")

    for model in MODELS:
        print(f"{'=' * 56}\n▸ {model}")
        try:
            res, stats = await run(model, entries)
        except Exception as e:
            print(f"  ❌ 异常 {type(e).__name__}: {str(e)[:90]}")
            continue
        if stats.get("err"):
            print(f"  ❌ {stats['err']}")
            continue
        if not res:
            print("  ❌ 未产出结果")
            continue
        data = res.get("data") or {}
        print(f"  简报：{res.get('brief', '')[:100]}")
        print(f"  字段：{ {k: len(v) for k, v in data.items()} }")
        print(f"  token：{stats.get('tok')}（命中 {stats.get('cached')}）")

        jd, _ = await check_json(model, entries)
        print(f"  JSON 校验：{jd}")

        if data.get("affinity"):
            print(f"  affinity 样例：{data['affinity'][0]}")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
