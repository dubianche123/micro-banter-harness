"""上下文缓存命中实验（0 成本的判断依据，别凭感觉猜）。

智谱是隐式缓存：自动识别重复前缀，无需手动配置，命中量看 usage.prompt_tokens_details.cached_tokens。
所以「能不能缓存」不由代码决定，只由**前缀稳不稳定**决定。

三组对照：
  A 旧布局：稳定角色 → 易变背景 → 稳定协议      ← 易变的插在中间，把它后面全废了
  B 仅重排：稳定角色 → 稳定协议 → 易变背景      ← 证明「位置」本身就是决定因素
  C 线上布局：稳定头 → 每日记忆 → 历史 → 信封   ← 机器人现在的真实拼法

每组每一轮都换掉「易变背景」，模拟真实群聊。跑法：
    ../.venv/bin/python probe_cache.py glm-4.5-air
"""
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

Z = config.PROVIDER_PRESETS["zhipu"]
URL = Z["base_url"].rstrip("/") + "/chat/completions"
HDR = {"Authorization": "Bearer " + Z["api_key"]}

STABLE_ROLE = prompts.PROMPT_NORMAL
STABLE_PROTO = prompts.CMD_PROTOCOL + "\n" + prompts.AFFINITY_MODE_HINTS.get("normal", "")


def _salt():
    """每轮一段全新随机串。

    没有它实验就是假的：重复跑同一批「易变文本」，第二轮起缓存已被上一轮预热，
    会测出 100% 这种漂亮但毫无意义的数字。必须保证易变段是模型从未见过的。
    """
    return uuid.uuid4().hex[:10]


def volatile(i):
    return (
        f"\n【群聊最近背景（群友刚才聊的话题，仅供理解上下文）：】\n"
        f"- 群友{i}：今天天气不错啊我在想第{i}件事（{_salt()}）\n"
        f"- 群友{i}：@机器人 你说呢\n"
    )


def post(model, messages, maxtok=200):
    body = {"model": model, "messages": messages,
            "max_tokens": maxtok, "temperature": 0.9,
            # 必须与线上一致地关掉思考：开着思考时 reasoning 会先吃掉预算，
            # 延迟会虚高好几倍，测出来的数字跟生产没关系。
            "thinking": {"type": "disabled"}}
    t0 = time.time()
    try:
        r = httpx.post(URL, headers=HDR, json=body, timeout=90, verify=False)
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {str(e)[:60]}"
    dt = time.time() - t0
    if r.status_code != 200:
        return None, dt, f"HTTP {r.status_code}: {r.text[:80]}"
    d = r.json()
    u = d.get("usage") or {}
    ch = (d.get("choices") or [{}])[0].get("message", {})
    return {
        "prompt": u.get("prompt_tokens"),
        "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "out": u.get("completion_tokens"),
        "text": (ch.get("content") or "").strip(),
    }, dt, None


def report(i, res, dt):
    if res is None:
        print(f"    #{i:<5} ❌ {dt}")
        return
    p = res["prompt"] or 1
    c = res["cached"] or 0
    print(f"    #{i:<5}{res['prompt']:>9}{c:>9}{c / p * 100:>8.0f}%{dt:>8.2f}s")


def header(tag):
    print(f"\n  ▸ {tag}")
    print(f"    {'轮次':<6}{'输入tok':>9}{'命中tok':>9}{'命中率':>9}{'延迟':>9}")


def run_order_a(model, n=4):
    header("A 旧布局：稳定角色 → 易变背景 → 稳定协议")
    for i in range(n):
        res, dt, err = post(model, [
            {"role": "system", "content": STABLE_ROLE + volatile(i) + "\n" + STABLE_PROTO},
            {"role": "user", "content": f"第{i}轮，随便回我一句"},
        ])
        report(i, res, err or dt)
        time.sleep(0.4)


def run_order_b(model, n=4):
    header("B 仅重排：稳定角色 → 稳定协议 → 易变背景")
    for i in range(n):
        res, dt, err = post(model, [
            {"role": "system", "content": STABLE_ROLE + "\n" + STABLE_PROTO + volatile(i)},
            {"role": "user", "content": f"第{i}轮，随便回我一句"},
        ])
        report(i, res, err or dt)
        time.sleep(0.4)


