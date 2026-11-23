"""RegistryClient against a local HTTP server."""
import io
import unittest
from unittest import mock

import nifi_stub  # noqa: F401
from http_fixture import reply, serve, stream

import container_registry_client as c

MiB = 1024 * 1024
DIGEST = "sha256:" + "a" * 64


class BoundedReads(unittest.TestCase):
    """A registry cannot make the client hold more than a small response, whatever it sends."""

    def test_a_manifest_over_the_cap_is_refused_while_it_is_read(self):
        with serve(lambda h, s: stream(h, s, 64 * MiB)) as (url, seen):
            raising = c.RegistryClient(url)
            with self.assertRaises(c.RegistryError):
                raising.get_manifest("team/app", DIGEST)
        self.assertLess(seen.sent, 32 * MiB)

    def test_a_token_answer_over_the_cap_is_refused(self):
        def respond(handler, seen):
            if handler.path.startswith("/token"):
                stream(handler, seen, 64 * MiB)
            else:
                reply(handler, 401, headers={"WWW-Authenticate": f'Bearer realm="{base[0]}/token"'})
        with serve(respond) as (url, seen):
            base = [url]
            raising = c.RegistryClient(url)
            with self.assertRaises(c.RegistryError):
                raising.get_manifest("team/app", "latest")
        self.assertLess(seen.sent, 32 * MiB)




class RegistrySuppliedUrls(unittest.TestCase):
    """A registry names where blobs are sent to; that must not reach what only this host can reach."""
    def test_an_https_registry_cannot_send_an_upload_to_plain_http(self):
        client = c.RegistryClient("https://registry.example.internal")
        with self.assertRaises(c.RegistryError):
            client._guard("http://storage.example.internal/up/1", "upload Location")

    def test_other_schemes_are_refused(self):
        client = c.RegistryClient("http://registry.example.internal")
        with self.assertRaises(c.RegistryError):
            client._guard("file:///etc/passwd", "upload Location")

    def test_only_the_registry_host_counts_as_its_own(self):
        client = c.RegistryClient("https://registry.example.internal")
        self.assertTrue(client._guard("https://registry.example.internal/v2/x/blobs/uploads/1", "upload Location"))
        self.assertFalse(client._guard("https://storage.example.internal/up/1", "upload Location"))

    def test_which_addresses_count_as_storage(self):
        client = c.RegistryClient("https://registry.example.internal", trusted_hosts="minio.internal, 10.20.0.0/16")
        for ip in ("127.0.0.1", "::1", "169.254.169.254", "10.1.2.3", "192.168.0.9", "::ffff:127.0.0.1", "fd00::1"):
            with self.assertRaises(c.RegistryError, msg=ip):
                client._check_storage("storage.example.com", ip)
        for host, ip in (("storage.example.com", "52.1.2.3"), ("minio.internal", "10.1.2.3"), ("x", "10.20.30.40")):
            client._check_storage(host, ip)


class SecretsStayOutOfMessages(unittest.TestCase):
    """RegistryError text becomes registry.error and the NiFi log, so it holds no password or pre-signed signature."""
    PASSWORD, SIGNATURE = "hunter2-canary", "X-Amz-Signature-canary"

    def test_a_registry_url_with_a_password_is_refused_without_echoing_it(self):
        for url in (f"https://robot:{self.PASSWORD}@registry.example.internal", f"robot:{self.PASSWORD}@registry.example.internal"):
            with self.assertRaises(c.RegistryError) as raised:
                c.RegistryClient(url)
            self.assertNotIn(self.PASSWORD, str(raised.exception))


    def test_redact_keeps_only_scheme_host_and_path(self):
        self.assertEqual(c.redact(f"https://u:{self.PASSWORD}@h.example:9000/a/b?sig={self.SIGNATURE}#f"),
                         "https://h.example:9000/a/b")


