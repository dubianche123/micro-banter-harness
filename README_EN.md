<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular

**A resident group-chat companion with code-owned relationships and memory.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![Offline checks](https://github.com/dubianche123/micro-banter-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dubianche123/micro-banter-harness/actions/workflows/ci.yml) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

[中文](README.md) · **English**

[Run it](#quick-start) · [Message routes](#one-message-three-possible-routes) · [Context design](#the-context-has-an-order) · [Commands](#-cheatsheet-commands--interactions)

</div>

The Regular lives in a QQ group as a recurring character: usually quiet, occasionally opinionated, and increasingly familiar with the people around it. It has nicknames, relationship tiers, interaction modes, a daily rhythm and mood, a rolling group chronicle and a promise ledger.

Under that social surface is a small agent harness: **code holds durable state, an activation policy decides when to speak, and models generate language inside a deliberately ordered context**. Local commands and configured provider fallbacks keep every interaction from depending on one large model call.

**First use:** [Install](#quick-start), run `regular setup` and `regular run`, then send one private message to claim ownership. To try it without credentials, use `regular demo`.

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

### 1. Install

Python 3.10+ is required; Python 3.12 is recommended. No database is needed.

```bash
git clone https://github.com/dubianche123/micro-banter-harness.git
cd micro-banter-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

To try it first, run `regular demo`. No account or API key is needed; it uses
temporary state and a fixed fake model, with no QQ connection or model requests.
Try `查好感`, `掷骰子`, or `群史记`; `/help` lists demo controls.

### 2. Configure once with the wizard

Have your QQ AppID/AppSecret and model endpoint URL, API key and model ID ready:

```bash
regular setup
```

The wizard hides secret input, creates a private `.env`, and runs local checks.
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
regular run
```

**The intended owner's first private message automatically claims the bot.**
No phrase or extra confirmation is needed; that message receives its normal
reply. Ownership persists across restarts and later private messages cannot
replace it. QQ supports full or allowlist access, so check the
[usage scope](https://bot.q.qq.com/wiki/#_10-%E4%BD%BF%E7%94%A8%E8%8C%83%E5%9B%B4%E4%B8%8E%E4%BA%BA%E5%91%98%E9%85%8D%E7%BD%AE)
before first use.

After adding the bot to a group, choose **获取群内全部消息** in its message scope
under group settings, then mention it. If still unowned after 10 minutes, its
next normal reply includes one reminder. Group messages cannot claim ownership.

Use `regular doctor` for local diagnostics and `regular status` in another
terminal for connection and budget status. For a separate instance, use
`regular --directory /path/to/instance setup`; its configuration and data live
there. Stop foreground execution with Ctrl+C. Run one process per instance.

<details>
<summary>Optional: scripted configuration and diagnostics</summary>

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

The macOS helper generates a plist using your actual paths. It never starts a
service by default, kills processes, injects a proxy or changes power settings:

```bash
bash tools/launchd/install.sh          # Generate data/io.github.the-regular.plist; inspect it
bash tools/launchd/install.sh install  # Explicitly install; do not start yet
bash tools/launchd/install.sh start    # Explicitly start; stop the manual instance first
bash tools/launchd/install.sh status
# bash tools/launchd/install.sh stop
```

It uses `.venv/bin/python` by default; set `REGULAR_PYTHON=/absolute/venv/bin/python`
for another environment. Configure any required proxy in your own `.env`; the
service reads the same deployment configuration.

Linux users can follow the [systemd user-service steps](tools/systemd/README.md).
The Docker path uses one container; first initialize/fill `.env` on the host:

```bash
docker compose build
docker compose run --rm regular regular doctor
docker compose up -d
docker compose exec regular regular status
docker compose logs -f
```

State, logs, archives and memory use the `regular-data` named volume; the container
runs as a non-root user. Recreating it retains the volume. `docker compose down -v`
deletes it, so keep your own backups. QQ uses outbound connections; no web port is
exposed.

### Runtime data

Runtime state, owner identity, logs, archives and summaries default to the
deployment's `data/` directory. Set `DATA_DIR` to choose another location;
relative paths are resolved from the deployment directory. Stop the instance
before backing up the data directory and `.env`. See [CHANGELOG](CHANGELOG.md)
for version changes.


</details>

---

## 🎮 Cheatsheet: Commands & Interactions

| Feature | How to Say | Token Cost |
| :--- | :--- | :---: |
| **Chat** | `@bot` or say its name directly (e.g. `Buddy what's good`) | 1 call |
| **Random Banter** | Talk normally in the group; it chimes in based on probability | 1 call |
| **Send an image** | `@bot` with a picture (it reads it once and remembers) | 1 chat + up to 1 vision call (provider pricing applies) |
| **Cyber Duel** | `roll dice` / `duel` / `掷骰子` / `比大小` | **0** |
| **Nicknames / Rename** | `call me Alex` / `叫我阿强` / `别叫我了` | 0~1 call |
| **Switch Mode** | `catgirl mode` / `crazy mode` / `discipline mode` / `fortune mode` | **0** |
| **Reset Mode** | `return to normal` / `恢复正常` / `冷静点` | **0** |
| **Check Affinity**| `check affinity` / `查好感` / `我跟你多熟` | **0** |
| **Leaderboard** | `relationship board` / `关系榜` / `点名册` | **0** |
| **Chronicles** | `group digest` / `群史记` / `最近聊了啥` | **0** |
| **Promise Ledger**| `check debts` / `谁请客` ; after fulfilling: `settled` / `结清` | **0** |
| **Verify Archives**| `lookup <keyword>` / `翻旧账 关键词` (pulls real quotes) | **0** |
| **Owner Admin** | `@someone call him Bob` / `lock renames` / `unlock renames` | **0** |

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
