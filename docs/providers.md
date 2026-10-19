# 供应商配置与路由 / Provider configuration

共享版直接使用现有 OpenAI Python SDK。`openai` 表示 OpenAI-compatible 协议入口；它不限定服务商，URL、模型和凭据由部署者明确提供。智谱与 Gemini 是可修改的便捷预设，预设中的模型名不代表任何账号具备额度或视觉能力。

## 配置一个通用端点

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://your-endpoint.example/v1
OPENAI_API_KEY=your-private-key
OPENAI_MODELS=chat-model
# 可选；省略时复用该端点的对话模型
OPENAI_MODELS_DIGEST=digest-model
OPENAI_MODELS_JUDGE=review-model
FALLBACK_PROVIDER=
AI_MAX_ATTEMPTS=3
AI_TIMEOUT_SECONDS=20
AI_TOTAL_TIMEOUT=30
```

`*_MODELS` 接受逗号分隔的有序列表，重复名称自动去重。只填自己确认可用的模型。`FALLBACK_PROVIDER` 可显式选择 `openai`、`zhipu` 或 `gemini`；留空关闭跨端点回退。主端点的候选用完或该端点需要跳过时，才进入备用端点，且共同受尝试上限约束。使用其他 endpoint 或 key pool 可运行自己已有的兼容网关，项目不另建网关框架。

老的 `AI_PROVIDER=zhipu/gemini`、对应 `*_API_KEY`、`*_BASE_URL`、`*_MODELS*` 及 `api_keys_<provider>.txt` 继续支持。所有入口默认 `AI_PROVIDER=openai`，不隐式选厂商。旧部署若只填智谱 Key、未设置 `AI_PROVIDER`，升级时须补 `AI_PROVIDER=zhipu`；doctor 会显示有效端点并提示这种迁移。

## 每次调用的边界

- 一次 `call_model` 最多 `AI_MAX_ATTEMPTS` 次物理模型请求；默认 3 次。候选顺序来自当前任务配置，摘要不会悄悄切到另一套对话模型。
- 每次请求最多 `AI_TIMEOUT_SECONDS`，整次调用最多 `AI_TOTAL_TIMEOUT`，默认分别 20 秒和 30 秒。
- SDK 设为 `max_retries=0`。所有聊天、摘要、审核及读图都在唯一请求入口消费同一个日预算；失败、超时和调用方显式重试也计数。
- 不重复提交同一候选。失败只可能进入下一个明确配置的模型/端点。没有最终 `content` 时，不把 `reasoning_content` 作为答复。

分类复用 SDK 的异常类型和 HTTP 状态，不识别厂商错误文案：

| 结果 | 行为 |
| --- | --- |
| 401 / 403 | 本进程停用该端点，不盲试同端点其他 Key/模型；只允许显式备用。修好配置后重启。 |
| 429 | 按 `Retry-After` 暂停该端点；没有有效响应头时短暂停 1 秒。只走显式备用，不在请求里睡眠。 |
| 带 `Retry-After` 的 503 | 同样按响应头暂停端点。支持秒数和 HTTP 日期。 |
| 404、408、409、其余 5xx | 尝试下一个明确候选，仍受总尝试数和超时约束。 |
| SDK 连接错误、超时 | 本轮跳过该端点，允许显式备用；下一次调用可正常重试。 |
| 其他 4xx 或未知异常 | 停止本次调用，避免把错误输入继续提交到别处。 |

冷却中的端点不会被“最后一搏”强行调用。候选多于尝试上限时，后面的候选未必会被访问；需要给备用留机会时，应缩短前面的候选列表。`regular doctor` 展示有效链、尝试上限、超时和已配置能力，默认不发网络请求。

## 视觉模型独立声明

```dotenv
VISION_ENABLED=true
VISION_PROVIDER=openai
VISION_MODEL=your-image-capable-model
VISION_INPUT_MODE=url
```

`VISION_PROVIDER` 可选任一配置端点。`VISION_MODEL` 必须对应支持图片的模型；不能从文本模型名或 `/models` 列表推断。所有入口默认关闭视觉，`VISION_PROVIDER` 缺省跟随当前 `AI_PROVIDER`，`VISION_MODEL` 默认为空。旧部署依赖隐式读图时，须明确填 `VISION_ENABLED=true`、实际视觉端点及模型（例如确认可用后选择智谱 `glm-4v-flash`）；开启却未填模型会给出配置错误，doctor 展示有效能力。

`url` 将图片 URL 直接传给模型。若 QQ 图床只能由本机访问，可显式设 `VISION_INPUT_MODE=download`：先下载，再以 base64 发送一次模型请求。没有“报某段错误后换输入再提交”的自动重试。同图前后台共用在途任务与已保存描述，不重复占图片名额；前台新图在全局预算只剩一次时跳过读图，保留一次正文调用机会。

## 旧路由策略迁移

缓存让位、慢模型处罚、根据错误文案判定地区/凭据故障、强制最后重试已经移除，不保留第二套 legacy 执行路径。以下旧变量不再生效；仍设置时 doctor 会明确提示：

`PROVIDER_FAIL_THRESHOLD`、`PROVIDER_COOLDOWN_SECONDS`、`PROVIDER_FATAL_COOLDOWN_SECONDS`、`PROVIDER_CACHE_WARM_SECONDS`、`MODEL_SLOW_PENALTY_SECONDS`。

升级时删除这些变量，核对真实需要的模型顺序、`FALLBACK_PROVIDER` 和三个调用边界参数。稳定提示词前缀仍然保留，但路由不假设某家的缓存寿命、免费配额或错误文案。

## 复用了什么

[OpenAI Python SDK 的异常和重试接口](https://github.com/openai/openai-python#handling-errors) 已提供 typed errors、状态码、超时及关闭自动重试的配置，本项目只保留与日预算和任务候选相关的薄边界。[LiteLLM 的重试和回退](https://docs.litellm.ai/docs/completion/reliable_completions) 适合需要更广供应商协议及集中路由的场景；当前统一兼容协议无需再引入第二套路由和额外依赖。此实现没有借用私有 SDK 重试函数。

## English migration notes

Use `AI_PROVIDER=openai` with an explicit `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and ordered `OPENAI_MODELS`. Task overrides are optional. Existing explicit Zhipu/Gemini variables and key files remain supported. All entry points now default to the generic openai endpoint and disabled vision. Old deployments that omitted `AI_PROVIDER` must set it explicitly; image reading requires an explicit opt-in and model. `VISION_PROVIDER` otherwise follows the selected chat provider.

The shared runtime now has one bounded policy: at most three physical attempts by default, a 20-second per-attempt timeout, and a 30-second total deadline. SDK retries are disabled; failed physical attempts consume the same persistent daily budget. Authentication/permission failures disable that endpoint until restart, rate limits honor `Retry-After`, request errors stop, and transient failures only advance through explicit candidates. No vendor error-message matching, cache-based yielding, slow-model penalties or forced last-resort calls remain. Doctor flags retired settings listed above.