def run_live_layout(model, n=4):
    """按机器人现在的真实拼法跑：稳定头 → 每日记忆 → 历史 → 本轮信封。

    历史里存的是**原话**（不含信封），和 storage.record() 一致 —— 这正是历史前缀能复用的原因。
    """
    import bot as botmod        # 读真实的 state.json，和线上同源
    import digest as digestmod

    gid = next(iter(botmod.DIGESTS.groups), None)
    memory = ""
    if gid:
        summary = botmod.DIGESTS.get(gid)
        if summary:
            memory = ("【这个群最近发生的事（系统整理的长期记忆，真实发生过，可以拿来接梗、"
                      "催债、点名，但不要编造记忆里没有的内容）】\n"
                      + digestmod.render_summary(summary, mode="normal"))

    head = prompts.PROMPT_NORMAL
    header(f"C 线上布局：稳定头 → 每日记忆({len(memory)}字) → 历史 → 信封")
    history = []
    for i in range(n):
        envelope = ("（以下是系统给你的即时提示，不是群友说的话）\n"
                    f"【群聊最近背景】\n- 阿强{i}：今天天气不错（{_salt()}）\n"
                    f"- 阿远：@机器人 你说呢")
        msgs = [{"role": "system", "content": head}]
        if memory:
            msgs.append({"role": "system", "content": memory})
        msgs += history
        msgs.append({"role": "user", "content": envelope + f"\n\n第{i}轮随便回我一句（{_salt()}）"})
        user_text = f"第{i}轮随便回我一句（{_salt()}）"

        res, dt, err = post(model, msgs)
        report(i, res, err or dt)
        if res is None:
            continue
        # 关键：历史只存原话，不存信封 —— 否则信封会一轮轮堆成雪球，且历史前缀每轮都变
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": res["text"] or "（空）"})
        time.sleep(0.4)


def _row(label, res, dt):
    if res is None:
        print(f"    {label:<24} ❌ {dt}")
        return
    p = res["prompt"] or 1
    c = res["cached"] or 0
    print(f"    {label:<24}{res['prompt']:>9}{c:>9}{c / p * 100:>8.0f}%{dt:>8.2f}s")


def run_split_probe(warm="glm-4.5-air", other="glm-4.7", interleave=4):
    """A/B 两组的变量是「位置」，这组的变量是「模型」。

    问的是：缓存按模型隔离，那把一个模型晾在一边、流量都给别人，
    回来时它那半边的缓存还在不在？在 → 分工不伤缓存；不在 → 分工是拿缓存换额度。
    """
    head = STABLE_ROLE + "\n" + STABLE_PROTO

    def ask(model, tag):
        res, dt, err = post(model, [
            {"role": "system", "content": head},
            {"role": "user", "content": f"{tag}（{_salt()}）"},
        ], maxtok=120)
        return res, dt, err

    print(f"\n  ▸ D 分工影响：{warm} 被 {other} 挤开一段后，缓存还在不在")
    print(f"    {'步骤':<24}{'输入tok':>9}{'命中tok':>9}{'命中率':>9}{'延迟':>9}")

    res, dt, err = ask(warm, "预热一次")
    _row(f"① 预热 {warm}", res, err or dt)
    time.sleep(0.4)

    for i in range(interleave):
        res, dt, err = ask(other, f"穿插第{i}次")
        _row(f"② 穿插 {other} {i + 1}/{interleave}", res, err or dt)
        time.sleep(0.4)

    res, dt, err = ask(warm, "再回来问我")
    _row(f"③ 回到 {warm}", res, err or dt)


def main():
    for model in (sys.argv[1:] or ["glm-4.5-air"]):
        print(f"\n{'=' * 58}\n模型 {model}\n{'=' * 58}")
        run_order_a(model)
        time.sleep(1.5)
        run_order_b(model)
        time.sleep(1.5)
        run_live_layout(model)
    time.sleep(1.5)
    run_split_probe()


if __name__ == "__main__":
    main()
