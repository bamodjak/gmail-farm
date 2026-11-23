"""A tiny local forwarding proxy that adds upstream authentication.

Why this exists: headless Chrome does not reliably use an
authenticated proxy. ``--proxy-server=http://user:pass@host:port``
is silently ignored by Chrome (it resolves to ``direct://`` — proven
by Chrome's own net log: ``proxy_chain: [direct://]``), and the
``https_proxy`` environment variable is only picked up
intermittently. The robust fix is to take Chrome out of the auth
equation entirely: Chrome talks to this proxy on localhost with NO
credentials (``--proxy-server=http://127.0.0.1:<port>``), and this
proxy adds the ``Proxy-Authorization: Basic`` header when it talks
to the real upstream proxy.

Only the standard library is used, so the tool stays portable
(Windows / Mac / Linux). The proxy binds to 127.0.0.1 only and is
started per browser session, then stopped when the driver closes —
nothing listens after the attempt ends.

The upstream proxy URL (with credentials) is the one the user
supplied via ``--proxy`` / ``GMAIL_FARM_PROXY`` or the environment
(``https_proxy`` / ``HTTPS_PROXY`` / ...). The credentials are used
only to build the ``Proxy-Authorization`` header for the upstream
connection; they are never logged.
"""

import base64
import socket
import socketserver
import threading
from urllib.parse import urlparse


class _ForwardHandler(socketserver.BaseRequestHandler):
    """One client connection: forward it to the upstream proxy."""

    def handle(self):
        # Read the request head (request line + headers).
        buf = b""
        try:
            self.request.settimeout(30)
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buf += chunk
        except OSError:
            return
        head, _, _rest = buf.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", "replace").split("\r\n")
        if not lines:
            return
        method, target, _version = (lines[0].split() + ["", ""])[:3]
        if method.upper() == "CONNECT":
            self._handle_connect(target)
        else:
            self._handle_plain_http(method, target, lines[1:])

    # -- CONNECT (HTTPS) -------------------------------------------------
    def _handle_connect(self, target):
        upstream = self.server.upstream
        try:
            sock = socket.create_connection((upstream.hostname,
                                             upstream.port), timeout=30)
        except OSError:
            self._send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        try:
            auth = self.server.auth_header
            req = (f"CONNECT {target} HTTP/1.1\r\n"
                   f"Host: {target}\r\n")
            if auth:
                req += f"Proxy-Authorization: {auth}\r\n"
            req += "\r\n"
            sock.sendall(req.encode("iso-8859-1"))
            resp = self._read_head(sock)
            if not resp.startswith(b"HTTP/1.1 200") \
                    and not resp.startswith(b"HTTP/1.0 200"):
                self._send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            self._send(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # -- plain HTTP (absolute-URI request) --------------------------------
    def _handle_plain_http(self, method, target, header_lines):
        upstream = self.server.upstream
        try:
            sock = socket.create_connection((upstream.hostname,
                                             upstream.port), timeout=30)
        except OSError:
            self._send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        try:
            auth = self.server.auth_header
            out = [f"{method} {target} HTTP/1.1"]
            for line in header_lines:
                if line.lower().startswith("proxy-authorization:"):
                    continue
                out.append(line)
            if auth:
                out.append(f"Proxy-Authorization: {auth}")
            out.append("")
            out.append("")
            sock.sendall("\r\n".join(out).encode("iso-8859-1"))
            self._relay(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # -- helpers -----------------------------------------------------------
    def _read_head(self, sock):
        buf = b""
        sock.settimeout(30)
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    def _send(self, data):
        try:
            self.request.sendall(data)
        except OSError:
            pass

    def _relay(self, upstream_sock):
        """Copy bytes both ways until either side closes."""
        client = self.request
        client.settimeout(60)
        upstream_sock.settimeout(60)
        stop = threading.Event()

        def _pump(src, dst):
            try:
                while not stop.is_set():
                    try:
                        data = src.recv(65536)
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        dst.sendall(data)
                    except OSError:
                        break
            finally:
                stop.set()
                for s in (client, upstream_sock):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t1 = threading.Thread(target=_pump, args=(client, upstream_sock),
                              daemon=True)
        t2 = threading.Thread(target=_pump, args=(upstream_sock, client),
                              daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalAuthProxy:
    """Localhost proxy that forwards to an upstream proxy with auth.

    ``upstream_url`` is the full upstream proxy URL, credentials
    included (``http://user:pass@host:port``). Use
    :attr:`proxy_url` (``http://127.0.0.1:<port>``, no credentials)
    as Chrome's ``--proxy-server``.
    """

    def __init__(self, upstream_url):
        upstream = urlparse(upstream_url)
        if not upstream.hostname or not upstream.port:
            raise ValueError(f"bad upstream proxy URL: {upstream_url!r}")
        self.upstream = upstream
        self.auth_header = None
        if upstream.username is not None:
            user = upstream.username
            password = upstream.password or ""
            token = base64.b64encode(
                f"{user}:{password}".encode("utf-8")).decode("ascii")
            self.auth_header = f"Basic {token}"
        self._server = _Server(("127.0.0.1", 0), _ForwardHandler)
        self._server.upstream = upstream
        self._server.auth_header = self.auth_header
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def proxy_url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        try:
            self._server.shutdown()
        except OSError:
            pass
        try:
            self._server.server_close()
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
