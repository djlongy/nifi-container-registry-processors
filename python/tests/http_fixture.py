"""A throwaway HTTP server on 127.0.0.1 whose behaviour a test supplies as a function."""
import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Seen:
    def __init__(self):
        self.requests = []   # (method, path, headers, body length)
        self.sent = 0        # body bytes the server managed to write


@contextlib.contextmanager
def serve(respond):
    """respond(handler, seen) answers one request; yields (base url, seen)."""
    seen = Seen()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.requests.append((self.command, self.path, dict(self.headers), len(body)))
            try:
                respond(self, seen)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_PUT = do_POST = do_HEAD = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def stream(handler, seen, size, status=200, headers=None):
    """Answer with size bytes of x, counting how many the client let through."""
    handler.send_response(status)
    handler.send_header("Content-Length", str(size))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    block = b"x" * (1024 * 1024)
    for _ in range(size // len(block)):
        handler.wfile.write(block)
        seen.sent += len(block)


def reply(handler, status=200, body=b"", headers=None):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
