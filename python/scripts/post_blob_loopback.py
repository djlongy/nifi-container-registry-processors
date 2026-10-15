#!/usr/bin/env python3
"""Stream one registry blob into a ListenHTTP on this node, for the loopback flow. For NiFi ExecuteStreamCommand.

usage: post_blob_loopback.py <registry URL> <repository> <blob digest> <port> [options]
  --path       ListenHTTP Base Path (default blobs)
  --name       the name in the ListenHTTP certificate, sent as TLS SNI and Host (default nifi-loopback). Jetty refuses
               an IP address as the host (400 Invalid SNI), so the connection goes to 127.0.0.1 under this name.
  --ca, --cert, --key   PEM files: the CA that signed the ListenHTTP certificate, and this client's certificate and
               key for mTLS. Defaults from LOOPBACK_CA, LOOPBACK_CERT, LOOPBACK_KEY.
  --header     NAME=VALUE sent with the POST, repeatable. ListenHTTP turns the ones its HTTP Headers to receive as
               Attributes regex matches (such as X-.*) into FlowFile attributes. X-Blob-Digest, filename and
               X-Attempt are always sent.
  --trusted-storage   hosts, IPs or CIDR ranges (comma or space separated) the registry may redirect a blob to although
               they are private, such as an on-premises MinIO: minio.internal,10.20.0.0/16. Default from
               LOOPBACK_TRUSTED_STORAGE. Without it a redirect may only go to a public address.
  --attempts   tries before giving up (default 6), with a wait of 2, 4, 8 ... up to 30 seconds between them
  --scheme     https (default) or http (for a test)
env:   REGISTRY_USER, REGISTRY_PASSWORD (empty password: no credentials, for an open registry)
A redirect is followed (at most 3 hops) only to http or https, never to plain http from an https registry, never with the
registry credentials unless it stays on the registry's own origin, and only to an address that is public or trusted.
Each attempt reads the blob from the registry again and copies it in 1 MiB chunks into a chunked POST, so memory stays
flat. ListenHTTP checks back pressure only on every 5th request and then closes the connection, so a retry is normal.
Exit codes: 0 delivered, 6 not delivered after all attempts, 10 credentials refused, 15 bad arguments, a refused
redirect, or a network error reaching the registry for the token. Needs push_from_stdin.py beside it. stdout: one JSON line. stderr: one line per try."""
import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import push_from_stdin as push

CHUNK = 1 << 20
MAX_HOPS = 3
REDIRECTS = (301, 302, 303, 307, 308)


class Refused(Exception):
    """A redirect the script will not follow. Not an OSError: retrying cannot change the answer."""


def trust(entries):
    """(names, networks) from a comma or space separated list of host names, IPs and CIDR ranges."""
    names, nets = set(), []
    for entry in re.split(r"[,\s]+", entries or ""):
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry.strip("[]"), strict=False))
        except ValueError:
            names.add(entry.lower())
    return names, nets


def check_storage(host, ip, trusted):
    """Raise Refused unless host, connected at ip, is a public address or trusted. Loopback, link-local (cloud
    metadata), private and similar addresses are refused, as RegistryClient._check_storage refuses them."""
    address = ipaddress.ip_address(ip.split("%")[0])
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    names, nets = trusted
    if ((host or "").lower().strip("[]") in names or any(address in net for net in nets if net.version == address.version)
            or (address.is_global and not address.is_multicast)):
        return
    raise Refused(f"the registry redirected to {host}, which is {address}, not a public address; "
                  "add it to --trusted-storage if it is the registry's object storage")


class Checked:
    """The connection check: the address actually connected to is vetted before a byte is sent."""

    def open_socket(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.check(self.host, sock.getpeername()[0])
        except BaseException:
            sock.close()
            raise
        return sock


class CheckedHTTP(Checked, http.client.HTTPConnection):
    def __init__(self, *args, check, **kwargs):
        super().__init__(*args, **kwargs)
        self.check = check

    def connect(self):
        self.sock = self.open_socket()


class CheckedHTTPS(Checked, http.client.HTTPSConnection):
    def __init__(self, *args, check, **kwargs):
        super().__init__(*args, **kwargs)
        self.check = check

    def connect(self):
        self.sock = self._context.wrap_socket(self.open_socket(), server_hostname=self.host)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def storage_opener(trusted):
    check = lambda host, ip: check_storage(host, ip, trusted)
    http_handler, https_handler = urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()
    http_handler.http_open = lambda req: http_handler.do_open(lambda host, **kw: CheckedHTTP(host, check=check, **kw), req)
    https_handler.https_open = lambda req: https_handler.do_open(
        lambda host, **kw: CheckedHTTPS(host, check=check, **kw), req, context=ssl.create_default_context())
    return urllib.request.build_opener(NoRedirect, http_handler, https_handler)


class Loopback(http.client.HTTPSConnection):
    """Connect to the given address but present `name` as the TLS server name."""

    def __init__(self, host, port, name, **kwargs):
        super().__init__(host, port, **kwargs)
        self.name = name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.name)


