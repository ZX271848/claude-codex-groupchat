# 开发这个客户端

给在这个仓库里改代码的人和 agent。

- 现在的客户端在 `app2/`。`app/` 是第一版（靠两边的 hook 搬原话），`app2` 复用了它的几个工具函数（`app/app.py`、`app/work_record.py`、`app/tray.py`）和根目录的 `common.py`。
- 运行规范和每个设计决定的理由：`app2/PROTOCOL.md`。给模型的说明：`app2/prompts/work.md`（Claude 和 GPT 的群聊规则）、`app2/prompts/gemini.md`（Gemini 第一次被叫醒时收到）、`app2/prompts/coordinator-memory*.md`（分类器）。每一轮另有一段客户端环境说明，由 `conversation.py` 的 `environment()` 生成。
- 结构是一个静默分类器，加上每位群聊成员一份有工具的持续工作会话。客户端只递送原话，不转述、不替模型管理上下文，也不决定它们怎么做事。不要加回“前台讨论 + 后台执行”或任务派发握手，那是试过后放弃的设计。
- 各成员程序（Claude Code、Codex、Antigravity CLI）的查找在 `app2/backends.py`；接新的程序时，在那里加查找位置，再写一个 lane（参考 `app.py` 里的 `GeminiLane`），协调逻辑按成员列表工作，不用改。
- 改完先跑离线测试（在 `app2/` 目录）：
  ```
  python -m unittest qa.test_coordinator qa.test_gemini_member qa.test_routing_session
  node qa/test_display.cjs
  node qa/test_paths.js
  ```
  改了 `ui/index.html` 之后运行 `python make_ui.py` 生成 `ui.html`。
- 真实模型探测（`qa/probe_*.py`、`qa/test_gemini_live.py`）会花额度，只在临时状态目录和临时群聊里跑，不要用正在使用的群聊或会话 ID。
- 改代码不会影响已经在运行的客户端；要生效，在托盘图标上点“退出”再重开。不要对同一个群聊同时开两个客户端。
