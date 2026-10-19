<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular

**A resident group-chat companion with code-owned relationships and memory.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

[中文](README.md) · **English**

[Run it](#quick-start) · [Message routes](#one-message-three-possible-routes) · [Context design](#the-context-has-an-order) · [Commands](#-cheatsheet-commands--interactions)

</div>

The Regular lives in a QQ group as a recurring character: usually quiet, occasionally opinionated, and increasingly familiar with the people around it. It has nicknames, relationship tiers, personality modes, a rolling group chronicle and a promise ledger.

Under that social surface is a small agent harness: **code holds durable state, an activation policy decides when to speak, and models generate language inside a deliberately ordered context**. Local commands and configured provider fallbacks keep every interaction from depending on one large model call.

## What you can try

| Interaction | What is implemented |
| --- | --- |
| A familiar voice | Eight relationship tiers influence tone and activation; code applies scores, decay and limits. |
| A character that can stay quiet | Mention/name triggers, probability, cooldown, quiet hours and activation state control replies. |
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

### 1. Clone & Install Dependencies

Requires Python 3.10+. The project is ultra-lightweight: **no databases, no heavy frameworks**, just 5 lean dependencies:

```bash
git clone https://github.com/dubianche123/micro-banter-harness.git
cd micro-banter-harness

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy the template:

```bash
cp .env.example .env
```

Open `.env` and fill in the essential credentials:

```env
# 1. Tencent QQ Open Platform Credentials
QQ_APP_ID=your_app_id
QQ_APP_SECRET=your_app_secret

# 2. What the bot is called in your group (Trigger words, comma-separated)
BOT_NAMES=Buddy,Alex,@Buddy

# 3. Secret phrase to claim ownership in private chat
OWNER_CLAIM_PHRASE=your_secret_passphrase_here

# 4. Primary AI Provider (gemini or zhipu) and Key
AI_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_key
# ZHIPU_API_KEY=your_fallback_zhipu_key (optional)
```

### 3. QQ Platform & Group Settings (Two Important Steps)

1. **Enable All Group Messages**: In QQ Group Settings $\to$ Bot Settings $\to$ set message scope to **"Obtain all messages in group" (获取群内全部消息)**. This is required for the bot to observe context and chime in.
2. **Enable Direct Message (C2C) Permissions**: In the QQ Open Platform console, enable C2C direct message permissions for safe ownership claim and 1-on-1 chatting.

### 4. Run & Daemonize

```bash
# Foreground run
python bot.py

# Or background run (with proxy if needed):
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 \
NO_PROXY=bigmodel.cn,open.bigmodel.cn nohup python -u bot.py >> bot.log 2>&1 &
```

> 🍏 **For macOS users**: Comes with a one-click launchd daemon installer (keeps process alive and prevents machine sleep):
> ```bash
> bash tools/launchd/install.sh
> ```

### 5. Claim Group Ownership via DM

After launching, the owner sends `OWNER_CLAIM_PHRASE` in a **private DM to the bot**. Once verified, the bot permanently binds your OpenID, locks your affinity to maximum, and grants exclusive admin commands.

---

## 🎮 Cheatsheet: Commands & Interactions

| Feature | How to Say | Token Cost |
| :--- | :--- | :---: |
| **Chat** | `@bot` or say its name directly (e.g. `Buddy what's good`) | 1 call |
| **Random Banter** | Talk normally in the group; it chimes in based on probability | 1 call |
| **Cyber Duel** | `roll dice` / `duel` / `掷骰子` / `比大小` | **0** |
| **Claim / Rename** | `call me Alex` / `叫我阿强` / `别叫我了` | 0~1 call |
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

* `BANTER_PROBABILITY`: Probability of random banter (default `0.08` or 8%)
* `OWNER_MENTION_PROBABILITY`: Banter chance when owner is mentioned (default `0.25`)
* `BANTER_COOLDOWN_SECONDS`: Minimum cooldown between banter replies (default `120`s)
* `AI_DAILY_BUDGET`: Max daily LLM calls for the whole group (default `3000`, prevents wallet drain)
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
| Chat works but owner commands do not | C2C permission and the private ownership-claim step. |
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
