"""缓存时效实验：前缀命中的窗口到底有多久（决定「让位后多久回来重试」）。

为什么要测这个
--------------
主供应商（Gemini）抖一下就让位给智谱，问题是什么时候**该回来**：
  · 回来太早 → 智谱刚暖起来的前缀白扔，回来还得重新 prefill；
  · 回来太晚 → 明明缓存凉了还硬留在慢的那家。
官方文档只写了「缓存有合理的时效性」，没给数字。所以自己测。

测法：同一段稳定前缀，隔一个越来越长的间隔发一次请求，看 usage 里的
cached_tokens 还在不在。命中一次就刷新一次计时，所以量到的是
「**距上一次请求**多久之后缓存还在」—— 正是我们要的那个数。

跑法（约 15 分钟，全程几十个 token）：
    ../.venv/bin/python probe_cache_ttl.py            # 只测智谱
    ../.venv/bin/python probe_cache_ttl.py --gemini   # 顺带测 Gemini 隐式缓存
"""
import json
import os
import sys
import time
import uuid
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import config  # noqa: E402
import prompts  # noqa: E402

GAPS = [60, 120, 240, 420]      # 距上一次请求的间隔，累计约 14 分钟


def _stable_head():
    """线上真实稳定头：角色 + 排版约束（全群全天逐字节一致的那一段）。"""
    head = prompts.PROMPT_NORMAL + prompts.PROMPT_OUTPUT_RULES
    # 再垫一段线上真实存在的长期记忆，让可缓存前缀足够长（太短可能压根不建缓存）
    try:
        with open(config.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        for gid, blob in (state.get("digests") or {}).items():
            brief = (blob or {}).get("brief")
            if brief:
                head += f"\n\n【这个群最近发生的事】\n{brief}"
                break
    except Exception:
        pass
    return head


def ask(url, headers, model, messages, extra=None, proxy=None, maxtok=64):
    body = {"model": model, "messages": messages, "max_tokens": maxtok, "temperature": 0.9}
    body.update(extra or {})
    t0 = time.time()
    try:
        kw = {"timeout": 60, "verify": False}
        if proxy:
            kw["proxy"] = proxy
        r = httpx.post(url, headers=headers, json=body, **kw)
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {str(e)[:60]}"
    dt = time.time() - t0
    if r.status_code != 200:
        return None, dt, f"HTTP {r.status_code}: {r.text[:80]}"
    u = (r.json().get("usage") or {})
    return {
        "prompt": u.get("prompt_tokens"),
        "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
    }, dt, None


def probe(model, url, headers, head, gaps, extra=None, proxy=None):
    print(f"  模型 {model}")
    print(f"    {'间隔':>7}{'输入tok':>10}{'命中tok':>10}{'命中率':>9}{'延迟':>9}")
    msgs = [{"role": "system", "content": head}]

    # 基线：连发两次，确认这个前缀确实能被缓存（间隔 0 就可能被测出假阴性）
    for tag, wait in (("预热", 0), ("复用", 5)):
        if wait:
            time.sleep(wait)
        res, dt, err = ask(url, headers, model, msgs + [{"role": "user", "content": f"{tag}（{uuid.uuid4().hex[:8]}）"}],
                           extra=extra, proxy=proxy)
        if res is None:
            print(f"    {tag:<7}❌ {err}")
            return
        p, c = res["prompt"] or 1, res["cached"] or 0
        print(f"    {tag:<7}{res['prompt']:>10}{c:>10}{c / p * 100:>8.0f}%{dt:>8.2f}s")

    last = time.time()
    for gap in gaps:
        time.sleep(max(0.0, last + gap - time.time()))
        res, dt, err = ask(url, headers, model, msgs + [{"role": "user", "content": f"间隔{gap}秒（{uuid.uuid4().hex[:8]}）"}],
                           extra=extra, proxy=proxy)
        last = time.time()
        if res is None:
            print(f"    {gap:>5}s ❌ {err}")
            continue
        p, c = res["prompt"] or 1, res["cached"] or 0
        verdict = "✅ 还在" if c else "❌ 已过期"
        print(f"    {gap:>5}s{res['prompt']:>10}{c:>10}{c / p * 100:>8.0f}%{dt:>8.2f}s  {verdict}")


def main():
    head = _stable_head()
    print(f"稳定前缀 {len(head)} 字\n")

    z = config.PROVIDER_PRESETS["zhipu"]
    print("=" * 56)
    print("智谱隐式缓存：距上次请求多久还命中")
    print("=" * 56)
    probe("glm-4.5-air", z["base_url"].rstrip("/") + "/chat/completions",
          {"Authorization": "Bearer " + z["api_key"]},
          head, GAPS, extra=z.get("extra_body"))

    if "--gemini" in sys.argv:
        g = config.PROVIDER_PRESETS["gemini"]
        print("\n" + "=" * 56)
        # 实测教训：请求体 ~1090 tok 已超过官方 1024 的下限，命中仍然恒为 0 ——
        # 所以别看到「够大了」就以为能命中，这个探针就是用来证伪这种预期的。
        print("Gemini 隐式缓存：命中与否（请求体 ~1090 tok，已过官方 1024 下限）")
        print("=" * 56)
        probe("gemini-3.5-flash-lite", g["base_url"].rstrip("/") + "/chat/completions",
              {"Authorization": "Bearer " + g["api_key"]},
              head, [60, 120, 240], extra=g.get("extra_body"),
              proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))


if __name__ == "__main__":
    main()
