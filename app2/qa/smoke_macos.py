"""Opt-in native window + backend handshake check, without sending any model prompt.

Run from app2: ../.venv/bin/python qa/smoke_macos.py
Uses a temporary group-chat state directory; existing chats are untouched.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
results = {}


def inspect_window(window, app):
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            engine = app.gpt['work']
            if engine.ready or engine.error:
                break
            time.sleep(0.2)
        # The UI starts through its asynchronous pywebviewready event.
        time.sleep(2)
        results.update(renderer=window.gui.renderer, gpt_ready=engine.ready,
                       gpt_error=engine.error, models=len(engine.models),
                       bridge=window.evaluate_js('Boolean(window.pywebview && window.pywebview.api)'),
                       page_text=window.evaluate_js('document.body.innerText').strip()[:500])
        results['gpt_pid'] = engine.p.pid if engine.p else None
    except Exception as ex:
        results['error'] = str(ex)
    finally:
        window.destroy()


with tempfile.TemporaryDirectory(prefix='groupchat-native-smoke-') as state:
    os.environ['GROUPCHAT2_STATE'] = state
    spec = importlib.util.spec_from_file_location('native_smoke_app', ROOT/'app.py')
    app_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_module)
    original = app_module.webview.create_window

    def create_window(*args, **kwargs):
        window = original(*args, **kwargs)
        window.events.loaded += lambda: inspect_window(window, kwargs['js_api']._app)
        return window

    app_module.webview.create_window = create_window
    app_module.main()
    if results.get('gpt_pid'):
        results['gpt_stopped'] = not app_module.C.pid_alive(results['gpt_pid'])
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not (results.get('bridge') and results.get('gpt_ready') and results.get('gpt_stopped') and not results.get('error')):
        raise SystemExit(1)
