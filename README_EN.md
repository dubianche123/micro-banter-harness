<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular — QQ AI Group Chat Bot

**A resident group-chat companion with code-owned relationships and memory.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![Offline checks](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

[中文](README.md) · **English** · [Get v0.2.0](https://github.com/dubianche123/the-regular-qq-bot/releases/tag/v0.2.0)

[Run it](#quick-start) · [Message routes](#one-message-three-possible-routes) · [Context design](#the-context-has-an-order) · [Commands](#-cheatsheet-commands--interactions)

</div>

Most group-chat bots wear out their welcome in three days: they reply to every message like a helpdesk ticket, speak to strangers and old friends in the exact same tone, and forget yesterday's running jokes the moment the context window slides.

The Regular lives in a QQ group like an actual regular: mostly lurking, occasionally chiming in when the mood strikes, and gradually building history with the people in the room. It tracks per-group nicknames, eight relationship tiers, four interaction modes (`banter`, `confidant`, `defender`, `hype`), a circadian clock (complete with 3 a.m. wake-up grumpiness), a 6-hour mood half-life, a rolling group chronicle (`群史记`), and a promise ledger.

Underneath that social surface is a compact agent harness: **Python owns the durable ledger and activation gates, while the LLM only generates dialogue inside a cache-friendly, four-layer prefix**. Combined with zero-token local handlers and multi-provider fallback, everyday group banter doesn't require burning a heavyweight model call on every message.

**First use:** [Install](#quick-start), follow the setup wizard, start the bot, and send it one private message to claim ownership. Want to try it before setting up QQ or API keys? Run `regular demo`.

## What you can try

| Interaction | How the code actually handles it |
| --- | --- |
| **Tone shifts as you get closer** | Eight relationship tiers (from `distant` to `soulmate`) are scored in Python. Daily affinity budgets are **asymmetric** (generous on gains, strict on losses) so a single off-kilter model judgement can't tank a weeks-old friendship back to stranger status. |
| **Different dynamic with each person** | Instead of forcing the model to append a self-grading JSON blob on every chat turn, the daily digest reviews the full day's conversation and proposes affinity deltas plus an interaction mode (`banter` / `confidant` / `defender` / `hype`), enforced against a hard whitelist. |
| **Circadian rhythm, mood, and knowing when to stay quiet** | Replies are gated by mentions, probability, cooldowns, time of day, mood, and affinity. Messages between 00:00 and 06:00 cut chime-in willingness to `0.45x` and inject "just woken up, keep it short and grumpy" into the turn state; mood decays toward neutral with a **6-hour half-life** (morning annoyance fades by evening). |
| **Closer friends get deeper context** | Affinity controls how much recent group chatter is pulled into the turn (`soulmate` sees 10 messages; `distant` sees 2—no need to dig up old history with someone it barely knows). |
| **Per-group nicknames that the model can't overwrite** | Self-claimed or owner-assigned nicknames live in per-group storage. Names mentioned in daily model digests are used strictly for **one-way lookup** to apply affinity deltas—never to overwrite a user's canonical nickname. |
| **Reads group images once** | A vision model captions incoming images and caches descriptions by message ID under independent daily budget caps. |
| **Zero-token local games & lookups** | `掷骰子` (dice), `查好感` (affinity), `关系榜` (leaderboard), `群史记` (chronicle), `查账` (promise ledger), and `翻旧账` (archive search) run entirely in local Python handlers with 0 tokens. |
| **Shared group atmosphere modes** | Switch into catgirl, unhinged, disciplinary, fortune-teller, or banter modes; gated by local affinity (`familiar` or higher) so drive-by strangers can't derail the room. |

## One message, three possible routes

![Routing, code-owned state and generated replies](docs/readme/flow.svg)

| Example input | Route | What happens under the hood |
| --- | --- | --- |
| `查好感` / `关系榜` | Local relationship lookup | Reads directly from the local ledger instead of asking an LLM to hallucinate a score. |
| `掷骰子` / `决斗` | Local game handler | Rolls locally and applies the coded affinity delta for wins/losses. |
| `叫我小林` | Naming flow | Checks owner locks, word filters, and cooldowns before updating the group-scoped nickname. |
| `@<bot> 今天吃什么` | Generated reply | Assembles the 4-layer context prefix, calls the configured provider tier, and sanitizes the output. |
| Unmentioned group chatter | Activation gate | Checks cooldown, circadian factor, mood, and affinity dice; stays quiet and archives the message if it doesn't fire. |

Keeping the ledger in Python means **the model never gets write access to relationship counters**—it can roast you in character, but it cannot accidentally wipe out your affinity score.

## The context has an order

[get_ai_reply()](bot.py) orders the prompt from most stable to most volatile so provider prefix caches can actually hit:

| Layer | Contents | Why it sits here |
| --- | --- | --- |
| 1 · Stable head | Persona, group rules, output constraints, optional protocol | Identical across turns in the same group and mode; maximizes prefix-cache reuse. |
| 2 · Group memory | Current rolling digest (`群史记`) | Changes only when background compression folds in a new batch of messages. |
| 3 · Session history | Accepted recent conversation turns | Appends cleanly onto the cached prefix. |
| 4 · This turn | Speaker relationship card, affinity-sliced nearby chatter, circadian/mood state line | Changes on every speaker and timestamp, so it always sits at the very end. |

A session lock guards prompt assembly and history write-back so rapid-fire group messages don't tear the conversation state. Real-world prefix-cache observations and provider probe notes are documented in [Design Notes](docs/DESIGN_NOTES_EN.md).

## Quick start

### 1. Install the release

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once, then copy:

```bash
uv tool install --python 3.12 https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl
```

uv prepares Python 3.12, dependencies and an isolated tool environment. No clone
or manual virtual-environment activation is needed. If `regular` is not found,
run `uv tool update-shell` and reopen the terminal.

To try it first:

```bash
uvx --python 3.12 --from https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl regular demo
```

The demo needs no account or key and makes no QQ or model requests. Try
`查好感`, `掷骰子`, or `群史记`; `/help` lists controls. After installation,
`regular demo` works too.

<details>
<summary>Install with an existing Python and pip</summary>

Python 3.10+ is required; 3.12 is recommended. Install the same wheel in a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl
```

On Windows, use `python` instead of `python3` and activate with `.\.venv\Scripts\Activate.ps1`. Setup and run commands
below remain the same.

</details>

<details>
<summary>Develop from source</summary>

```bash
git clone https://github.com/dubianche123/the-regular-qq-bot.git
cd the-regular-qq-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

</details>

### 2. Configure once with the wizard

Have your QQ AppID/AppSecret and model endpoint URL, API key and model ID ready:

```bash
regular --directory regular-bot setup
```

`regular-bot` is your local instance directory and is created automatically;
you can choose an absolute path instead. The wizard hides secret input, creates
a private `.env`, and runs local checks.
If `.env` already exists, it fills missing values while keeping existing settings.
Zhipu/Gemini presets only need their corresponding key. Successful setup gives
you the launch command. No database, retry policy or separate image configuration
is required.

Image reading follows the main model by default. For a text-only chat model, set
`VISION_MODEL`; set `VISION_PROVIDER` too for another provider. Optional
`BOT_NAMES` adds group aliases; platform nicknames and mentions still work when
it is blank. [Model configuration](docs/providers.md) · [All example settings](.env.example)

### 3. Start and send one private message to claim ownership

In QQ, enable C2C and the required group events, and restrict usage scope to the
intended owner or test members. Then start:

```bash
regular --directory regular-bot run
```

Wait for the logged online confirmation before treating QQ as connected. Rejected
credentials or permissions stop startup. The first login waits at most
60 seconds, and repeated connection failures can stop earlier with guidance; fix the instance's `.env` and rerun the command above.

**The intended owner's first private message automatically claims the bot.**
No phrase or extra confirmation is needed; that message receives its normal
reply. Ownership persists across restarts and later private messages cannot
replace it. QQ supports full or allowlist access, so check the
[usage scope](https://bot.q.qq.com/wiki/#_10-%E4%BD%BF%E7%94%A8%E8%8C%83%E5%9B%B4%E4%B8%8E%E4%BA%BA%E5%91%98%E9%85%8D%E7%BD%AE)
before first use.

After adding the bot to a group, choose **获取群内全部消息** in its message scope
under group settings, then mention it. If still unowned after 10 minutes, its
next normal reply includes one reminder. Group messages cannot claim ownership.

Use `regular --directory regular-bot doctor` for local diagnostics and
`regular --directory regular-bot status` in another terminal for connection and
budget status. Configuration and data stay inside the selected instance. Stop foreground execution with Ctrl+C. Run one process per instance.

<details>
<summary>Optional: scripted configuration and diagnostics</summary>

Enter the instance directory before using commands below without `--directory`.

For noninteractive installation, `regular init` creates a blank credentials
template; fill it and run `regular doctor`. `init` never overwrites a file; use
`regular setup` to fill missing settings. Real environment variables take
precedence over `.env`, including explicitly empty values.

`regular doctor --json` produces structured local checks. Explicit
`regular doctor --network` also checks provider `/models` metadata without
inference; success does not prove model quota or image support.
`regular status --json` reads the running instance's status snapshot.

</details>

<details>
<summary>Optional: process supervision and Docker</summary>

The installed package includes a service generator. It uses the current tool's
Python and your chosen instance path; it does not install or start a service.

On macOS, generate and inspect the plist, then load it:

```bash
regular --directory regular-bot service launchd --output regular-bot/data/io.github.the-regular.plist
mkdir -p ~/Library/LaunchAgents
cp regular-bot/data/io.github.the-regular.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.the-regular.plist
```

On Linux, generate and enable a user service:

```bash
regular --directory regular-bot service systemd --output regular-bot/data/the-regular.service
mkdir -p ~/.config/systemd/user
cp regular-bot/data/the-regular.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now the-regular
```

Stop the foreground process before starting a service for the same instance.
Configure proxies in that instance's `.env` if needed.

Docker and source helpers are included in the
[source archive](https://github.com/dubianche123/the-regular-qq-bot/releases/tag/v0.2.0).
Run these commands from its extracted source directory:

```bash
regular init
# Fill the current directory's .env
docker compose build
docker compose run --rm regular regular doctor
docker compose up -d
docker compose logs -f
```

The container reads that `.env` and stores data in the `regular-data` named
volume. `docker compose down -v` deletes the volume.

### Runtime data

Each instance keeps its `.env` at its root and state, owner identity, logs,
archives and summaries under `data/` by default. `DATA_DIR` chooses another
location; relative paths resolve from the instance directory. Stop the instance
before backing up its `.env` and data directory. QQ uses outbound connections;
no web port needs to be exposed.

</details>

---

## 🎮 Cheatsheet: Commands & Interactions

Built-in command phrases below are shown exactly as accepted by the local handlers.

| Feature | How to Say | Token Cost |
| :--- | :--- | :---: |
| **Chat** | Mention the bot or use a configured alias | 1 call |
| **Random Banter** | Ordinary group conversation may trigger an interjection | 1 call |
| **Send an image** | Mention the bot with a picture | 1 chat + up to 1 vision call |
| **Dice / Duel** | `掷骰子` / `摇骰子` / `比大小` / `决斗` | **0** |
| **Nicknames / Rename** | `叫我阿强` / `我是阿强` / `别叫我了` | 0–1 call |
| **Switch Mode** | `猫娘模式` / `发疯模式` / `风纪委员模式` / `算命模式` / `老司机模式` | **0** |
| **Reset Mode** | `恢复正常` / `正常点` / `冷静点` / `吃药了` | **0** |
| **Check Affinity** | `查好感` / `我跟你多熟` / `几档了` | **0** |
| **Leaderboard** | `关系榜` / `点名册` / `群友榜` | **0** |
| **Chronicles** | `群史记` / `最近聊了啥` / `周报` | **0** |
| **Promise Ledger** | `查账` / `谁请客`; after fulfilling: `结清` | **0** |
| **Verify Archives** | `翻旧账 关键词` | **0** |
| **Owner Admin** | Mention a member with `叫他阿珍`; `锁起名` / `解锁起名` | **0** |


---

## ⚙️ Key Configuration Options

Tweak bot behavior and budget guardrails in `.env`:

* `BANTER_PROBABILITY`: Probability of random banter (default `0.12` or 12%)
* `OWNER_MENTION_PROBABILITY`: Banter chance when owner is mentioned (default `0.30`)
* `BANTER_COOLDOWN_SECONDS`: Minimum cooldown between banter replies (default `120`s)
* `DAILY_BUDGET`: Max daily LLM calls for the whole group (default `3000`, prevents wallet drain)
* `MODE_SWITCH_MIN_AFFINITY`: Minimum affinity level required to switch modes (default `familiar`)

---

## Troubleshooting

| Symptom | What to check first |
| --- | --- |
| Process starts, but group messages never arrive | QQ sandbox member list and the group's **获取群内全部消息** (Receive all group messages) setting. |
| Doesn't respond when called by name | Check `BOT_NAMES` in `.env` (when blank, only `@` mentions and the platform nickname trigger it). |
| Chat works, but owner commands don't | Make sure you've sent the first private message (C2C) to claim ownership; the bound owner ID is stored in `data/owner.txt`. |
| Group nicknames don't show up in private chat | Nicknames are scoped per group; C2C private chat has no group context. |
| Prefix cache hit rate is lower than expected | Check provider prefix-caching support, task-tier routing, prefix stability, and whether silence between messages exceeded the provider's cache TTL. |

## Code Map

| File | Responsibility |
| --- | --- |
| [bot.py](bot.py) | QQ event routing, zero-token local commands, 4-layer prompt assembly, output sanitization & send loop |
| [activation.py](activation.py) | Circadian clock (with 3 a.m. wake-up grumpiness), 6-hour mood half-life, affinity-gated context window |
| [affinity.py](affinity.py), [relations.py](relations.py) | Eight-tier relationship ledger, asymmetric daily caps, daily digest scoring & leaderboards |
| [naming.py](naming.py), [nicking.py](nicking.py) | Per-group nickname resolution & one-way lookup protection |
| [runtime.py](runtime.py), [storage.py](storage.py) | Shared runtime singletons, session locks & local JSON persistence |
| [archive.py](archive.py), [digest.py](digest.py) | Raw message archive, rolling chronicle (`群史记`) compression & interaction-mode extraction |
| [providers.py](providers.py) | Task-tiered model routing & provider fallback |
| [wordfilter.py](wordfilter.py), [qqtext.py](qqtext.py) | Content checks & QQ message rendering |
| [tests/](tests/) | Offline regression suite & standalone provider cache probes |

---

## 📄 License

Open-sourced under the [MIT License](LICENSE).
Pull requests and stars are welcome—give your group chat a regular who actually remembers the room.
