# 0.2 升级说明 / Upgrade to 0.2

本次源码更新日期：2026-10-20。版本号为 `0.2.0`；这份说明不代表已经创建 Git tag、GitHub Release 或发布 Python 包。

## 本轮修复范围

| 类别 | 现在的行为 |
| --- | --- |
| 供应商与预算 | 复用 OpenAI Python SDK，使用显式端点、任务模型链、有界尝试与超时；移除厂商错误文案匹配、缓存让位、慢模型处罚和强制最后重试。聊天、摘要、审核、视觉的每次物理请求共同计入持久化日预算。 |
| 发送与会话 | 只有 QQ 返回正向发送回执，才将实际送达文本记入会话；内容审核替换后的文本也按实际结果记录。重试复用消息序号，新的会话轮次或重置会使旧的未确认回复失效。生成回复附带的认主提示和关系里程碑随对应答复送达后消费。 |
| 图片 | 按来源消息关联并持久化描述，前台与后台共享在途任务和缓存；处理取消、重启去重及全局预算边界。带文字的图片仍保留图片标记。 |
| 摘要与档案 | 只提交实际处理前缀的档案游标，保留晚到和同时间戳消息；无效或不完整的压缩结果不推进摘要。兼容旧时间戳进度。 |
| 关系与承诺 | 闲置衰减重复读取不重复扣分；恢复互动模式和里程碑等字段，按当前配置重算固定身份。承诺使用稳定身份，改名和可唯一匹配的旧版长文本升级保留催促计数。 |
| 模块边界 | 供应商与昵称提供公开入口，删除重复的旧客户端池；发送、图片和健康状态各有共享实现，演示复用正式消息处理器。 |
| 安装与部署 | 提供标准 Python 包、`regular` CLI、便携 launchd/systemd 生成器及单容器 Compose 路径；去掉个人机器路径和代理。新实例集中使用 `data/`，旧档案保留兼容。 |
| 诊断与工程流程 | 增加 `init/demo/doctor/status`、实际任务及网关状态、离线 CI、配置一致性回归、贡献指南和问题模板；同步中英文入门说明。 |

## 升级步骤

### 1. 停止旧实例并备份有效数据

先停止前台实例或原守护服务，保存 `.env`，以及有效配置指向的状态文件、认主文件、原始档案和记忆目录。先确认 `STATE_FILE`、`OWNER_FILE`、`ARCHIVE_DIR`、`MEMORY_DIR` 的覆盖配置，再选择备份位置；不要同时运行两个实例共享这些数据。

旧 macOS 部署可能使用 `com.qqbot.xiaowang` 和 `com.qqbot.keepawake`。确认本机实际使用的旧服务后，在启动新服务前分别卸载：

```sh
launchctl bootout "gui/$(id -u)/com.qqbot.xiaowang"
launchctl bootout "gui/$(id -u)/com.qqbot.keepawake"
```

没有安装过的服务可跳过。旧 plist 如仍放在 `~/Library/LaunchAgents/`，请移出该目录，避免下次登录重新加载。新脚本不会替你卸载旧服务、终止进程或调整休眠策略。

在原虚拟环境中更新源码和依赖：

```sh
git pull --ff-only
python -m pip install -e .
regular --version
```

Python 3.10+ 可用，推荐 Python 3.12。已有 `.env` 不会被 `regular init` 覆盖；升级应直接核对现有文件。

### 2. 明确模型端点与能力

