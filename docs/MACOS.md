# Mac 安装与使用

当前 `app2` 客户端支持 macOS，使用系统 Cocoa / WKWebView。程序查找覆盖 Apple Silicon 与 Intel 的常见安装位置；已在 Apple Silicon 上验证，Intel 尚未实机验证。第一版 `app/` 的独立桌面入口仍只支持 Windows。

## 安装

准备 Python 3.11 或更新版本，然后在仓库根目录运行：

```sh
python3 install.py
```

安装器会在仓库里创建 `.venv`，安装 `requirements.txt` 中的 pywebview 及其 Mac 依赖，并生成 `~/Applications/群聊.app`。桌面上没有同名文件时，也会创建入口。不会改写现有 Claude / Codex hooks。

双击“群聊.app”或仓库里的“启动群聊.command”即可打开。它是本机启动器，仍依赖源码目录与 `.venv`；请保留它们，移动仓库后重新运行安装器。它不是可分发的独立安装包。

## 模型程序与登录

客户端使用你自己的官方 CLI 和登录账号。检查能否找到程序：

```sh
.venv/bin/python install.py --check
```

Claude Code 按[官方安装说明](https://code.claude.com/docs/en/setup)安装，首次登录运行 `claude auth login`。也可以将官方 npm 包装在本项目内：

```sh
npm install --prefix .tools @anthropic-ai/claude-code
```

如果 npm 提示该包的 postinstall 尚未获准执行，先检查它，再按 npm 的提示允许此包的安装脚本；否则可能只安装了一个占位程序。双击仓库里的“登录Claude.command”会启动登录流程，需要你在浏览器中完成认证。登录后重开群聊。

Codex 会查找 `/Applications/Codex.app`、`/Applications/ChatGPT.app` 内附带的 CLI，以及常见 CLI 安装路径。已有 Codex 登录可以复用；独立 CLI 的安装和登录见 [Codex 官方仓库](https://github.com/openai/codex)。Gemini / `agy` 为可选成员，不安装也能使用 Claude + GPT。

Finder 的环境变量与终端不同。客户端会补充 Homebrew（`/opt/homebrew/bin`、`/usr/local/bin`）、`~/.local/bin`、常见 npm / nvm / Volta 路径以及本项目 `.tools/node_modules/.bin`。自定义安装可写入 `~/.claude/groupchat2/paths.json`：

```json
{
  "claude": "/absolute/path/to/claude",
  "codex": "/absolute/path/to/codex"
}
```

只填需要覆盖的项。也支持 `GROUPCHAT_CLAUDE_PATH`、`GROUPCHAT_CODEX_PATH`、`GROUPCHAT_AGY_PATH`。自定义程序必须有执行权限。

## Mac 窗口与文件

- 红色关闭按钮和 ⌘Q 会退出并清理模型进程；黄色按钮缩到 Dock，任务继续运行。
- 再次打开时会显示已有窗口。同一状态目录只允许一个客户端写入；崩溃后锁自动释放。
- 文档用默认程序打开；代码优先用 VS Code，否则用系统文本编辑器；`.app`、`.dmg`、`.pkg`、`.command` 等只在 Finder 中定位。
- 支持 `/Users/...`、`~/...`、`file:///...`、含空格的 Markdown 文件链接以及 `:行号` / `#L行号` 路径。
- 聊天数据位于 `~/.claude/groupchat2/`；启动器日志位于 `~/Library/Logs/ClaudeCodexGroupChat/client.log`。

Claude 和 Gemini 继承上游的工具自动批准策略。GPT 使用 `workspace-write` 沙箱，可写当前群聊的项目目录及系统临时目录，其他本地文件可读；新建和恢复会话都会明确设置该策略，避免继承 CLI 的只读默认值。网络仍受 Codex 沙箱及管理策略限制。未指定成员时会调用 Claude 分类器并消耗额度。请在信任的项目中使用。手动选择 GPT 或输入 `@gpt` 可跳过分类器，在 Claude 尚未登录时先使用 GPT。

## 验证

```sh
cd app2
../.venv/bin/python -m unittest qa.test_coordinator qa.test_gemini_member qa.test_routing_session qa.test_macos
node qa/test_display.cjs
node qa/test_paths.js
# 可选：打开原生窗口，检查 Codex 握手和退出，不发送模型提示
../.venv/bin/python qa/smoke_macos.py
```
