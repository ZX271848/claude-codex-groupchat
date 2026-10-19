"""Private HTTPS ingress. Never bind the companion to a LAN interface or use Funnel."""
from email.header import decode_header, make_header
import json
from pathlib import Path
import re
import shutil
import subprocess


class TransportError(Exception):
    pass


class TailscaleServe:
    https_port = 8443

    def __init__(self):
        self.cli = None
        self.host = self.login = self.target = ''
        self.active = False

    def run(self, *args):
        if not self.cli:
            candidates = [shutil.which('tailscale'), '/Applications/Tailscale.app/Contents/MacOS/Tailscale',
                          str(Path.home() / 'Applications/Tailscale.app/Contents/MacOS/Tailscale')]
            self.cli = next((p for p in candidates if p and Path(p).is_file()), None)
        if not self.cli:
            raise TransportError('请先安装 Tailscale，并让 Mac 与 iPhone 登录同一账号')
        try:
            result = subprocess.run([self.cli, *args], capture_output=True, text=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            raise TransportError('Tailscale 未响应。请确认已登录，并已启用 HTTPS / Serve；连接保持关闭') from None
        if result.returncode:
            raise TransportError('Tailscale 配置未就绪，请检查登录、HTTPS 和 Serve 设置；连接保持关闭')
        return result.stdout

    def read(self, *args):
        try:
            value = json.loads(self.run(*args, '--json'))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise TransportError('无法验证 Tailscale 配置；连接保持关闭') from None

    @staticmethod
    def configs(config):
        yield config
        for child in (config.get('Foreground') or {}).values():
            yield from TailscaleServe.configs(child)

    def owned_config(self, config):
        return (config.get('TCP', {}).get(str(self.https_port)) == {'HTTPS': True}
                and config.get('Web', {}).get(self.host) == {'Handlers': {'/': {'Proxy': self.target}}})

    def check(self, config, required=False):
        found = False
        for part in self.configs(config):
            if any((part.get('AllowFunnel') or {}).values()):
                raise TransportError('检测到公网 Funnel 配置。请先关闭 Funnel；群聊拒绝开启连接')
            uses_port = (str(self.https_port) in (part.get('TCP') or {})
                         or any(k.endswith(':' + str(self.https_port)) for k in (part.get('Web') or {})))
            if uses_port:
                # Never replace another app's endpoint or a foreground Serve session.
                if part is not config or not self.owned_config(part):
                    raise TransportError('Tailscale HTTPS 8443 已用于其他服务；群聊没有覆盖该配置')
                found = True
        if required and not found:
            raise TransportError('未能验证私有 HTTPS 代理；连接保持关闭')

    def start(self, port):
        status = self.read('status')
        node = status.get('Self') or {}
        dns = node.get('DNSName', '').rstrip('.').lower()
        login = (status.get('User') or {}).get(str(node.get('UserID')), {}).get('LoginName', '')
        if status.get('BackendState') != 'Running' or not node.get('Online') or not login:
            raise TransportError('请在 Mac 的 Tailscale 中完成登录并连接；手机需使用同一账号')
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net', dns):
            raise TransportError('Tailscale 的私有 DNS 地址尚未就绪')
        if dns not in [d.rstrip('.').lower() for d in (status.get('CertDomains') or [])]:
            raise TransportError('请先在 Tailscale 启用 HTTPS / Serve，参见手机使用说明；连接保持关闭')
        self.host, self.login = f'{dns}:{self.https_port}', login
        self.target = f'http://127.0.0.1:{port}'
        self.check(self.read('serve', 'status'))
        self.run('serve', '--bg', f'--https={self.https_port}', self.target)
        self.active = True
        self.check(self.read('serve', 'status'), required=True)

    def stop(self):
        self.active = False
        if not self.host:
            return
        try:
            config = self.read('serve', 'status')
            self.check(config, required=True)
            self.run('serve', '--bg', f'--https={self.https_port}', 'off')
        except TransportError:
            pass  # Backend is closed regardless; never reset another app's settings.

    def accepts(self, headers, peer):
        # Serve overwrites these identity headers and omits them for public Funnel traffic.
        # Only local processes can reach this backend; they are in the desktop trust boundary.
        try:
            login = str(make_header(decode_header(headers.get('Tailscale-User-Login', ''))))
        except (ValueError, UnicodeError, LookupError):
            return False
        singles = ('Host', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'Tailscale-User-Login')
        return (self.active and peer == '127.0.0.1'
                and all(len(headers.get_all(k, [])) == 1 for k in singles)
                and headers.get('Host') == self.host
                and headers.get('X-Forwarded-Host') == self.host
                and headers.get('X-Forwarded-Proto') == 'https'
                and not headers.get('Tailscale-Funnel-Request')
                and bool(self.login) and login == self.login)