def open_blob(url, repository, digest, headers, trusted=(set(), [])):
    """The registry's answer for the blob. A redirect (object storage) is followed without the registry credentials
    unless it stays on the registry's own origin, and only to http(s) at a public or trusted address."""
    origin = urllib.parse.urlsplit(url)
    current = f"{url}/v2/{repository}/blobs/{digest}"
    own_opener, foreign_opener = urllib.request.build_opener(NoRedirect), storage_opener(trusted)
    for hop in range(MAX_HOPS + 1):
        here = urllib.parse.urlsplit(current)
        own = (here.scheme, here.netloc) == (origin.scheme, origin.netloc)
        try:
            return (own_opener if own else foreign_opener).open(
                urllib.request.Request(current, headers=headers if own else {}), timeout=600)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code not in REDIRECTS or not location:
                raise
            error.close()
            current = urllib.parse.urljoin(current, location)
        target = urllib.parse.urlsplit(current)
        if target.scheme not in ("http", "https") or not target.hostname:
            raise Refused(f"the registry redirected to {target.scheme or 'a non-URL'}:, refused")
        if origin.scheme == "https" and target.scheme != "https":
            raise Refused(f"the registry redirected to plain http on {target.hostname} from an https registry, refused")
    raise Refused(f"more than {MAX_HOPS} redirects")


def connection(args, context):
    if args.scheme == "http":
        return http.client.HTTPConnection("127.0.0.1", args.port, timeout=600)
    return Loopback("127.0.0.1", args.port, args.name, context=context, timeout=600)


def deliver(args, context, headers, sent):
    with open_blob(args.url, args.repository, args.digest, headers, args.trusted) as blob:
        def body():
            while True:
                chunk = blob.read(CHUNK)
                if not chunk:
                    return
                sent[0] += len(chunk)
                yield chunk

        link = connection(args, context)
        try:
            link.request("POST", "/" + args.path.strip("/"), body=body(), encode_chunked=True,
                         headers={**args.headers, "Host": f"{args.name}:{args.port}",
                                  "Content-Type": "application/octet-stream"})
            response = link.getresponse()
            response.read()
            return response.status
        finally:
            link.close()


def parse(argv):
    parser = argparse.ArgumentParser(prog="post_blob_loopback.py", add_help=False)
    parser.add_argument("url")
    parser.add_argument("repository")
    parser.add_argument("digest")
    parser.add_argument("port", type=int)
    parser.add_argument("--path", default="blobs")
    parser.add_argument("--name", default="nifi-loopback")
    parser.add_argument("--ca", default=os.environ.get("LOOPBACK_CA"))
    parser.add_argument("--cert", default=os.environ.get("LOOPBACK_CERT"))
    parser.add_argument("--key", default=os.environ.get("LOOPBACK_KEY"))
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--trusted-storage", default=os.environ.get("LOOPBACK_TRUSTED_STORAGE", ""))
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--scheme", choices=("https", "http"), default="https")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        push.fail(15, __doc__.split("\n", 1)[0] + "\nusage: post_blob_loopback.py <registry URL> <repository> <digest> <port> [options]")
    pairs = [h.partition("=") for h in args.header]
    if any(not name or not sep for name, sep, _ in pairs):
        push.fail(15, "--header wants NAME=VALUE")
    args.headers = {name: value for name, _, value in pairs}
    args.headers.update({"X-Blob-Digest": args.digest, "filename": args.digest.split(":")[-1]})
    args.url = args.url.rstrip("/")
    args.trusted = trust(args.trusted_storage)
    push.check_names(args.repository, args.digest, digest_only=True)
    return args


def tls_context(args):
    if args.scheme == "http":
        return None
    if not (args.ca and args.cert and args.key):
        push.fail(15, "https needs --ca, --cert and --key (or LOOPBACK_CA, LOOPBACK_CERT, LOOPBACK_KEY)")
    try:
        context = ssl.create_default_context(cafile=args.ca)
        context.check_hostname = False  # the CA is verified; the certificate may name the pod, not --name
        context.load_cert_chain(args.cert, args.key)
    except (OSError, ssl.SSLError) as error:
        push.fail(15, f"cannot load the TLS files: {error}")
    return context


def main(argv):
    args = parse(argv)
    context = tls_context(args)
    headers = push.authorization(args.url, push.target(args.url), args.repository, os.environ.get("REGISTRY_USER", ""),
                                 os.environ.get("REGISTRY_PASSWORD", ""), actions="pull")
    status = "none"
    for attempt in range(1, args.attempts + 1):
        started, sent = time.time(), [0]
        args.headers["X-Attempt"] = str(attempt)
        try:
            status = deliver(args, context, headers, sent)
        except Refused as error:
            push.fail(15, str(error))
        except (OSError, http.client.HTTPException) as error:
            status = f"error {type(error).__name__}"
        print(f"attempt {attempt}: {status} sent={sent[0]} secs={time.time() - started:.1f}", file=sys.stderr, flush=True)
        if status == 200:
            print(json.dumps({"delivered": args.digest, "bytes": sent[0], "attempts": attempt}))
            return
        if attempt < args.attempts:
            time.sleep(min(30, 2 ** attempt))
    push.fail(6, f"not delivered after {args.attempts} attempts, last {status}")


if __name__ == "__main__":
    main(sys.argv)
