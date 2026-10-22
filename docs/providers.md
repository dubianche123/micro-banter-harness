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

智谱或 Gemini 可选 `AI_PROVIDER=zhipu` / `gemini`，并填写对应 `*_API_KEY`；`*_BASE_URL` 和 `*_MODELS*` 可覆盖预设。需要按端点管理多个 Key 时，可在部署目录使用 `api_keys_<provider>.txt`，每行一个 Key。默认 `AI_PROVIDER=openai`；`regular doctor` 展示当前有效端点和任务模型。

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

## 识图默认继承，也可独立配置

识图默认开启，未设置视觉覆盖时跟随 `AI_PROVIDER` 及其首个对话模型。因此主模型支持图片时无需再配一遍：

```dotenv
AI_PROVIDER=openai
OPENAI_BASE_URL=https://your-endpoint.example/v1
OPENAI_API_KEY=your-private-key
OPENAI_MODELS=your-multimodal-model
```

主模型是纯文本，或需要为读图选择更合适的模型时，独立覆盖即可：

```dotenv
AI_PROVIDER=openai
OPENAI_MODELS=your-text-model
# 同一端点使用另一款模型
VISION_MODEL=your-image-capable-model
# 也可选另一个已配置凭据的端点
# VISION_PROVIDER=gemini
# VISION_MODEL=your-gemini-image-model
```

`VISION_PROVIDER` 可选任一配置端点；跨端点时须同时填写 `VISION_MODEL`，不会猜测另一端点的默认视觉模型。凭据与 URL 复用该端点的配置，不另建一套客户端或路由。`VISION_ENABLED=false` 可关闭识图。`regular doctor` 展示启用状态、有效视觉端点与模型。继承配置不能保证模型支持图片，也不能从模型名或 `/models` 列表可靠推断；需要选用自己确认支持图片输入的模型。不支持或调用失败时会明确报告未读到图片，不编造描述。

默认 `VISION_INPUT_MODE=url` 将图片 URL 直接传给模型。若 QQ 图床只能由本机访问，可显式设 `VISION_INPUT_MODE=download`：先下载，再以 base64 发送一次模型请求。没有“报某段错误后换输入再提交”的自动重试。同图前后台共用在途任务与已保存描述，不重复占图片名额；前台新图在全局预算只剩一次时跳过读图，保留一次正文调用机会。

## 思考模型的输出上限

`AI_MAX_TOKENS`、`AI_MAX_TOKENS_BANTER`、`JUDGE_MAX_TOKENS`、`DIGEST_MAX_TOKENS`、`VISION_MAX_TOKENS` 都是**输出上限**，不是目标长度。思考模型的 reasoning token 同样计入上限：上限太小会先把额度花在思考上，留下空正文；称呼审核拿不到 `OK/NG` 时会 fail-open。

实测 `glm-5.3-flash` 一句普通回复约思考 90–140 token，默认值按此留余量：聊天 768、接话 320、称呼审核 400、摘要 1200、读图 300。摘要正文与 `<DIGEST>` JSON 共用 1200；读图描述最多保留 120 字符。显式设置 `AI_TEMPERATURE` 才覆盖供应商预设（智谱 0.7），不设置则沿用预设。

每个成功应答记录 `in/out/cached/thinking/finish`。出现 `finish=length` 或“正文为空”时，先提高对应任务的上限或压缩输入，不要靠重试解决。单模型全包时，一个支持图片的多模态模型可同时承担聊天、摘要、审核和读图；主模型只支持文本时再设 `VISION_MODEL`。

## 复用了什么

[OpenAI Python SDK 的异常和重试接口](https://github.com/openai/openai-python#handling-errors) 已提供 typed errors、状态码、超时及关闭自动重试的配置，本项目只保留与日预算和任务候选相关的薄边界。[LiteLLM 的重试和回退](https://docs.litellm.ai/docs/completion/reliable_completions) 适合需要更广供应商协议及集中路由的场景；当前统一兼容协议无需再引入第二套路由和额外依赖。此实现没有借用私有 SDK 重试函数。

## English configuration summary

Use `AI_PROVIDER=openai` with an explicit `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and ordered `OPENAI_MODELS`. Task overrides are optional. Zhipu/Gemini are optional presets with their own endpoint, key and model variables. `FALLBACK_PROVIDER` is empty by default and only explicitly configured endpoints receive fallback prompts.

Image reading is enabled by default. Without overrides, it inherits `AI_PROVIDER` and its first chat model. Set `VISION_MODEL` to override the model; when choosing another endpoint with `VISION_PROVIDER`, also provide `VISION_MODEL`. Set an independent image-capable model when chat uses a text-only model, or disable image reading with `VISION_ENABLED=false`. Configuration inheritance does not guarantee image support; failures produce an explicit unavailable-image result, never a guessed caption.

Token caps are output ceilings, and reasoning tokens count against them on thinking models; undersized caps yield empty visible content. Keep headroom, watch `finish=length`, and raise the task cap instead of retrying. An explicit `AI_TEMPERATURE` overrides the provider preset.

The runtime uses one bounded policy: at most three physical attempts by default, a 20-second per-attempt timeout, and a 30-second total deadline. SDK retries are disabled; failed physical attempts consume the same persistent daily budget. Routing uses SDK exception types, HTTP status and `Retry-After`, with only explicitly configured candidates.
