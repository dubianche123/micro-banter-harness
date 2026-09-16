"""好感度到底有没有体现？把同一句话喂给不同档位，看回答差多少。

顺便验证记账协议的代价：带 <CMD> 要求 vs 不带，正文质量有没有肉眼差别。
"""
import asyncio
import sys
import time

sys.path.insert(0, "..")

import config
import prompts
import relations
from httpx import AsyncClient
from openai import AsyncOpenAI

# 占位 openid：只用来给档案当 key，随便写一个假的即可（别把真实 openid 提交进仓库）
OID = "0123456789ABCDEF0123456789ABCDEF"
NOW = time.time()

# 同一句话，分别以「第一次搭话」和「本命」的身份问
SCENES = [
    "今天加班到十点，累死了",
    "机器人你帮我看看这题怎么做",
    "在吗",
]

PROFILES = {
    "陌生人（第1次）": {"interactions": 0},
    "点头之交（+12）": {"score": 12, "interactions": 6, "last_seen": NOW - 7200},
    "本命（100分/50次）": {"score": 100, "interactions": 50, "last_seen": NOW - 60,
                          "nick": "小满"},
}


def build_note(profile, with_cmd):
    rec = dict(profile)
    note = relations.build_relation_note(rec, OID, mode="normal", now=NOW)
    system = prompts.PROMPT_NORMAL + "\n" + note
    if with_cmd:
        system += "\n" + prompts.CMD_PROTOCOL
    return system


def make_client():
    p = config.PROVIDER_PRESETS[config.AI_PROVIDER]
    return AsyncOpenAI(api_key=p["api_key"], base_url=p["base_url"],
                       timeout=config.AI_TIMEOUT_SECONDS,
                       http_client=AsyncClient(verify=False))


async def ask(client, system, user):
    t0 = time.time()
    r = await client.chat.completions.create(
        model=config.PROVIDER_PRESETS[config.AI_PROVIDER]["models"][0],
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=config.AI_MAX_TOKENS,
        temperature=config.AI_TEMPERATURE,
    )
    return (r.choices[0].message.content or "").strip(), time.time() - t0


async def main():
    client = make_client()
    model = config.PROVIDER_PRESETS[config.AI_PROVIDER]["models"][0]
    print(f"供应商 {config.PROVIDER['label']} / {model}\n")

    for user in SCENES:
        print("=" * 76)
        print(f"群友说：{user}")
        print("=" * 76)
        for name, prof in PROFILES.items():
            sysmsg = build_note(prof, with_cmd=True)
            reply, dt = await ask(client, sysmsg, user)
            print(f"\n  [{name}] {dt:.1f}s")
            for line in reply.split("\n"):
                if line.strip():
                    print(f"      {line}")
        print()

    print("=" * 76)
    print("记账协议的代价：本命档下带 / 不带 <CMD> 要求")
    print("=" * 76)
    prof = PROFILES["本命（100分/50次）"]
    for tag, with_cmd in (("带记账协议", True), ("不带", False)):
        sysmsg = build_note(prof, with_cmd)
        reply, dt = await ask(client, sysmsg, "今天加班到十点，累死了")
        print(f"\n  [{tag}] {dt:.1f}s  正文 {len(reply)} 字")
        print(f"      {reply[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
