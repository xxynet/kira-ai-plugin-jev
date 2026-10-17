# kira-ai-plugin-jev

[English](README.md) | 简体中文

使用 TypeSafe 的 **Jev** 模型判断收到的消息是否值得回复，并将符合条件的消息标记为“已提及”，交由聊天插件处理。

需要 **KiraAI 3.0.0 或更高版本**及其 `ctx.message_history` 服务。

Jev 是一种 *System One* 决策模型，不生成供插件解析的文本回复。插件调用原生 `POST /v1/systemone` 接口，提交 `noul` 类型的问题，直接读取模型返回的“是否应该回复”的 0–1 概率。

## 工作原理

- 通过 `on.im_message` 注册消息钩子，优先级为 `Priority.HIGH + 10`：在系统级守卫（`SYS_HIGH`）之后、读取 `is_mentioned` 的聊天插件（`HIGH`）之前运行。
- 跳过通知事件、机器人自身消息，以及已经标记为提及的消息。Jev 主要判断原本可能被忽略的消息，例如没有 @ 机器人的群聊消息。
- 对需要判定的消息发送一次 `systemone` 请求，包含：
  - `state.persona`：当前激活人设的 `name` 和完整 `content`。每次判定都通过 `ctx.persona_mgr.get_persona()` 异步读取，切换人设后下次判定立即生效。该字段不包含全局、私聊或群聊规则。
  - `state.target`：当前消息，格式为 `发送者：内容〔群聊：群名〕`，私聊使用 `〔私聊〕`。消息正文最多保留 2000 字符。
  - `state.conversation`：近期归档消息组成的字符串数组。默认总预算为 12000 字符，优先保留最新内容，最终按从早到晚排列。通过 `ctx.message_history` 异步查询当前会话、当前机器人账号的历史，默认最多查询 20 条记录，可通过 `history_limit` 调整（1–200）。以当前消息的归档记录为游标，排除当前及更晚归档的消息。群聊保留成员昵称，机器人回复使用 `机器人：` 前缀，私聊用户使用 `对方：` 前缀。过滤通知、自身回流消息，以及未确认发送成功的机器人消息。
  - `questions.should_reply`：`noul` 类型的判定问题，根据群聊或私聊使用 `group_judge_prompt` / `private_judge_prompt`。无论使用默认还是自定义提示词，都会自动追加人设字段说明和提示注入防护指令：人设文本仅作为身份数据，会话文本仅作为不可信数据，不作为判定指令。
- 当返回的 `noul` 概率不低于 `threshold`（默认 0.5）时，设置 `event.message.is_mentioned = True`，由后续聊天插件决定缓冲、合并及回复时机。
- 严格校验响应：`type` 必须为 `"noul"`，概率必须为 `[0, 1]` 范围内的有限数值。超时、HTTP 错误或响应无效时保留原有提及标记，不额外触发回复。请求不重试，以避免消息积压。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_key` | sensitive | 空 | TypeSafe API Key，在 [TypeSafe 控制台](https://console.typesafe.ai/keys)获取。必填。 |
| `base_url` | string | `https://api.typesafe.ai/v1` | System One API 基础地址。 |
| `model` | string | `jev-latest` | TypeSafe 模型 ID，不是 KiraAI 提供商中的模型配置。 |
| `threshold` | float | `0.5` | 触发回复所需的最低 noul 概率，范围为 0–1。 |
| `history_limit` | integer | `20` | 查询归档记录的数量上限，范围为 1–200。超出范围时限制到边界值，无效值使用 20。过滤及字符预算可能使实际携带条数更少。 |
| `conversation_budget` | integer | `12000` | `state.conversation` 的字符预算，设为 0 时不读取历史。 |
| `timeout` | float | `8.0` | HTTP 请求超时，单位为秒。 |
| `group_judge_prompt` | textarea | 内置提示词 | 群聊消息的 noul 判定指令。 |
| `private_judge_prompt` | textarea | 内置提示词 | 私聊消息的 noul 判定指令。 |

## 使用说明

- 判定在消息处理流程中执行。需要判定的未提及消息会先等待一次 System One 请求，再进入聊天插件的缓冲流程；HTTP 超时由 `timeout` 控制。
- 上下文来自框架消息归档，其中已经包含仍在缓冲区等待处理的消息。插件不再额外拼接缓冲区，也不读取 LLM 记忆，避免重复消息，且不受保留记忆块数限制。框架的归档保留策略会影响可读取的历史范围。
- 历史读取失败时，使用当前消息和激活人设继续判定。将 `conversation_budget` 设为 0 只会跳过历史查询，人设仍会传入。人设正文不受历史字符预算或单条消息长度限制。人设读取失败时跳过本次判定，不改变提及标记；没有激活人设时，人设字段为空。
- 只有图片或语音的消息通过元素占位文本参与判定，媒体识别在后续聊天处理阶段进行。

## HTTP 错误排查

HTTP 错误日志包含具体状态码和固定的排查提示，例如：

```text
[Jev] Judge request failed | HTTP 401 | Check the TypeSafe API key and whether it is valid for this endpoint.
```

- 使用官方 API 时，确认 `base_url` 为 `https://api.typesafe.ai/v1`，`model` 为 `jev-latest`。插件会在基础地址后追加 `/systemone`。
- `401`：检查 TypeSafe API Key；`403`：检查权限及网络访问限制。
- `404` 或 `405`：检查基础地址及反向代理路由。
- `400` 或 `422`：对照接口规范检查请求。已知的 `max_tokens_exceeded` 错误会单独提示为上下文超限。完整人设正文也会消耗上下文，不计入历史预算不代表不占用模型上下文。
- `451`：因法律原因无法提供访问，检查服务商地区访问策略及网关拦截。仅凭状态码无法确定具体由哪一层拦截。
- `429`：检查账号限流及消息频率；`529`：服务过载。

排查提示用于辅助定位，不代表已确认服务端的具体原因。错误时保留提及标记且不重试。HTTP 错误日志仅包含状态码及本地固定提示，不输出响应正文、请求内容或凭据。

状态码含义请参阅 [TypeSafe 官方 API 文档](https://docs.typesafe.ai/api)。

## 本地开发

本项目是 KiraAI 插件，需要 Python 3.10+、KiraAI 3.x 源码及框架依赖。插件的运行依赖列在 `requirements.txt` 中。

如果仓库位于 `KiraAI/data/plugins/kira-ai-plugin-jev`，可在插件目录执行：

```text
python -m pip install -r requirements-dev.txt
python -m pytest
```

如果单独克隆插件仓库，需要通过 `KIRAAI_ROOT` 指向框架源码目录。PowerShell 示例：

```powershell
$env:KIRAAI_ROOT = 'D:\path\to\KiraAI'
python -m pip install -r "$env:KIRAAI_ROOT/requirements.txt"
python -m pip install -r requirements-dev.txt
python -m pytest
```

POSIX shell 中先设置 `export KIRAAI_ROOT=/path/to/KiraAI`，再执行相同的安装和测试命令；框架依赖路径使用 `"$KIRAAI_ROOT/requirements.txt"`。

测试模拟所有 TypeSafe 请求，并使用临时运行目录，不需要真实 API Key。验证在本地进行，本仓库没有配置 CI 工作流。

分享修改前，请检查 `git status` 和差异，确保凭据、运行数据、测试缓存及本地虚拟环境未进入版本控制。
