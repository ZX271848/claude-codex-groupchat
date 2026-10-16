# Checks

Run with the desktop client's Python environment:

```
python -X utf8 qa/test_coordinator.py
node qa/test_display.cjs
node qa/test_paths.js
python make_ui.py
```

`test_coordinator.py` uses only temporary rooms and mock transports. It covers explicit/automatic/both routing, ordered replies, insertion, deferred history, collaboration and exit, replies to peer requests, progress, failures/retries/cursors, restart recovery, classifier isolation, prompt loading and real Claude-style two-result insertion.

`probe_classifier.py` and `probe_stream.py` are opt-in real Claude calls with tools, hooks, project instructions and persistence disabled. They never use work session IDs or live rooms. The recorded tiny probe results are in `classifier-probe-result.json` and `stream-probe-result.json`. Do not include these probes in an automatic test suite.

`test_gemini_member.py` covers rooms with a third member (Gemini): targets, ordered answers, queueing instead of insertion, peer requests, and the agy event parser on a recorded turn. `test_gemini_live.py`, `probe_agy.py` and `probe_agy2.py` are opt-in real calls to the Antigravity CLI in throwaway rooms.