class TokenRealmErrors(unittest.TestCase):
    """The query of a token realm can be a signed or secret value; no error built from the realm may show it."""
    CANARY = "REDACTION_CANARY"
    REALM = f"https://auth.example/token?ticket={CANARY}&client_secret={CANARY}"

    def authorize(self, status, body):
        response = io.BytesIO(body)
        response.status = status
        client = c.RegistryClient("https://registry.example")
        with mock.patch.object(client, "_open", return_value=response):
            with self.assertRaises(c.RegistryError) as raised:
                client._authorize(f'Bearer realm="{self.REALM}"', "team/app", "pull", None)
        return str(raised.exception)

    def check(self, message):
        self.assertNotIn(self.CANARY, message)
        self.assertIn("https://auth.example/token", message)

    def test_a_refused_token_request(self):
        self.check(self.authorize(403, b"denied"))

    def test_a_refusal_that_echoes_the_realm_query_in_its_body(self):
        message = self.authorize(403, f"denied {self.REALM}".encode())
        self.check(message)
        self.assertIn("HTTP 403", message)

    def test_an_oversized_token_answer(self):
        self.check(self.authorize(200, b"x" * (c.CONTROL_LIMIT + 1)))

    def test_an_answer_without_a_token(self):
        self.check(self.authorize(200, b"{}"))

    def test_an_unknown_challenge_shows_its_scheme_only(self):
        client = c.RegistryClient("https://registry.example")
        with self.assertRaises(c.RegistryError) as raised:
            client._authorize(f'Negotiate realm="{self.REALM}"', "team/app", "pull", None)
        self.assertNotIn(self.CANARY, str(raised.exception))


