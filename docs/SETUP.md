# 安装与排查

**macOS 用户请先看 [Mac 安装说明](MACOS.md)。** 以下安装命令以 Windows 为主。两种系统使用相同的 `paths.json` 和群聊协议；Mac 程序名没有 `.exe` 后缀。

客户端自己不带模型。它在你的电脑上找到三个官方程序，用你在这些程序里已经登录的账号运行它们：

| 成员 | 程序 | 用的账号 |
|---|---|---|
| Claude | Claude Code（`claude.exe`） | 你的 Claude 订阅或 API key |
| GPT | Codex（`codex.exe`） | 你的 ChatGPT 订阅或 API key |
| Gemini（可选） | Google Antigravity CLI（`agy.exe`） | 你在反重力里登录的 Google 账号 |

Claude 和 GPT 是必需的；找得到 `agy.exe` 时，Gemini 自动成为每个群聊的第三位成员，找不到就是两人群聊。

先跑一次检查，看客户端在你的电脑上找到了什么：

```
python app2\backends.py
```

三行都是 √ 就可以直接用；有 × 的，按下面对应的一节处理。

---

## 1. 装好三个程序并登录一次

每个程序都要**先自己登录一次**。客户端只调用它们，不替你登录。

### Claude Code

任选一种：

- **Claude 桌面端**（[下载](https://claude.ai/download)）：自带 Claude Code。客户端会在桌面端的安装目录里找到它。
- **独立安装**（PowerShell）：
  ```
  irm https://claude.ai/install.ps1 | iex
  ```
  装好后在终端运行一次 `claude`，按提示登录。
- **npm**：`npm install -g @anthropic-ai/claude-code`，然后运行一次 `claude` 登录。

### Codex（GPT）

任选一种：

- **Codex 桌面端**（[下载](https://openai.com/codex)），在桌面端里登录。
- **Codex CLI**：`npm install -g @openai/codex`，然后 `codex login`。

### Antigravity CLI（Gemini，可选）

PowerShell：

```
irm https://antigravity.google/cli/install.ps1 | iex
```

它装在 `%LOCALAPPDATA%\agy\bin\agy.exe`。登录和反重力桌面端共用：在桌面端登录过就不用再登；没有的话，在终端运行一次 `agy`，会打开浏览器让你登录 Google 账号。也可以用 Gemini API key，见[官方认证说明](https://antigravity.google/docs/install?tab=cli)。

哪些 Google 账号、哪些套餐能用反重力，由 Google 决定。运行一次 `agy models`：能列出模型，客户端就能用；报登录或权限错误，客户端也一样用不了。

---

## 2. 安装客户端

需要 Windows 10/11、Python 3.11 或更新（[python.org](https://www.python.org/downloads/)，安装时勾选 Add to PATH），以及 Edge WebView2（Windows 自带）。

```
git clone https://github.com/ZX271848/claude-codex-groupchat
cd claude-codex-groupchat
python install.py --check    # 只看找到了什么
python install.py            # 安装 pywebview，在桌面放「群聊」快捷方式
```

双击桌面的「群聊」打开。第一次打开时，客户端在你的 Codex 项目里建第一个群聊；没有 Codex 项目时，建在默认目录。之后用左侧的 ＋ 在项目里新建群聊。

---

## 3. 客户端找不到程序时

客户端按这个顺序找，找到就停：

1. 你指定的路径：环境变量，或 `paths.json`（见下）；
2. 桌面端自带的位置：
   - Claude：`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\<版本>\claude.exe`、`%APPDATA%\Claude\claude-code\<版本>\claude.exe`
   - Codex：`%LOCALAPPDATA%\OpenAI\Codex\bin\<版本>\codex.exe`
3. 独立安装的位置：`%USERPROFILE%\.local\bin\claude.exe`、`%LOCALAPPDATA%\agy\bin\agy.exe`、npm 的 `%APPDATA%\npm\claude.cmd` / `codex.cmd`；
4. `PATH` 里的 `claude` / `codex` / `agy`。

桌面端每次更新都会换一个版本文件夹，客户端总是用最新的那个。

**装在别处的，手动指定路径。** 新建 `%USERPROFILE%\.claude\groupchat2\paths.json`：

```json
{
  "claude": "D:\\tools\\claude\\claude.exe",
  "codex": "D:\\tools\\codex\\codex.exe",
  "agy": "D:\\tools\\agy\\agy.exe"
}
```

只写找不到的那几项即可。也可以用环境变量：`GROUPCHAT_CLAUDE_PATH`、`GROUPCHAT_CODEX_PATH`（或 Codex 自己的 `CODEX_CLI_PATH`）、`GROUPCHAT_AGY_PATH`。改完重开客户端（托盘图标 → 退出，再打开）。

---

## 4. 常见问题

**侧栏显示"出错"或"未连接"。** 鼠标停在状态上能看到原因。最常见的是那个程序还没登录：在终端里直接运行它（`claude`、`codex login`、`agy`）登录一次，再重开客户端。

**没有 Gemini 卡片。** 说明没找到 `agy.exe`，侧栏下方会写出来。装好 Antigravity CLI 后重开客户端，所有没有单独设置成员的群聊都会加入 Gemini。

**某个群聊不要 Gemini（或只要 Gemini 加入某几个）。** 在那个群聊的 `room.json`（`%USERPROFILE%\.claude\groupchat2\rooms\<群聊>\room.json`）里写上成员：`"members": ["gpt", "claude"]`。写了列表的群聊按列表来，没写的按本机装了什么来。

**Gemini 加入老群聊时读多少历史。** 老群聊可能很长，Gemini 第一次被叫醒时读最近约 5.5 万字的原文（一次调用）。更早的原文它知道在哪个文件里，需要时自己用工具查。想让它从某条消息开始读，在它第一次说话之前运行：

```
python app2\tools\add_member.py "<群聊名>" --from-seq 1200
```

**Gemini 和另外两位有什么不同。** 反重力 CLI 不支持中途插话，也没有中断指令，客户端这样处理：
- Gemini 工作时发给它的消息，要等它这一轮做完才处理（Claude 和 GPT 可以中途插进去）。反重力的无界面模式没有插话的办法；客户端试过“结束这一轮再带着新消息续上”，会在对话里留下被切断的回复，已经撤掉；
- "停止"会结束它的进程，下次自动续上同一个对话；
- 图片只能把路径告诉它，由它自己用工具打开；
- 额度从 `agy -p /usage` 读取，卡片上显示周额度和 5 小时额度。

**权限。** 三位默认都不弹确认：Claude 跳过所有确认；GPT 不请求审批，能访问的范围按你 Codex 的沙盒设置（`~/.codex/config.toml` 的 `sandbox_mode`）；Gemini 自动批准所有工具。只在你信任的项目里使用。Gemini 改成需要确认：在 `%USERPROFILE%\.claude\groupchat2\app-prefs.json` 里写 `"gemini_permission": "ask"`。headless 模式下，需要确认的工具会被直接拒绝。

**费用。** 没有点名的消息，由一个静默分类器（Claude Opus 5.5，思考强度 low）决定交给谁，这会用你的 Claude 额度。点名（`@gpt` / `@claude` / `@gemini`）或手动选对象时不经过分类器。

**关窗口不等于退出。** 关窗口只是缩到托盘，模型还在后台工作。要真正退出或重启，在托盘图标上点"退出"。
