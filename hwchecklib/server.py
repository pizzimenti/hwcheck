"""the http side: one page and a small json api, on 127.0.0.1 only."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import STATIC_DIR, __version__
from .session import SessionError

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
# the page may load nothing from anywhere but this server
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
MAX_BODY = 65536
KEEPALIVE = 10.0
TOKEN_HEADER = "X-Hwcheck-Token"
NO_TOKEN = "open the address hwcheck printed, with its ?t= part"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, session, port: int = 0, static_dir=STATIC_DIR, hibernate_delay: float = 1.0,
                 token: str | None = None):
        self.session = session
        self.static_dir = static_dir
        self.hibernate_delay = hibernate_delay
        # a secret for this run only: every api request must carry it, so another local
        # user or process can neither read the notes nor answer or hibernate
        self.token = token or secrets.token_urlsafe(18)
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server_version = f"hwcheck/{__version__}"
    sys_version = ""

    def log_message(self, fmt, *args):  # quiet: the terminal is for the url and the outcome
        pass

    # -------------------------------------------------------------- plumbing

    def _hosts(self) -> tuple[str, ...]:
        port = self.server.port
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def _host_ok(self) -> bool:
        # refuse other host names so a web page can't reach us by dns rebinding
        return self.headers.get("Host", "") in self._hosts()

    def _token_ok(self, allow_query: bool = False) -> bool:
        got = self.headers.get(TOKEN_HEADER, "")
        if not got and allow_query:  # EventSource can't send headers
            got = parse_qs(urlsplit(self.path).query).get("t", [""])[0]
        return hmac.compare_digest(got.encode("utf-8"), self.server.token.encode("utf-8"))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # -------------------------------------------------------------- GET

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "wrong host")
        path = urlsplit(self.path).path
        if path == "/api/state":
            if not self._token_ok():
                return self._error(403, NO_TOKEN)
            return self._json(200, self.server.session.snapshot())
        if path == "/api/events":
            if not self._token_ok(allow_query=True):
                return self._error(403, NO_TOKEN)
            return self._events()
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                body = (self.server.static_dir / name).read_bytes()
            except OSError:
                return self._error(500, f"missing page file {name}")
            extra = {"Content-Security-Policy": CSP} if name.endswith(".html") else None
            return self._send(200, body, ctype, extra)
        return self._error(404, "not found")

    def _events(self) -> None:
        """server-sent events: the whole state whenever it changes."""
        session = self.server.session
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        last = -1
        while not session.stopping.is_set():
            with session.cond:
                if session.version == last:
                    session.cond.wait(KEEPALIVE)
                if session.stopping.is_set():
                    break
                snap = session.snapshot() if session.version != last else None
            try:
                if snap is not None:
                    self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode("utf-8"))
                    last = snap["version"]
                else:
                    self.wfile.write(b": still here\n\n")
                self.wfile.flush()
            except OSError:
                return

    # -------------------------------------------------------------- POST

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "wrong host")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in tuple(f"http://{h}" for h in self._hosts()):
            return self._error(403, "requests from other sites are refused")
        if not self._token_ok():
            return self._error(403, NO_TOKEN)
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "send json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "bad content-length")
        if length < 0 or length > MAX_BODY:
            return self._error(413, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error(400, "bad json")
        if not isinstance(body, dict):
            return self._error(400, "send a json object")
        session = self.server.session
        path = urlsplit(self.path).path
        try:
            if path == "/api/answer":
                snap = session.answer(body.get("id"), body.get("answer"), body.get("note", ""),
                                      body.get("observed"))
            elif path == "/api/select":
                snap = session.select(body.get("id"))
            elif path == "/api/hibernate":
                snap = session.start_hibernate()
                session.launch_hibernate(self.server.hibernate_delay)
            else:
                return self._error(404, "not found")
        except SessionError as exc:
            return self._error(exc.status, str(exc))
        return self._json(200, snap)


def serve(session, server: Server, timeout: float | None = None, grace: float = 1.5,
          poll_interval: float = 1.0, clock=time.monotonic, stop_requested=None) -> str:
    """run until every check is answered, --timeout passes, or something stops the session.

    returns "finished", "timeout", "stopped" or "interrupted". monotonic time doesn't
    advance while the machine sleeps, so hibernating doesn't eat into the timeout.
    """
    # the timeout counts from before anything is served, so no answer can come in ahead of it
    started = clock()
    web = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                           name=f"hwcheck-http-{server.port}", daemon=True)
    poller = threading.Thread(target=session.poll_loop, args=(poll_interval,),
                              name=f"hwcheck-poll-{server.port}", daemon=True)
    web.start()
    poller.start()
    outcome = "stopped"
    try:
        while True:
            if session.finished.is_set():
                session.stopping.wait(grace)  # let the page get the final state
                outcome = "finished"
                break
            if session.stopping.is_set() or (stop_requested is not None and stop_requested()):
                outcome = "stopped"  # a signal, recorded by its handler: stopped here, outside it
                break
            if timeout and clock() - started >= timeout:
                outcome = "timeout"
                break
            session.finished.wait(0.2)
    except KeyboardInterrupt:
        outcome = "interrupted"
    finally:
        session.stop()  # from here nothing is written: all before the caller gives up its lock
        server.shutdown()
        # the request threads are daemon threads and server_close() joins none of them; a request
        # still running can't write after this point because stop() set the stopping flag
        # under the session lock, and every save checks that flag under the same lock
        server.server_close()
        poller.join(5)
        web.join(5)
        session.join_background(2.0)
        session.readers.release()
    return outcome
