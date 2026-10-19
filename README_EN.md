<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular — QQ AI Group Chat Bot

**A resident group-chat companion with code-owned relationships and memory.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![Offline checks](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

[中文](README.md) · **English** · [Get v0.2.0](https://github.com/dubianche123/the-regular-qq-bot/releases/tag/v0.2.0)

[Run it](#quick-start) · [Message routes](#one-message-three-possible-routes) · [Context design](#the-context-has-an-order) · [Commands](#-cheatsheet-commands--interactions)

</div>

The Regular lives in a QQ group as a recurring character: usually quiet, occasionally opinionated, and increasingly familiar with the people around it. It has nicknames, relationship tiers, interaction modes, a daily rhythm and mood, a rolling group chronicle and a promise ledger.

Under that social surface is a small agent harness: **code holds durable state, an activation policy decides when to speak, and models generate language inside a deliberately ordered context**. Local commands and configured provider fallbacks keep every interaction from depending on one large model call.

**First use:** [Install](#quick-start), follow the setup wizard, start the bot, then send one private message to claim ownership. To try it without credentials, use `regular demo`.

## What you can try

| Interaction | What is implemented |
| --- | --- |
| A familiar voice | Eight relationship tiers influence tone and activation; code applies scores, nonlinear growth, decay and limits. |
| A different way of getting along with each person | The digest proposes an interaction mode (banter/confidant/defender/hype) from real interactions; a hard whitelist validates it into the archive. |
| A character that can stay quiet | Mention/name triggers, probability, cooldown, quiet hours, mood and affinity control replies. |
| Understand the images people send | A configured vision model captions images; results are reused by message identity under budget and vision limits. |
| Local games and lookups | Dice, relationship queries, rankings and ledger lookup have local handlers. |
| Rename a group member | Per-group identity, owner permissions, name checks and rename cooldowns. |
| Change the atmosphere | Shared personality modes; eligibility is resolved from local state. |
| Remember without replaying everything | Local raw-message archive plus a rolling digest assembled from the old digest and new messages. |
| Keep a provider failure visible and bounded | Configured task tiers, provider fallback, connection recovery and recorded design probes. |

## One message, three possible routes

![Routing, code-owned state and generated replies](docs/readme/flow.svg)

| Example input | Route | What changes |
| --- | --- | --- |
| `查好感` | Local relationship lookup | Reads the current state; it does not ask a model to invent a score. |
| `掷骰子` | Local game handler | Produces a game result and applies the coded relationship effect. |
| `叫我小林` | Naming flow | Checks permissions/name policy, optionally reviews the name, then updates the per-group name record. |
| `@<bot> 今天吃什么` | Generated reply | Builds ordered context, calls the configured provider, checks/records the reply. |
| Ordinary unmentioned chatter | Activation gate | May stay quiet; qualifying messages may trigger a generated interjection. |

These examples describe routes, not exact model replies. A code-owned score prevents the language model from directly rewriting the relationship counter; it does not make every sentence factually reliable.

## The context has an order

The ordering in [get_ai_reply()](bot.py) keeps stable material ahead of changing material:

| Layer | Contents | Why it sits here |
| --- | --- | --- |
| 1 · Stable head | Persona, shared rules, output rules, optional protocol | Shared within the same group/persona configuration. |
| 2 · Group memory | The current rolling digest | Changes when compression refreshes it. |
| 3 · Session history | Accepted conversation turns | Extends the existing prefix. |
| 4 · This turn | Speaker relationship, nearby chatter, owner/private/back-off notes, activation state | Changes by speaker and turn, so it belongs last. |

Session access is locked while a reply is built and recorded. Names are rendered from the current archive rather than left to free-form model memory. Digest extraction can suggest relationship changes; local code still owns validation, caps and persistence.

This layout creates opportunities for prefix caching. Actual cache behavior depends on the provider and task tier: the [design notes](docs/DESIGN_NOTES_EN.md) distinguish live group observations, controlled probes and regressions. Their measurements are evidence for those conditions, not a universal cache-hit or monthly-cost promise.

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

## 📄 License

Open-sourced under the [MIT License](LICENSE).
Pull requests and stars are welcome! Bring a true companion to your chat group.

## Understand a quiet or half-working bot

| Symptom | Check first |
| --- | --- |
| The process starts but group chatter never arrives | QQ sandbox membership and the group's **获取群内全部消息** setting. |
| It never reacts to its spoken name | `BOT_NAMES`; the trigger words must match your deployment. |
| Chat works but owner commands do not | QQ message-list access, C2C permission and the owner's first private message; saved ownership is in `owner.txt`. |
| A nickname appears to be missing in private chat | Nicknames are scoped to a group; C2C has no group identity. |
| Cache cost differs from the notes | Provider, model/task tier, prefix stability and the observed cache TTL. |

## Explore the harness

| File | Responsibility |
| --- | --- |
| [bot.py](bot.py) | QQ event routing, local commands, prompt assembly, output and send handling |
| [activation.py](activation.py) | Activity state, time-of-day and reply policy inputs |
| [affinity.py](affinity.py), [relations.py](relations.py) | Relationship updates, tiers, budgets and ranking |
| [naming.py](naming.py), [nicking.py](nicking.py) | Runtime identity rendering and per-group nicknames |
| [runtime.py](runtime.py), [storage.py](storage.py) | Shared runtime objects, sessions and persistence |
| [archive.py](archive.py), [digest.py](digest.py) | Raw message archive, rolling memory and extracted structured updates |
| [providers.py](providers.py) | Provider routing and fallback |
| [wordfilter.py](wordfilter.py), [qqtext.py](qqtext.py) | Text checks and QQ message rendering |
| [tests/](tests/) | Regression cases and separate provider probes |

Live operation needs your QQ application and model credentials. Message archives and generated content are deployment data; the repository contains the code and example configuration. Network availability, QQ platform limits and model behavior remain external dependencies.