所有入口默认 `AI_PROVIDER=openai`。它表示 OpenAI-compatible 协议，不限定服务商；通用端点须显式配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODELS`。旧实例若只配置了智谱或 Gemini 的 Key，请补上相应的 `AI_PROVIDER=zhipu` 或 `AI_PROVIDER=gemini`。对应厂商预设、模型覆盖和 Key 文件继续支持。

`FALLBACK_PROVIDER=` 留空会关闭跨端点回退。模型顺序必须是自己确认可用的候选；尝试上限不会因为候选多而扩大。默认 `AI_MAX_ATTEMPTS=3`、`AI_TIMEOUT_SECONDS=20`、`AI_TOTAL_TIMEOUT=30`；SDK 自带重试已关闭。`DAILY_BUDGET` 统计所有任务的**物理请求次数**，失败与超时也计数，不是成功答复数或群聊轮数。

下列旧路由变量不再生效，保留时 doctor 会提示；请删除并按实际需求调整显式模型链和上述边界：

- `PROVIDER_FAIL_THRESHOLD`
- `PROVIDER_COOLDOWN_SECONDS`
- `PROVIDER_FATAL_COOLDOWN_SECONDS`
- `PROVIDER_CACHE_WARM_SECONDS`
- `MODEL_SLOW_PENALTY_SECONDS`

视觉现在默认关闭，不再由文本供应商隐式决定。需要读图时显式设置：

```dotenv
VISION_ENABLED=true
VISION_PROVIDER=openai
VISION_MODEL=your-image-capable-model
VISION_INPUT_MODE=url
```

`VISION_PROVIDER` 缺省跟随 `AI_PROVIDER`；`VISION_MODEL` 必须明确指定实际支持图片的模型。若需由本机下载 QQ 图片后发送，可选择 `VISION_INPUT_MODE=download`。完整请求分类、冷却和任务链规则见 [供应商配置与迁移](providers.md)。

### 3. 核对数据、认主与 TLS

新实例的运行数据默认位于部署目录的 `data/`。旧根目录已有 `state.json`、`owner.txt`、`archive/` 或 `memory/` 且没有设置 `DATA_DIR` 时，继续使用旧布局；没有自动移动、合并或删除。

若要迁入新目录，保持实例停止，先备份并复制有效数据，再设置 `DATA_DIR`。显式的 `STATE_FILE`、`OWNER_FILE`、`ARCHIVE_DIR`、`MEMORY_DIR`、`LOG_FILE` 仍优先，且相对部署目录解析，必须一起核对。`regular --directory /path/to/instance ...` 可以选择独立部署目录。

认主默认关闭：要启用，请设置私有 `OWNER_CLAIM_PHRASE`，通过 C2C 私聊认主；已有 `owner.txt` 继续有效。不要把认主口令发到群聊。TLS 验证默认开启，核对旧配置中的 `SSL_VERIFY`，优先修好证书和代理配置。

### 4. 体检后只启动一个实例

```sh
regular doctor
regular demo 查好感 掷骰子
regular run
# 另开终端读取实际运行状态：
regular status
```

`doctor` 默认只做本地检查，不连接模型或 QQ。`doctor --network` 才会查询供应商模型元数据，仍不等于获得模型推理权限或额度。`regular probe` 的真实模型推理必须显式传入 `--allow-paid`。

常驻部署见 [macOS 入门](../README.md#4-选择常驻方式) 和 [systemd 说明](../tools/systemd/README.md)。launchd 脚本默认仅生成文件，安装和启动是独立操作。Docker 配置和持久卷说明见 [README](../README.md)。QQ 的全量群消息开关、沙箱成员、C2C 权限需在平台侧配置并用真实流量确认。

## 发送重试的边界

补投队列是**进程内、有界、尽力而为**：仅在 QQ 的被动回复窗口内重试，过期或队列淘汰不会写入会话，进程重启不恢复待发送队列。同一会话开始新一轮或执行重置后，旧的未确认答复会被替代，不再补投或提交。正向回执是本地提交依据；传输中断导致平台已接受但本地未收到回执的情况，仍有外部系统的不确定性，不承诺端到端恰好一次送达。

## 本轮验证与剩余外部验证

本地 Python 3.12 的离线回归为 **610/610 通过**；另有 **21 项产品检查**和从源码目录外安装 wheel 的 **9 步冒烟验证**。离线检查使用假模型及隔离数据，覆盖安装、配置、消息流程与故障路径，不消耗真实模型额度。

真实 QQ 收发及权限联调、付费模型 probe 和 Docker 镜像实际构建尚未执行。CI 工作流已纳入源码；本地结果不能代替 GitHub Actions 的远端运行结果，请以仓库 [Actions](https://github.com/dubianche123/micro-banter-harness/actions) 中对应提交为准。Git tag、GitHub Release 和包上传属于单独的发布操作。

## English migration essentials

Stop the old process/service and back up the effective configuration and data before upgrading. Remove old launchd jobs from automatic loading before starting the new one. Keep one instance per data directory.

Explicitly set `AI_PROVIDER`; old Zhipu/Gemini deployments that omitted it must opt into their preset. The default is a generic OpenAI-compatible endpoint with explicit URL, Key and models. Vision is disabled by default and requires an explicit image-capable model. Remove the retired routing variables listed above. Every physical chat/digest/judge/vision attempt, including failures, consumes the same daily budget.

Existing root-level data stays in place when `DATA_DIR` is unset; explicit per-file paths still take precedence. Owner claiming is opt-in, existing ownership remains valid, and TLS verification defaults to enabled. Run `regular doctor` before restarting. The resend queue is bounded and in-memory: a new turn/reset supersedes old unconfirmed replies, and restarts do not restore them.

Local Python 3.12 validation passed 610 offline tests, 21 product checks and a nine-step installed-wheel smoke. Live QQ, paid inference and an actual Docker build remain unverified; check Actions separately for remote CI. This source update does not itself create a release or upload a package.
