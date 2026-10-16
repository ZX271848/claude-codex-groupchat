// Path and link rendering of the chat page: node qa/test_paths.js
const fs = require('fs'); const h = fs.readFileSync(__dirname + '/../ui.html', 'utf8');
const a = h.indexOf('function esc('), b = h.indexOf('/* ---------- images');
const document = {addEventListener() {}}, api = () => null;
eval(h.slice(a, b));
const cases = [
  '路径：`C:\\Users\\me\\.claude\\codex-bridge\\app\\prompts\\PROTOCOL.md`',
  '见 C:\\Users\\me\\Documents\\项目！\\CLAUDE.md，里面有说明。',
  '状态放在 `~/.claude/groupchat2`，和 `app2/qa/test_flow.py:42` 以及 `thread/inject_items`',
  '[文档](C:/Users/me/Desktop/a.md) 和 [网页](https://example.com/x)',
  '"C:\\tmp\\x.txt" 与 `https://claude.ai/code`，还有 **C:\\a\\b.pdf**。',
];
for (const c of cases) console.log(inline(c).replace(/ title="[^"]*"/g, ''), '\n');
