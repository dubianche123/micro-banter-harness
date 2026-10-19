<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular · 老群友 — QQ AI 群聊机器人

**把关系和记忆留在代码里，让模型负责接话。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![Offline checks](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/dubianche123/the-regular-qq-bot/actions/workflows/ci.yml) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

**中文** · [English](README_EN.md) · [下载 v0.2.0](https://github.com/dubianche123/the-regular-qq-bot/releases/tag/v0.2.0)

[运行](#快速开始) · [消息路径](#一条消息三种去向) · [上下文设计](#上下文为什么要分层) · [玩法](#-群里怎么玩)

</div>

老群友是常驻 QQ 群的一个角色：大部分时候潜水，偶尔接一句话，逐渐熟悉群里的人。它有称呼、交情档位、相处模式、作息和心情、滚动《群史记》和承诺账本。

社交表面之下，是一个小型 Agent harness：**代码持有长期状态，激活策略决定什么时候开口，模型在有顺序的上下文里生成语言**。本地指令和可配置的供应商回退，让每次互动不必都依赖一次大模型请求。

**第一次使用：** [安装](#快速开始)后跟随配置向导启动，再由主人私聊一句完成自动认主。只想体验可先运行 `regular demo`。

## 可以玩到什么

| 互动 | 当前实现 |
| --- | --- |
| 越熟越有自己的语气 | 八档关系影响语气和激活；算分、非线性增长、衰减与上限由代码执行。 |
| 跟不同的人有不同的相处方式 | 压缩按真实互动提议相处模式（损友/知心/护短/捧场），白名单硬校验后写入档案。 |
| 知道什么时候安静 | 提及/名字触发、概率、冷却、作息、心情与亲密度共同控制回复。 |
| 看懂群友发的图 | 图片由配置的视觉模型描述，按消息标识复用，受预算与独立额度控制。 |
| 本地小游戏与查档 | 骰子、好感查询、排行榜和账本查询有本地处理器。 |
| 认领或修改称呼 | 按群隔离身份，结合群主权限、名字检查和修改冷却。 |
| 切换全群气氛 | 公共人格模式，切换资格由本地状态决定。 |
| 记住群聊而不重放全部历史 | 本地消息底稿与“旧摘要 + 新消息”的滚动摘要。 |
| 观察供应商故障与恢复 | 按任务分层、供应商回退、连接恢复，并保留设计与测试记录。 |

## 一条消息，三种去向

![消息路由、代码状态与生成回复](docs/readme/flow-cn.svg)

| 输入示例 | 路径 | 状态怎样变化 |
| --- | --- | --- |
| `查好感` | 本地关系查询 | 读取已有状态，不让模型临时编一个分数。 |
| `掷骰子` | 本地游戏处理器 | 生成游戏结果，按规则结算相应交情变化。 |
| `叫我小林` | 称呼处理流程 | 检查权限与名字规则，必要时审核，再更新按群隔离的称呼记录。 |
| `@机器人 今天吃什么` | 生成回复 | 组装分层上下文，调用已配置的供应商，检查并记录回复。 |
| 没有提及机器人的普通聊天 | 激活门控 | 可以保持安静；满足条件时才生成一次接话。 |

这里展示的是执行路径，不是模型的固定台词。代码持有分数，意味着语言模型不会直接改写关系计数器；这不等于每一句生成文本都事实可靠。

## 上下文为什么要分层

[get_ai_reply()](bot.py) 把越稳定的材料放得越靠前：

| 层次 | 内容 | 为什么放这里 |
| --- | --- | --- |
| 1 · 稳定头 | 人设、共享规则、输出规则、可选协议 | 同群、同人格配置下尽量复用。 |
| 2 · 群长期记忆 | 当前滚动摘要 | 完成一次压缩后才更新。 |
| 3 · 会话历史 | 已接纳的对话轮次 | 沿着已有前缀追加。 |
| 4 · 本轮动态 | 发言人关系、附近聊天、群主/私聊/喊停提示、激活状态 | 随人和轮次变化，统一压到末尾。 |

构造回复与写回历史期间持有会话锁，避免并发撕裂。称呼从当前档案渲染，关系变化可以由摘要提取建议，但校验、封顶与持久化仍由本地代码完成。

这样的顺序为前缀缓存创造条件；实际效果取决于供应商与任务档位。[设计笔记](docs/DESIGN_NOTES.md) 区分了真实群聊观察、受控探针和回归检查，其中的测量只对应各自条件，不能当成通用命中率或月费承诺。

## 快速开始

### 1. 安装发布包

安装一次 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后复制这一行：

```bash
uv tool install --python 3.12 https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl
```

uv 会准备 Python 3.12、依赖和独立环境。无需克隆仓库或手动激活虚拟环境。若终端找不到 `regular`，运行 `uv tool update-shell` 后重开终端。

想先试用，也可以直接运行：

```bash
uvx --python 3.12 --from https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl regular demo
```

演示无需账号或 Key，不连接 QQ，不调用真实模型。可体验 `查好感`、`掷骰子`、`群史记`，`/help` 查看帮助。已安装后也可直接用 `regular demo`。

<details>
<summary>已有 Python，使用 pip 安装</summary>

需要 Python 3.10+，推荐 3.12。在虚拟环境中安装同一个 wheel：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install https://github.com/dubianche123/the-regular-qq-bot/releases/download/v0.2.0/the_regular-0.2.0-py3-none-any.whl
```

Windows 使用 `python` 代替 `python3`，激活命令为 `.\.venv\Scripts\Activate.ps1`。后续配置与运行命令相同。

</details>

<details>
<summary>开发或修改源码</summary>

```bash
git clone https://github.com/dubianche123/the-regular-qq-bot.git
cd the-regular-qq-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

</details>

### 2. 跟向导填一次配置

准备 QQ 应用的 AppID、AppSecret，以及模型服务的 URL、API Key 和模型名，然后运行：

```bash
regular --directory regular-bot setup
```

`regular-bot` 是你的本地实例目录，向导会自动创建；也可换成自己的绝对路径。向导隐藏密钥输入，创建私有 `.env`，并自动做本地检查。已有 `.env` 时只补空缺，保留已填内容；智谱、Gemini 预设只需对应的 Key。配置完整后会直接给出启动命令。无需手工设置数据库、重试策略或识图配置。

默认识图跟随主模型；主模型只支持文本时，可另设 `VISION_MODEL`，跨供应商再设 `VISION_PROVIDER`。`BOT_NAMES` 是可选的群内别名，留空仍可用平台昵称或 `@` 触发。[更多模型配置](docs/providers.md) · [全部配置示例](.env.example)

### 3. 启动，私聊一句认主

QQ 后台开启 C2C、所需群事件，并将使用范围设为预期主人或测试成员。然后启动：

```bash
regular --directory regular-bot run
```

日志显示机器人“已更新上线”后，QQ 连接才算成功。凭据或权限被拒绝会停止启动；首次上线最多等待 60 秒，连续连接失败也会提前退出，并指明需要核对的配置。修正实例目录内的 `.env` 后重新运行上面的命令。

**启动后，主人先私聊机器人说一句话，就自动认主。** 无需口令、没有额外确认，原消息照常回复；主人身份会保存，重启后也保留，其他私聊不会更换主人。QQ 平台支持全量或白名单范围，首次使用前请确认[后台使用范围](https://bot.q.qq.com/wiki/#_10-%E4%BD%BF%E7%94%A8%E8%8C%83%E5%9B%B4%E4%B8%8E%E4%BA%BA%E5%91%98%E9%85%8D%E7%BD%AE)。

拉进群后，在群设置 → 该机器人 → 消息范围中选 **获取群内全部消息**，再 `@它` 试聊。入群 10 分钟仍未认主，会随下一条正常回复提醒一次。群消息本身不会绑定主人。

遇到问题运行 `regular --directory regular-bot doctor`；另一个终端用 `regular --directory regular-bot status` 查看连接与预算。两者默认只读本地状态。数据和配置均保存在所选实例目录下。前台运行用 Ctrl+C 停止，同一实例只启动一份。

<details>
<summary>可选：脚本部署与诊断</summary>

以下命令省略 `--directory` 时，请先进入实例目录。

无需问答时，`regular init` 生成空凭据模板，填好后执行 `regular doctor`。`init` 永不覆盖已有文件；补配用 `regular setup`。真实环境变量优先于 `.env`，包括显式设为空的变量。

`regular doctor --json` 输出结构化本地检查；`regular doctor --network` 额外检查供应商 `/models` 元数据，不调用模型，元数据成功也不代表模型额度或图片能力。`regular status --json` 读取运行中的状态快照。

</details>

<details>
<summary>可选：后台常驻与 Docker</summary>

安装包内已包含服务配置生成器；它使用当前工具环境的 Python 和你指定的实例路径，不安装或启动服务。

macOS：先生成并查看 plist，再自行加载：

```bash
regular --directory regular-bot service launchd --output regular-bot/data/io.github.the-regular.plist
mkdir -p ~/Library/LaunchAgents
cp regular-bot/data/io.github.the-regular.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.the-regular.plist
```

Linux：生成用户服务后加载：

```bash
regular --directory regular-bot service systemd --output regular-bot/data/the-regular.service
mkdir -p ~/.config/systemd/user
cp regular-bot/data/the-regular.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now the-regular
```

先停止同一实例的前台进程，再启动后台服务。代理等设置放在实例自己的 `.env` 中。

Docker 和源码辅助脚本随[源码包](https://github.com/dubianche123/the-regular-qq-bot/releases/tag/v0.2.0)提供；Docker 步骤在解压后的源码目录执行：

```bash
regular init
# 填写当前目录的 .env
docker compose build
docker compose run --rm regular regular doctor
docker compose up -d
docker compose logs -f
```

容器的配置来自源码目录 `.env`，数据存于 `regular-data` 命名卷。`docker compose down -v` 会删除该卷。

### 数据目录

每个实例的 `.env` 位于实例根目录，状态、主人身份、日志、消息底稿和摘要默认放在该实例的 `data/` 下。可用 `DATA_DIR` 自定数据位置；相对路径从实例目录解析。停止实例后备份 `.env` 和数据目录即可。QQ 使用出站连接，无需暴露 Web 端口。

</details>

---

## 🎮 群里怎么玩？

| 玩法 | 怎么说 | 消耗 Token |
| :--- | :--- | :---: |
| **日常聊天** | `@它` 或者直接喊它的名字（如 `小王 今天吃啥`） | 1 次 |
| **随缘插嘴** | 正常在群里聊天，它会按概率看心情接话 | 1 次 |
| **发图** | `@它` 带一张图（它读一次就记住） | 1 次对话 + 最多 1 次读图（按供应商计费） |
| **赛博决斗** | 发送 `掷骰子` / `摇骰子` / `比大小` / `决斗` | **0** |
| **自报称呼/改名** | 发送 `叫我阿强` / `我是阿强` / `别叫我了` | 0~1 次 |
| **切换人格** | 发送 `猫娘模式` / `发疯模式` / `风纪委员模式` / `算命模式` / `老司机模式` | **0** |
| **恢复正常** | 发送 `恢复正常` / `正常点` / `冷静点` / `吃药了` | **0** |
| **查好感度** | 发送 `查好感` / `我跟你多熟` / `几档了` | **0** |
| **关系排行榜**| 发送 `关系榜` / `点名册` / `群友榜` | **0** |
| **群史记简报**| 发送 `群史记` / `最近聊了啥` / `周报` | **0** |
| **查承诺账本**| 发送 `查账` / `谁请客`；兑现后说 `结清` | **0** |
| **翻旧账查证**| 发送 `翻旧账 关键词`（从本地底稿检索真实发言） | **0** |
| **群主特权** | `@某人 叫他阿珍` / `锁起名` / `解锁起名` / `办他` / `护驾` | **0** |

---

## ⚙️ 常见配置项速览

在 `.env` 中可按需调整机器人的脾气与成本闸门：

* `BANTER_PROBABILITY`：潜水插嘴概率（默认 `0.12` 即 12%）
* `OWNER_MENTION_PROBABILITY`：聊到群主时的插嘴概率（默认 `0.30`）
* `BANTER_COOLDOWN_SECONDS`：插嘴冷却时间（默认 `120` 秒）
* `DAILY_BUDGET`：每天全群 AI 调用上限总预算（默认 `3000` 次，防刷爆钱包）
* `MODE_SWITCH_MIN_AFFINITY`：模式切换门槛（默认熟人 `familiar` 起步，防路人捣乱）

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源。
欢迎 Fork、Star 和贡献代码！让每一个群都拥有一个有血有肉的老群友。

## 不回应或只工作了一半时，看哪里

| 现象 | 优先检查 |
| --- | --- |
| 进程启动了，却看不到群聊 | QQ 沙箱成员与群设置里的 **获取群内全部消息**。 |
| 喊名字也不接话 | `BOT_NAMES` 是否配置成当前部署的触发词。 |
| 聊天正常，群主命令不生效 | QQ 消息列表范围、C2C 权限，以及主人是否已首次私聊认主；主人身份保存在 `owner.txt`。 |
| 私聊里好像找不到群内称呼 | 称呼按群保存；C2C 没有群身份。 |
| 缓存成本和笔记差很多 | 供应商、模型/任务档位、前缀稳定性与观测到的缓存 TTL。 |

## 从代码看 harness

| 文件 | 职责 |
| --- | --- |
| [bot.py](bot.py) | QQ 事件路由、本地指令、提示词组装、输出与发送处理 |
| [activation.py](activation.py) | 激活状态、作息与回复策略输入 |
| [affinity.py](affinity.py)、[relations.py](relations.py) | 交情结算、档位、预算与排行 |
| [naming.py](naming.py)、[nicking.py](nicking.py) | 运行时身份渲染与按群隔离的称呼 |
| [runtime.py](runtime.py)、[storage.py](storage.py) | 共享运行时对象、会话与持久化 |
| [archive.py](archive.py)、[digest.py](digest.py) | 消息底稿、滚动记忆与结构化更新提取 |
| [providers.py](providers.py) | 供应商路由与回退 |
| [wordfilter.py](wordfilter.py)、[qqtext.py](qqtext.py) | 文本检查与 QQ 消息格式渲染 |
| [tests/](tests/) | 回归场景与独立的供应商探针 |

实际运行需要你自己的 QQ 应用和模型凭据。消息底稿与生成内容属于部署数据；仓库提供代码和示例配置。网络、QQ 平台限制与模型行为仍是外部依赖。
