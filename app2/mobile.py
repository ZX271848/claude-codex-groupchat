"""Opt-in private companion. Owns transport/auth, never a second model runtime."""
import base64
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

import common as C
from history import public_event
from mobile_transport import TailscaleServe, TransportError

STATIC = Path(__file__).parent / 'mobile'
BODY_LIMIT = 6_000_000
DAY = 86400
COOKIE = '__Host-gc_mobile'
COOKIE_FLAGS = f'Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={30 * DAY}'


class MobileError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temp, path)
    os.chmod(path, 0o600)


class MobileCompanion:
    def __init__(self, app, state, conversations, new_room):
        self.app, self.state = app, Path(state)
        self.conversations, self.new_room = conversations, new_room
        self.lock = threading.RLock()
        self.server = None
        self.code, self.code_until = '', 0
        self.attempts = []
        stored = C.load_json(str(self.state / 'mobile-devices.json'), {})
        self.principal = stored.get('principal', '') if stored.get('version') == 2 else ''
        self.devices = stored.get('devices', {}) if stored.get('version') == 2 else {}
        if stored and stored.get('version') != 2:
            self.save_devices()  # revoke every legacy plaintext-HTTP pairing
        self.transport = TailscaleServe()
        self.receipts = C.load_json(str(self.state / 'mobile-receipts.json'), {})
        self.run_id = secrets.token_hex(16)
        self.tree_at, self.tree_cache = 0, []

    def save_devices(self):
        private_json(self.state / 'mobile-devices.json',
                     {'version': 2, 'principal': self.principal, 'devices': self.devices})

    def desktop_status(self):
        with self.lock:
            return {'running': bool(self.server),
                    'urls': [f'https://{self.transport.host}/'] if self.server else [],
                    'code': self.code if time.time() < self.code_until else '',
                    'expires': int(self.code_until),
                    'devices': sum(expiry > time.time() for expiry in self.devices.values())}

    def start(self, port=8765):
        with self.lock:
            if self.server:
                return self.desktop_status()
            owner = self

            class Handler(BaseHTTPRequestHandler):
                server_version = 'GroupChat'
                sys_version = ''

                def setup(self):
                    super().setup()
                    self.connection.settimeout(10)

                def log_message(self, *_args):
                    pass  # no cookies, codes, query strings or chat text in HTTP logs

                def reply(self, status, data, content_type='application/json; charset=utf-8', cookie=None):
                    if not isinstance(data, bytes):
                        data = json.dumps(data, ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Strict-Transport-Security', 'max-age=31536000')
                    self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('X-Frame-Options', 'DENY')
                    self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                    if cookie:
                        self.send_header('Set-Cookie', cookie)
                    self.end_headers()
                    self.wfile.write(data)

                def handle_request(self, post=False):
                    try:
                        host = self.headers.get('Host', '')
                        if not owner.transport.accepts(self.headers, self.client_address[0]):
                            raise MobileError('仅允许同账号的 Tailscale 私有 HTTPS 连接', 403)
                        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and urlsplit(self.path).path.startswith('/api/'):
                            raise MobileError('不允许跨站请求', 403)
                        if post and (len(self.headers.get_all('Origin', [])) != 1 or self.headers.get('Origin') != 'https://' + host):
                            raise MobileError('请求来源不匹配', 403)
                        path = urlsplit(self.path)
                        if post:
                            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                                raise MobileError('需要 JSON 请求', 415)
                            if self.headers.get('Transfer-Encoding'):
                                raise MobileError('不支持该传输方式')
                            try:
                                size = int(self.headers.get('Content-Length', '0'))
                            except ValueError:
                                raise MobileError('无效请求长度')
                            if not 0 < size <= BODY_LIMIT:
                                raise MobileError('请求过大或为空', 413)
                            raw = self.rfile.read(size)
                            if len(raw) != size:
                                raise MobileError('请求未完整接收')
                            try:
                                body = json.loads(raw)
                            except (ValueError, UnicodeError):
                                raise MobileError('无效 JSON')
                            if not isinstance(body, dict):
                                raise MobileError('需要 JSON 对象')
                            if path.path == '/api/pair':
                                token = owner.pair(body.get('code'))
                                self.reply(200, {'ok': True}, cookie=f'{COOKIE}={token}; {COOKIE_FLAGS}')
                                return
                        with owner.lock:
                            if not owner.server or not owner.transport.active:
                                raise MobileError('手机连接已关闭', 503)
                            token = self.token()
                            if path.path.startswith('/api/'):
                                owner.authenticate(token)
                            if not post and path.path == '/api/snapshot':
                                query = parse_qs(path.query)
                                self.reply(200, owner.snapshot(query.get('room', [''])[0], query.get('after', [None])[0]))
                            elif not post and path.path == '/api/image':
                                query = parse_qs(path.query)
                                data, kind = owner.image(query.get('room', [''])[0], query.get('path', [''])[0])
                                self.reply(200, data, kind)
                            elif post and path.path == '/api/action':
                                self.reply(200, owner.action(body))
                            elif post and path.path == '/api/logout':
                                owner.logout(token)
                                self.reply(200, {'ok': True}, cookie=f'{COOKIE}=; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                            elif not post and path.path in ('/', '/mobile.js', '/mobile.css', '/manifest.webmanifest', '/icon.svg', '/apple-touch-icon.png'):
                                name = 'index.html' if path.path == '/' else path.path[1:]
                                kind = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
                                        'css': 'text/css; charset=utf-8', 'webmanifest': 'application/manifest+json', 'svg': 'image/svg+xml', 'png': 'image/png'}[name.rsplit('.', 1)[1]]
                                self.reply(200, (STATIC / name).read_bytes(), kind)
                            else:
                                raise MobileError('找不到这个接口', 404)
                    except MobileError as exc:
                        self.reply(exc.status, {'error': str(exc)})
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        pass
                    except Exception:
                        self.reply(500, {'error': 'Mac 暂时无法处理请求，请稍后重试'})

                def token(self):
                    cookie = SimpleCookie()
                    try:
                        cookie.load(self.headers.get('Cookie', ''))
                        return cookie[COOKIE].value if COOKIE in cookie else ''
                    except Exception:
                        return ''

                def do_GET(self):
                    self.handle_request()

                def do_POST(self):
                    self.handle_request(True)

            class Server(ThreadingHTTPServer):
                daemon_threads = True
                allow_reuse_address = True
                slots = threading.BoundedSemaphore(16)

                def process_request(self, request, client_address):
                    if not self.slots.acquire(blocking=False):
                        self.shutdown_request(request)
                        return
                    try:
                        super().process_request(request, client_address)
                    except Exception:
                        self.slots.release()
                        raise

                def process_request_thread(self, request, client_address):
                    try:
                        super().process_request_thread(request, client_address)
                    finally:
                        self.slots.release()

            server = Server(('127.0.0.1', port), Handler)
            try:
                self.transport.start(server.server_port)
            except TransportError as exc:
                server.server_close()
                self.transport.stop()
                raise MobileError(str(exc), 503) from None
            except Exception:
                server.server_close()
                raise
            if self.principal != self.transport.login:
                self.devices = {}
                self.principal = self.transport.login
                self.save_devices()
            self.server = server
            threading.Thread(target=self.server.serve_forever, daemon=True, name='groupchat-mobile').start()
            self.refresh_code()
            return self.desktop_status()

    def stop(self):
        with self.lock:
            server, self.server = self.server, None
            self.code, self.code_until = '', 0
            self.transport.active = False
        if server:
            server.shutdown()
            server.server_close()
            self.transport.stop()
        return self.desktop_status()

    def refresh_code(self):
        with self.lock:
            if self.server:
                self.code = f'{secrets.randbelow(100_000_000):08d}'
                self.code_until = time.time() + 300
            return self.desktop_status()

    def revoke(self):
        with self.lock:
            self.devices = {}
            self.save_devices()
            return self.refresh_code()

    def pair(self, code):
        with self.lock:
            if not self.server or not self.transport.active:
                raise MobileError('手机连接已关闭', 503)
            now = time.time()
            self.attempts = [t for t in self.attempts if t > now - 60]
            if len(self.attempts) >= 8:
                raise MobileError('尝试过多，请一分钟后再试', 429)
            self.attempts.append(now)
            if not isinstance(code, str) or not self.code or now >= self.code_until or not secrets.compare_digest(code, self.code):
                raise MobileError('配对码无效或已过期，请在 Mac 上刷新', 401)
            token = secrets.token_urlsafe(32)
            self.devices = {key: expiry for key, expiry in self.devices.items() if expiry > now}
            self.devices[self.digest(token)] = now + 30 * DAY
            self.save_devices()
            self.code, self.code_until = '', 0  # one use
            return token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        with self.lock:
            if not token or self.devices.get(self.digest(token), 0) <= time.time():
                raise MobileError('请先与 Mac 配对', 401)

    def logout(self, token):
        with self.lock:
            self.devices.pop(self.digest(token), None)
            self.save_devices()

    def room(self, rid):
        if not isinstance(rid, str) or not rid or rid in ('.', '..') or any(c in rid for c in '/\\\x00'):
            raise MobileError('无效群聊')
        path = Path(C.ROOMS_DIR) / rid
        if path.resolve().parent != Path(C.ROOMS_DIR).resolve():
            raise MobileError('无效群聊')
        info = C.load_json(str(path / 'room.json'), None)
        if not info or info.get('archived'):
            raise MobileError('群聊不存在或已归档', 404)
        return C.room_by_dir(str(path))

    def tree(self, fresh=False):
        with self.lock:
            if fresh or time.monotonic() - self.tree_at > 10:
                self.tree_cache = [{ 'id': p['id'], 'name': p['name'], 'can_create': bool(p.get('root')),
                                     'rooms': [{k: r[k] for k in ('id', 'title', 'when', 'archived')}
                                               for r in p['rooms'] if not r.get('archived')]}
                                   for p in self.conversations()]
                self.tree_at = time.monotonic()
            return self.tree_cache

    def snapshot(self, rid='', after=None):
        with self.lock, self.app.sessions_lock:
            return self._snapshot(rid, after)

    def _snapshot(self, rid='', after=None):
        rid = rid or self.app.rid
        room = self.room(rid)
        try:
            cursor = int(after) if after is not None else 0
            if cursor < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise MobileError('无效消息游标')
        with C.Lock(C.channel_path(room)):
            rows = C.read_after(room, cursor)
        visible = [p for e in rows if (p := public_event(e)) is not None]
        session = self.app.sess(rid)
        return {'room': rid, 'title': room['name'], 'tree': self.tree(), 'members': list(session.members),
                'lines': visible[-300:] if after is None else visible,
                'cursor': rows[-1]['seq'] if rows else cursor,
                'status': self.app.status_payload(rid),
                'models': {'gpt': self.app.gpt_cfg.get('model'), 'claude': self.app.claude_cfg.get('model'),
                           'gemini': self.app.gemini_cfg.get('model')}}

    def image(self, rid, raw):
        room = self.room(rid)
        root = Path(room['root']).resolve() if room.get('root') else None
        folder = (Path(room['dir']) / 'attachments').resolve()
        path = Path(raw)
        if not path.is_absolute():
            path = (Path(room['dir']) if path.parts[:1] == ('attachments',) else root or folder) / path
        path = path.resolve()
        if not (path.is_relative_to(folder) or (root and path.is_relative_to(root))):
            raise MobileError('图片不在这个项目中', 403)
        kind = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp'}.get(path.suffix.lower())
        if not kind or not path.is_file() or path.stat().st_size > 8_000_000:
            raise MobileError('图片不存在、格式不支持或超过 8 MB', 404)
        return path.read_bytes(), kind

    def action(self, body):
        """Durable at-most-once admission. Uncertain work after a crash is never replayed."""
        with self.lock, self.app.sessions_lock:
            if self.app.closing.is_set():
                raise MobileError('Mac 客户端正在退出', 503)
            request_id = body.get('request_id', '')
            match = re.fullmatch(r'(\d{13})-[a-f0-9]{32}', request_id) if isinstance(request_id, str) else None
            now = time.time()
            if not match or not now - DAY < int(match[1]) / 1000 < now + 300:
                raise MobileError('请求已过期，请检查消息记录后重新发送')
            signature = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            previous = self.receipts.get(request_id)
            if previous:
                if previous['signature'] != signature:
                    raise MobileError('请求编号与内容不一致', 409)
                if previous['run'] == self.run_id and 'result' in previous:
                    return previous['result']
                if body.get('action') == 'send':
                    room = self.room(body.get('room'))
                    with C.Lock(C.channel_path(room)):
                        if any(e.get('mobile_request_id') == request_id for e in C.read_after(room, 0)):
                            return {'queued': True, 'recorded': True}
                raise MobileError('Mac 曾重启或处理被中断，无法确认这次请求；请先查看聊天记录，勿直接重发', 409)
            self.receipts = {key: value for key, value in self.receipts.items() if value['at'] > now - 25 * 3600}
            if len(self.receipts) >= 5000:
                raise MobileError('手机请求过多，请稍后再试', 429)
            record = {'at': now, 'signature': signature, 'run': self.run_id}
            self.receipts[request_id] = record
            private_json(self.state / 'mobile-receipts.json', self.receipts)
            try:
                result = self.perform(body)
            except MobileError as exc:
                result = {'error': str(exc)}
            record['result'] = result
            private_json(self.state / 'mobile-receipts.json', self.receipts)
            return result

    def perform(self, body):
        kind = body.get('action')
        if kind == 'new_room':
            project = next((p for p in self.conversations() if p['id'] == body.get('project')), None)
            if not project or not project.get('root'):
                raise MobileError('找不到这个项目')
            rid = self.new_room(project['id'], project['name'], project['root'], '新对话 ' + time.strftime('%m-%d %H:%M'))
            self.tree_at = 0
            return {'room': rid}
        room = self.room(body.get('room'))
        session = self.app.sess(body['room'])
        if kind == 'send':
            text, route, images = body.get('text', ''), body.get('route', 'auto'), body.get('images', [])
            routes = {'auto', 'both', *session.members}
            if 'gemini' in session.members:
                routes.add('all')
            if not isinstance(text, str) or len(text) > 100_000 or route not in routes:
                raise MobileError('消息过长或接收者无效')
            if not isinstance(images, list) or len(images) > 8:
                raise MobileError('每条消息最多附 8 张图片')
            folder = Path(room['dir']) / 'attachments'
            paths = []
            for name in images:
                if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg|gif|webp)', name):
                    raise MobileError('无效附件')
                path = folder / name
                if not path.is_file() or path.resolve().parent != folder.resolve():
                    raise MobileError('附件已不存在')
                paths.append(str(path))
            if not text.strip() and not paths:
                raise MobileError('消息为空')
            session.enqueue('user', text=text, route=route, images=paths, mobile_request_id=body['request_id'])
            return {'queued': True}
        if kind == 'mode':
            if body.get('mode') not in ('normal', 'collaboration'):
                raise MobileError('无效协同模式')
            session.enqueue('mode', mode=body['mode'])
        elif kind == 'stop':
            if body.get('model') not in session.members:
                raise MobileError('无效模型')
            session.stop(body['model'])
        elif kind == 'attach':
            match = re.fullmatch(r'data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)', body.get('data', ''))
            if not match:
                raise MobileError('只支持 PNG、JPEG、GIF 或 WebP 图片')
            try:
                data = base64.b64decode(match[2], validate=True)
            except ValueError:
                raise MobileError('图片编码无效')
            if not data or len(data) > 4_000_000:
                raise MobileError('图片不能超过 4 MB')
            ext = 'jpg' if match[1] == 'jpeg' else match[1]
            name = secrets.token_hex(16) + '.' + ext
            folder = Path(room['dir']) / 'attachments'
            folder.mkdir(exist_ok=True)
            (folder / name).write_bytes(data)
            return {'name': name}
        else:
            raise MobileError('不支持此操作', 404)
        return {'queued': True}
