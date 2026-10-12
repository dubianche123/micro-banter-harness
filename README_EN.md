# The Regular 🍺

**English** · [中文](README.md)

> **A resident AI buddy living in your group chat — not a robotic assistant.**

It does not race to answer, does not act like a corporate bot, and certainly never says *"How may I assist you today?"*. Most of the time it lurks quietly, occasionally chiming in with a sharp, witty one-liner when the vibe is right. It knows who its close buddies are, keeps its distance from strangers, and secretly jots down in a ledger who owes the group a cup of milk tea...

**Code holds the state. The model only puts the state into words.**

---

## ✨ Why It's Fun

### 🎭 1. Hangs Out Like a Real Group Member
* **Contextual Banter**: It lurks most of the time. Default 8% chance to drop a comment into ongoing chatter; bumps to 25% when the group owner is mentioned.
* **Reads the Room & Reads the Person**: The closer you are, the more eager it is to banter with you (1.6× affinity weight). It stays polite with strangers (0.7×), and goes to sleep during quiet hours (23:00–08:00).
* **Knows When to Chill**: If someone says "stop roasting me" or "chill out", it immediately dials back. No relentless arguing.

### 💖 2. 8-Tier Affinity & Bond System
Affinity is an integer score (-100 to +100) managed entirely by local code. The model never touches the numbers, so it **can never hallucinate past interactions that never happened**:
* `💀 Blacklist` $\to$ `❄️ Cold` $\to$ `😐 Distant` $\to$ `👀 Familiar`
* `🙂 Acquaintance` $\to$ `😎 Friend` $\to$ `🍻 Buddy` $\to$ `💖 Soulmate`

*Close friends get teasing and loyal backup; strangers get polite distance. To a stranger it might say "Understood, take care", while to a buddy it says "Not this again... fine, I got your back this time."*

### 🎪 3. 5 Personality Modes on the Fly
A shared group state switchable with a single command (only close friends and the owner can command it!):
* 🐱 **Catgirl Mode**: Tsundere feline begging for treats.
* 👮 **Hall Monitor Mode**: Strict disciplinarian busting slackers.
* 💼 **Crazy / Burnt-out Mode**: Late-night overtime worker venting corporate rage.
* 🔮 **Fortune Teller Mode**: Mystical charlatan reading your horoscope.
* 🏎️ **Night Driver Mode**: Easygoing veteran highway cruiser.

### 🧾 4. Group Chronicles & Promise Nagging
* **Rolling Group Memory**: Automatically compresses chat logs daily into a hilarious 《Group Chronicles》 digest. It injects a stable roster of members rather than raw gossip transcripts, completely eliminating stale catchphrase loops.
* **Promise Ledger**: Did someone brag about "treating everyone to drinks tomorrow"? It logs the promise into its ledger and periodically nags them in the group until they say "settled".

### 🎲 5. 0-Token Instant Local Easter Eggs
Most casual interactions **never hit the LLM, cost 0 tokens, and respond in 0 milliseconds**:
* **Cyber Duel**: Roll dice against each other — winning or losing actually shifts your affinity score!
* **Local In-Jokes**: Triggers preset memes instantly on keyword matches.
* **Status Lookups**: Check affinity level, view relationship leaderboard, inspect name aliases, or search raw quote archives.

---

## 🛠️ Secret Sauce Under the Hood

Running 24/7 in an active chat group without crashing, getting rate-limited, or getting banned requires serious defensive engineering:

| Mechanism | Implementation | Benefit |
| :--- | :--- | :--- |
| **Code-Held State** | Affinity, nicknames, and promise ledgers live in code/JSON | Models can't hallucinate numbers; renamed old names are purged before prompt injection |
| **98% Cache Hit Rate** | Physical prompt ordering (Static rules $\to$ Daily memory $\to$ History $\to$ Turn context) | Maximize LLM prefix caching; runs in active groups for pennies a month |
| **Task-Tier Routing** | Chat uses strong models, compression uses weak models, safety uses strict models | Compression bypasses strict content filters; seamless Gemini/GLM failover based on cache TTL |
| **Anti-Griefing Armor** | Dual-tier word filter + model judge + rename rate-limiter | Resists malicious naming attacks; group owner claimed via private secret phrase |
| **24/7 Gateway Watchdog** | Monkey-patches WS downlinks to kick stale silent connections | Solves upstream bot SDK silent freezing bugs during network hiccups |

> 💡 *Interested in the deep architecture, benchmark probes, and 474 offline regression tests? Check out [Architecture & Design Notes](docs/DESIGN_NOTES_EN.md).*

---

## 🚀 Quick Start in 5 Minutes

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
