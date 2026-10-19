<div align="center">

<img src="docs/readme/cover.svg" alt="The Regular project overview" width="100%" />

# The Regular · 老群友

**把关系和记忆留在代码里，让模型负责接话。**

[![Python](https://img.shields.io/badge/Python-3.10%2B-5375B5)](requirements.txt) [![Offline checks](https://github.com/dubianche123/micro-banter-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/dubianche123/micro-banter-harness/actions/workflows/ci.yml) [![QQ](https://img.shields.io/badge/QQ-Group%20%2B%20C2C-B48543)](bot.py) [![License](https://img.shields.io/badge/License-MIT-3A9F82)](LICENSE)

**中文** · [English](README_EN.md)

[运行](#快速开始) · [消息路径](#一条消息三种去向) · [上下文设计](#上下文为什么要分层) · [玩法](#-群里怎么玩)

</div>

老群友是常驻 QQ 群的一个角色：大部分时候潜水，偶尔接一句话，逐渐熟悉群里的人。它有称呼、交情档位、相处模式、作息和心情、滚动《群史记》和承诺账本。

社交表面之下，是一个小型 Agent harness：**代码持有长期状态，激活策略决定什么时候开口，模型在有顺序的上下文里生成语言**。本地指令和可配置的供应商回退，让每次互动不必都依赖一次大模型请求。

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

### 1. 安装并先玩一下

需要 Python 3.10+，推荐 Python 3.12。无需数据库；采用现有运行时和标准 Python 工具。

```bash
git clone https://github.com/dubianche123/micro-banter-harness.git
cd micro-banter-harness
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
regular demo
# 或一次体验几个指令：
regular demo 查好感 掷骰子 群史记
```

交互演示可用 `/user NAME` 切换群友、`/group ID` 切换群，`/help` 查看帮助。

演示使用正式消息处理入口、隔离的临时档案与固定假模型；不连接 QQ，不调用真实模型，退出即清理。它能体验本地指令和关系处理，不代表模型回复质量或 QQ 权限已经验证。

### 2. 初始化配置并体检

```bash
regular init --interactive
regular doctor
```

向导对 AppSecret、API Key 使用隐藏输入，只创建权限为 `0600` 的 `.env`，不会覆盖已有文件。非交互安装可用 `regular init` 生成空凭据配置，再手工编辑；完整选项见 [.env.example](.env.example)。环境变量优先于 `.env`。

必填 QQ `QQ_APP_ID` / `QQ_APP_SECRET`。所有入口默认通用 `AI_PROVIDER=openai`，明确填写 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODELS`；智谱和 Gemini 仍可作为便捷预设。`BOT_NAMES` 填群里真正使用的叫法，例如 `BOT_NAMES=小王,阿王`。`FALLBACK_PROVIDER` 留空关闭跨端点回退。

供应商只使用一套通用有限回退：显式候选、默认最多 3 次尝试、单次 20 秒/总计 30 秒；所有真实尝试计入同一日预算。根据 SDK 异常与 HTTP 状态处理，不根据厂商文案、缓存时长或慢模型特例决定路由。任务模型与独立识图配置见[供应商配置](docs/providers.md)。

机器人尚未认主时，第一位发送非空 C2C 私聊的用户自动成为主人，后台静默写入 `owner.txt`，无需口令，也不额外发送认主提示；这条私聊照常处理指令或聊天。主人保存后不会因其他私聊更换；群消息不能认主。先在 QQ 后台将消息列表使用范围限制给预期主人或测试成员，再由主人先发一句私聊。QQ 的消息列表可设置全量或白名单，详见[官方使用范围说明](https://bot.q.qq.com/wiki/#_10-%E4%BD%BF%E7%94%A8%E8%8C%83%E5%9B%B4%E4%B8%8E%E4%BA%BA%E5%91%98%E9%85%8D%E7%BD%AE)。TLS 校验默认开启。

新群加入或首次出现后先留 10 分钟；若仍无人认主，只在下一次正常聊天回复末尾提醒一次，成功送达后不再重复，不额外定时发消息。群里明确询问认主时则直接指引去私聊。

识图默认开启，使用主供应商及首个对话模型。主模型是纯文本或希望读图用另一款模型时，单独设置 `VISION_PROVIDER` / `VISION_MODEL`；不需要识图则设 `VISION_ENABLED=false`。继承配置不代表模型具备视觉能力；不支持图片时会明确提示读图失败，不猜测图片内容。

`regular doctor` 默认仅检查本地凭据是否存在、依赖、有效路径、状态文件和能力配置，不推理、不联网。`regular doctor --network` 明确允许一次供应商 `/models` 元数据检查；元数据成功不等于所选模型有额度。`--json` 便于附上诊断，输出不包含凭据；分享前仍请检查本机路径和机器人别名。

### 3. QQ 平台设置并启动

1. QQ 开放平台配置应用、沙箱成员及 C2C 权限；将消息列表使用范围限制给预期主人或测试成员。
2. 启动后，主人先给机器人发一句非空私聊，后台静默保存主人身份，消息照常处理。
3. 群设置 → 该机器人 →「机器人可获取的群聊消息范围」→ **获取群内全部消息**。
4. 本地检查无法确认平台开关，需上线后发送实际消息验证。

```bash
regular run
# 另一个终端观察结构化状态（网关、预算、供应商和后台任务）：
regular status
regular status --json
```

指定其他部署目录可用 `regular --directory /path/to/instance doctor`，其 `.env` 与数据均从该目录解析。旧的 `python bot.py` 入口保留。停止前台进程用 Ctrl+C；不要同时运行两个实例。

### 4. 选择常驻方式

macOS 的脚本默认只生成本机路径的 plist，不启动服务、不杀进程、不改变休眠策略，也不注入代理：

```bash
bash tools/launchd/install.sh          # 生成 data/io.github.the-regular.plist，先检查
bash tools/launchd/install.sh install  # 明确安装；仍未启动
bash tools/launchd/install.sh start    # 明确启动，先停止手动运行的实例
bash tools/launchd/install.sh status
# bash tools/launchd/install.sh stop
```

脚本默认使用项目 `.venv/bin/python`，其他环境通过 `REGULAR_PYTHON=/absolute/venv/bin/python` 指定。代理如有需要写在自己的 `.env`，服务也从同一目录读取。

Linux 使用 [systemd 用户服务生成步骤](tools/systemd/README.md)。Docker 是单容器部署，先在宿主机生成并填写 `.env`：

```bash
docker compose build
docker compose run --rm regular regular doctor
docker compose up -d
docker compose exec regular regular status
docker compose logs -f
```

状态、日志、归档与摘要存于 `regular-data` 命名卷，容器以非 root 用户运行；重建容器保留数据。`docker compose down -v` 会删除该卷，请自行备份。QQ 采用出站连接，无需暴露 Web 端口。

### 数据目录

运行数据默认集中在部署目录的 `data/`，包括状态、主人身份、日志、消息底稿和摘要。可用 `DATA_DIR` 指定其他位置；相对路径均从部署目录解析。停止实例后备份数据目录与 `.env` 即可保留配置和运行记录。版本变化见 [CHANGELOG](CHANGELOG.md)。

---

## 🎮 群里怎么玩？

| 玩法 | 怎么说 | 消耗 Token |
| :--- | :--- | :---: |
| **日常聊天** | `@它` 或者直接喊它的名字（如 `小王 今天吃啥`） | 1 次 |
| **随缘插嘴** | 正常在群里聊天，它会按概率看心情接话 | 1 次 |
| **发图** | `@它` 带一张图（它读一次就记住） | 1 次对话 + 最多 1 次读图（按供应商计费） |
| **赛博决斗** | 发送 `掷骰子` / `摇骰子` / `比大小` / `决斗` | **0** |
| **认领/改名** | 发送 `叫我阿强` / `我是阿强` / `别叫我了` | 0~1 次 |
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
