"""Bounded public HTTP reads. DNS answers are validated and pinned per connection."""
from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
import time
from threading import RLock
from threading import Thread, Timer
from queue import Queue, Empty
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser

USER_AGENT = 'CBSPartner/1.0 (local AI partner; read-only personal reader)'


class FetchError(ValueError):
    pass


def in_domains(url, domains):
    p = urlsplit(url)
    host = (p.hostname or '').lower().rstrip('.')
    return (p.scheme in ('http', 'https') and not p.username and not p.password
            and any(host == d or host.endswith('.' + d) for d in domains))


def public_url(url, resolver=socket.getaddrinfo, timeout=None):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise FetchError('http / https の公開URLを指定してください。')
    try:
        port = p.port or (443 if p.scheme == 'https' else 80)
        if timeout is None:
            addresses = resolver(p.hostname, port, type=socket.SOCK_STREAM)
        else:
            answers = Queue(maxsize=1)
            def resolve():
                try:
                    answers.put(resolver(p.hostname, port, type=socket.SOCK_STREAM))
                except Exception:
                    answers.put([])
            Thread(target=resolve, daemon=True).start()
            try:
                addresses = answers.get(timeout=max(.001, timeout))
            except Empty:
                raise FetchError('名前解決の時間の上限を超えました。') from None
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise FetchError('このパソコンや社内ネットワークのアドレスや名前解決できないURLは取得できません。') from None
    return p, addresses[0][4][0], port


@dataclass
class Page:
    url: str
    body: bytes
    content_type: str = ''

    def text(self, encoding=None):
        import re
        match = re.search(r'charset=["\']?([\w-]+)', self.content_type, re.I)
        if not match and not encoding:
            # Older RSS-less HTML often declares Shift_JIS only in <meta>.
            head = self.body[:4096].decode('ascii', errors='ignore')
            match = re.search(r'<meta\b[^>]*charset\s*=\s*["\']?([\w-]+)', head, re.I)
        try:
            return self.body.decode(encoding or (match[1] if match else 'utf-8'), errors='replace')
        except LookupError:
            return self.body.decode('utf-8', errors='replace')


class PublicFetcher:
    def __init__(self, *, resolver=socket.getaddrinfo, transport=None, interval=1.0, timeout=15, max_bytes=12_000_000, blocked_domains=()):
        self.blocked_domains = blocked_domains
        self.resolver, self.transport = resolver, transport or self._request
        self.interval, self.timeout, self.max_bytes = interval, timeout, max_bytes
        self.last, self.robots, self.delays = {}, {}, {}
        self.lock = RLock()

    def _request(self, url, ip, port, timeout, limit):
        deadline = time.monotonic() + timeout
        p = urlsplit(url)
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        timer = None
        try:
            conn.connect()
            conn.sock.settimeout(max(.01, deadline-time.monotonic()))
            if p.scheme == 'https':
                context = ssl.create_default_context()
                # Windows enterprise roots may predate RFC 5280 strict constraints.
                # Keep chain/hostname validation; use the platform's compatible policy.
                context.verify_flags &= ~ssl.VERIFY_X509_STRICT
                conn.sock = context.wrap_socket(conn.sock, server_hostname=p.hostname)
            conn.sock.settimeout(max(.01, deadline-time.monotonic()))
            stream_socket = conn.sock
            def stop():
                try:
                    stream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # An absolute deadline also bounds slowly trickled response headers.
            timer = Timer(max(.001, deadline-time.monotonic()), stop)
            timer.daemon = True
            timer.start()
            conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                         headers={'Host': p.netloc, 'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if int(response.getheader('Content-Length') or 0) > limit:
                raise FetchError('取得サイズの上限を超えました。')
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchError('取得時間の上限を超えました。')
                stream_socket.settimeout(remaining)
                chunk = response.read1(min(65536, limit + 1 - len(body)))
                body.extend(chunk)
                if len(body) > limit:
                    raise FetchError('取得サイズの上限を超えました。')
                if not chunk:
                    break
            return response.status, dict(response.getheaders()), bytes(body)
        finally:
            if timer:
                timer.cancel()
            conn.close()

    def _get(self, url, limit, *, robots=False, deadline=None):
        deadline = deadline or time.monotonic() + self.timeout
        for _ in range(6):
            if in_domains(url, self.blocked_domains):
                raise FetchError('買い物サイトは検索結果だけを読みます。直接取得はできません。')
            p, ip, port = public_url(url, self.resolver, deadline-time.monotonic())
            origin = f'{p.scheme}://{p.netloc}'
            if not robots:
                self._allow(url, origin, deadline)
            wait = max(0, self.last.get(origin, 0) + self.delays.get(origin, self.interval) - time.monotonic())
            if time.monotonic() + wait >= deadline:
                raise FetchError('取得時間の上限を超えました。')
            time.sleep(wait)
            self.last[origin] = time.monotonic()
            status, headers, body = self.transport(url, ip, port, max(.01, deadline-time.monotonic()), limit)
            headers = {k.lower(): v for k, v in headers.items()}
            if len(body) > limit or time.monotonic() > deadline:
                raise FetchError('取得サイズまたは時間の上限を超えました。')
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, headers.get('location', ''))
                continue
            if robots and status in (404, 410):
                return Page(url, b'')
            if status != 200:
                raise FetchError(f'取得先の応答: HTTP {status}')
            return Page(url, body, headers.get('content-type', ''))
        raise FetchError('転送が多すぎます。')

    def _allow(self, url, origin, deadline):
        cached = self.robots.get(origin)
        if not cached or time.monotonic() - cached[0] > 3600:
            page = self._get(origin + '/robots.txt', 256_000, robots=True, deadline=deadline)
            parser = RobotFileParser()
            parser.parse(page.text().splitlines())
            self.robots[origin] = (time.monotonic(), parser)
        parser = self.robots[origin][1]
        if not parser.can_fetch(USER_AGENT, url):
            raise FetchError('robots.txt により取得できません。')
        delay = parser.crawl_delay(USER_AGENT) or 0
        self.delays[origin] = max(self.interval, delay)

    def __call__(self, url, *, max_bytes=None):
        with self.lock:
            try:
                return self._get(url, min(max_bytes or self.max_bytes, self.max_bytes))
            except FetchError:
                raise
            except Exception:
                raise FetchError('取得に失敗しました。URLと接続を確認してください。') from None