class Rebinding(unittest.TestCase):
    """A registry name that resolves somewhere else after its first answer must not receive credentials or blobs."""
    CANARY = "canary-1234"

    def setUp(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.rebound = False
        self.rebind_on_redirect = False
        self.hits = []
        test = self

        class Registry(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self.headers.get("Authorization"):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="r"')
                    self.send_header("Content-Length", "0")
                else:
                    test.rebound = test.rebound or test.rebind_on_redirect
                    self.send_response(302)
                    self.send_header("Location", f"http://rebind.test:{test.port}/latest/meta-data/")
                    self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        class Other(BaseHTTPRequestHandler):
            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                test.hits.append((self.command, self.path, self.headers.get("Authorization")))
                body = b"metadata-canary"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_PUT = do_POST = handle_any

            def log_message(self, *args):
                pass

        try:
            self.first = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
            self.port = self.first.server_address[1]
            self.second = ThreadingHTTPServer(("127.0.0.2", self.port), Other)
        except OSError as error:
            if hasattr(self, "first"):
                self.first.server_close()
            self.skipTest(f"cannot bind 127.0.0.2: {error}")
        for server in (self.first, self.second):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        real = socket.getaddrinfo

        def resolve(host, port, *args, **kwargs):
            if host == "rebind.test":
                return real("127.0.0.2" if self.rebound else "127.0.0.1", port, *args, **kwargs)
            return real(host, port, *args, **kwargs)
        patcher = mock.patch("socket.getaddrinfo", resolve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_redirect_to_the_registry_name_at_another_address_gets_no_credentials(self):
        client = c.RegistryClient(f"http://rebind.test:{self.port}", "robot", self.CANARY)
        client.blob_exists("t/a", DIGEST)  # the first connection pins 127.0.0.1
        self.rebound = True
        with self.assertRaises(c.RegistryError) as raised:
            client.get_manifest("t/a", "latest")
        self.assertIn("not a public address", str(raised.exception))
        self.assertEqual(self.hits, [])

    def test_an_upload_to_the_registry_name_at_another_address_is_refused(self):
        client = c.RegistryClient(f"http://rebind.test:{self.port}", "robot", self.CANARY)
        client.blob_exists("t/a", DIGEST)
        self.rebound = True
        with self.assertRaises(c.RegistryError):
            client.start_upload("t/a")
        self.assertEqual(self.hits, [])

    def test_the_pinned_address_is_used_again_without_trouble(self):
        client = c.RegistryClient(f"http://rebind.test:{self.port}", "robot", self.CANARY)
        client.blob_exists("t/a", DIGEST)
        client.blob_exists("t/b", DIGEST)
        self.assertEqual(client.pins, {"rebind.test": {"127.0.0.1"}})

    def test_a_trusted_address_the_name_was_re_pointed_at_is_reached_without_credentials(self):
        client = c.RegistryClient(f"http://rebind.test:{self.port}", "robot", self.CANARY, trusted_hosts="127.0.0.2")
        client.blob_exists("t/a", DIGEST)
        client.auth[("t/a", "pull", None)] = "Basic " + self.CANARY
        self.rebound = True
        self.assertEqual(client.get_manifest("t/a", "latest")[0], b"metadata-canary")
        self.assertEqual(self.hits, [("GET", "/v2/t/a/manifests/latest", None)])

    def test_a_public_address_the_name_was_re_pointed_at_gets_no_credentials(self):
        client = c.RegistryClient("http://registry.example", "robot", self.CANARY)
        client.pins["registry.example"] = {"10.1.2.3"}
        self.assertIs(client._check_pinned("registry.example", "8.8.8.8", ["8.8.8.8"]), False)
        self.assertIs(client._check_pinned("registry.example", "10.1.2.3", ["10.1.2.3"]), True)
        with self.assertRaises(c.RegistryError):
            client._check_pinned("registry.example", "169.254.169.254", ["169.254.169.254"])

    def test_an_upload_to_a_trusted_address_the_name_was_re_pointed_at_gets_no_credentials(self):
        client = c.RegistryClient(f"http://rebind.test:{self.port}", "robot", self.CANARY, trusted_hosts="127.0.0.2")
        client.blob_exists("t/a", DIGEST)
        client.auth[("t/a", "pull,push", None)] = "Basic " + self.CANARY
        self.rebound = True
        with self.assertRaises(c.RegistryError):  # the other server answers 200, not 202
            client.start_upload("t/a")
        self.assertEqual([hit[2] for hit in self.hits], [None])


class TwoAddressRegistry(unittest.TestCase):
    """A registry name with two addresses (round-robin DNS, an HA pair) answers on both, whichever a lookup lists first."""
    def setUp(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.served = []

        class Registry(BaseHTTPRequestHandler):
            def do_HEAD(handler):
                self.served.append(handler.server.server_address[0])
                handler.send_response(200)
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            def log_message(self, *args):
                pass

        try:
            self.first = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
            self.port = self.first.server_address[1]
            self.second = ThreadingHTTPServer(("127.0.0.2", self.port), Registry)
        except OSError as error:
            if hasattr(self, "first"):
                self.first.server_close()
            self.skipTest(f"cannot bind 127.0.0.2: {error}")
        for server in (self.first, self.second):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        real, self.answers = socket.getaddrinfo, 0

        def resolve(host, port, *args, **kwargs):
            if host != "registry.internal":
                return real(host, port, *args, **kwargs)
            self.answers += 1
            order = ("127.0.0.1", "127.0.0.2") if self.answers % 2 else ("127.0.0.2", "127.0.0.1")
            return [info for address in order for info in real(address, port, *args, **kwargs)]
        patcher = mock.patch("socket.getaddrinfo", resolve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_address_of_the_registry_name_is_served(self):
        client = c.RegistryClient(f"http://registry.internal:{self.port}", "robot", "secret")
        for _ in range(4):
            self.assertTrue(client.blob_exists("t/a", DIGEST))
        self.assertEqual(set(self.served), {"127.0.0.1", "127.0.0.2"})




if __name__ == "__main__":
    unittest.main()


class UploadLocationRepository(unittest.TestCase):
    """An upload goes to the repository it was started for, whatever the registry's Location says."""
    @staticmethod
    def location(requested, header, url="https://registry.example.internal"):
        response = mock.Mock(headers={"Location": header})
        return c.RegistryClient(url)._upload_location(response, requested)

    def test_a_location_in_the_requested_repository_is_kept(self):
        for header in ("/v2/team/app/blobs/uploads/id?_state=x",
                       "https://registry.example.internal/v2/team/app/blobs/uploads/id?_state=x"):
            self.assertEqual(self.location("team/app", header),
                             "https://registry.example.internal/v2/team/app/blobs/uploads/id?_state=x")

    def test_a_location_in_another_repository_of_the_registry_is_refused(self):
        with self.assertRaises(c.RegistryError) as raised:
            self.location("itis-dev/library/busybox", "/v2/library/busybox/blobs/uploads/id?_state=x")
        self.assertIn("library/busybox", str(raised.exception))

    def test_a_location_on_another_host_is_left_to_the_host_check(self):
        header = "https://storage.example.internal/v2/other/blobs/uploads/id"
        self.assertEqual(self.location("team/app", header), header)
