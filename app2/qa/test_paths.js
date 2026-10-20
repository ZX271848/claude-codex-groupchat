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
const assert = require('node:assert/strict');
for (const p of ['/Users/me/project/file.py:42', '/tmp/test.txt', '/用户/项目/说明.md', '~/.claude/groupchat2']) {
  assert.ok(inline(p).includes(`data-path="${p}"`), `bare path: ${p}`);
  assert.ok(inline('`'+p+'`').includes(`data-path="${p}"`), `code path: ${p}`);
}
assert.ok(inline('`/Users/me/My Project`').includes('data-path="/Users/me/My Project"'));
assert.ok(!inline('https://example.com/docs/page').includes('data-path='));
assert.ok(!inline('`thread/inject_items`').includes('data-path='));
assert.ok(inline('[文档](/Users/me/a.md)').includes('href="/Users/me/a.md"'));
console.log('macOS and Windows path assertions passed');
