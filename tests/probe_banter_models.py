"""插嘴场景模型对照：glm-4.7 关/开思考 vs glm-4.5-air vs gemini-3.5-flash-lite。

回答两个问题：
  1) 4.7 和 gemini-3.5-flash-lite 在「群聊插嘴」这种短回复任务上差多少？
  2) 关思考到底亏没亏 —— 开思考在 120 token 的插嘴上是不是必然返回空？

控制变量：同一份线上 system prompt（含真实 MODE_PROMPT）、同一个 harness 结构
（稳定头 → 记忆 → 历史 → 末尾信封）、同一个 max_tokens、同一个 temperature。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import prompts
from openai import AsyncOpenAI
from httpx import AsyncClient as _AC

CHAT_TOK = config.AI_MAX_TOKENS          # 512，正经对话
BANTER_TOK = config.AI_MAX_TOKENS_BANTER  # 120，插嘴

# 全部取自今天 archive 里的真实群聊消息，非 @，且是「值得接话」的那类
BANTER_CASES = [
    ("吐槽角色", "感觉换了个模型之后没有之前那个有趣啊"),
    ("日常短句", "等我回去换一下"),
    ("抽象玩梗", "我日牛魔的别的都记不住唯独记得住阿龙"),
    ("一句吐槽", "太蠢了日"),
]

CHAT_CASES = [
    ("正经接话", "机器人，这游戏平衡性是不是烂透了"),
]

CTX_BANTER = "【群聊最近背景】\n- 老张：这ai最近有点笨\n- 群友D3E8：？？？"


def build_head(mode="normal"):
    return prompts.MODE_PROMPTS.get(mode, prompts.PROMPT_NORMAL) if hasattr(prompts, "MODE_PROMPTS") \
        else prompts.PROMPT_NORMAL


def build_turn_ctx(is_banter):
    bits = []
    if is_banter:
        bits.append(prompts.PROMPT_BANTER_NOTE.strip())
    bits.append(CTX_BANTER)
    return "（以下是系统给你的即时提示，不是群友说的话）\n" + "\n".join(bits)


def make_client(provider):
    p = config.PROVIDER_PRESETS[provider]
    return AsyncOpenAI(api_key=p["api_key"], base_url=p["base_url"],
                       timeout=config.AI_TIMEOUT_SECONDS,
                       http_client=_AC(verify=False))


def build_messages(user_text, is_banter):
    """按 storage.build_messages 的同一顺序手搭，避免依赖 session 状态。"""
    sys_ = build_head()
    msgs = [{"role": "system", "content": sys_}]
    msgs.append({"role": "system", "content":
                 "【这个群最近发生的事（系统整理的长期记忆）】\n昨天大伙在聊换模型的事，老张嫌新模型没意思。"})
    if is_banter:
        msgs.append({"role": "user", "content": "这ai最近有点笨"})
        msgs.append({"role": "assistant", "content": "（挠头）行吧，那我再努努力。"})
    msgs.append({"role": "user", "content": user_text})
    ctx = build_turn_ctx(is_banter)
    if ctx:
        msgs[-1]["content"] = ctx + "\n\n" + user_text
    return msgs


async def call(client, model, extra, messages, maxtok):
    t0 = time.time()
    try:
        r = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=maxtok,
            temperature=config.AI_TEMPERATURE, extra_body=extra or None,
        )
        dt = time.time() - t0
        msg = r.choices[0].message
        rc = getattr(msg, "reasoning_content", None)
        u = getattr(r, "usage", None)
        return {"ok": True, "dt": dt, "content": (msg.content or "").strip(),
                "reasoning_len": len(rc or ""),
                "tok": getattr(u, "completion_tokens", None),
                "cached": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)}
    except Exception as e:
        return {"ok": False, "dt": time.time() - t0, "err": f"{type(e).__name__}: {e}"[:200]}


async def main():
    z = make_client("zhipu")
    g = make_client("gemini")

    VARIANTS = [
        ("A glm-4.7  关思考", z, "glm-4.7", {"thinking": {"type": "disabled"}}),
        ("B glm-4.7  开思考", z, "glm-4.7", {"thinking": {"type": "enabled"}}),
        ("C glm-4.5-air 关思考", z, "glm-4.5-air", {"thinking": {"type": "disabled"}}),
    ]
    if "--with-gemini" in sys.argv:
        VARIANTS.append(("D gemini-3.5-flash-lite", g, "gemini-3.5-flash-lite", None))

    for label, cases, is_banter, maxtok in (
        ("插嘴场景", BANTER_CASES, True, BANTER_TOK),
        ("正经对话", CHAT_CASES, False, CHAT_TOK),
    ):
        print("\n" + "=" * 84)
        print(f"【{label}】max_tokens={maxtok}")
        print("=" * 84)
        for title, user in cases:
            print(f"\n▸ {title}：{user}")
            for vname, cl, model, extra in VARIANTS:
                res = await call(cl, model, extra, build_messages(user, is_banter), maxtok)
                if not res["ok"]:
                    print(f"   [{vname:24s}] ✗ {res['err']}")
                    continue
                empty = "⚠️空" if not res["content"] else ""
                print(f"   [{vname:24s}] {res['dt']:5.1f}s tok={res['tok']} "
                      f"think={res['reasoning_len']}字 cache={res['cached']} {empty}")
                print(f"       → {res['content'][:120]!r}")


if __name__ == "__main__":
    asyncio.run(main())
