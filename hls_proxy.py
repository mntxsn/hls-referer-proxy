#!/usr/bin/env python3
"""A local proxy for HLS streams that are locked to a website's own player.

Written for the WISH 107.5 livestream, which cannot be opened in VLC or any
radio app for two independent reasons:

  1. The origin returns "418 I'm a teapot" unless the request carries a
     Referer pointing at the station's website. The check applies to the
     playlist *and* to every single media segment.
  2. The origin speaks TLS 1.3 only. VLC 3.x on macOS uses SecureTransport,
     which has no TLS 1.3 support, so it fails during the handshake
     (error -9836) before the Referer ever matters.

This proxy fetches the playlist and its segments over HTTPS with the required
Referer, then re-serves them as plain HTTP on localhost. That defeats both
problems at once: the player never speaks TLS, and never needs a Referer.

    http://127.0.0.1:8765/stream.m3u8   HLS passthrough, bit-for-bit original
    http://127.0.0.1:8765/stream.mp3    continuous MP3, looks like Icecast
    http://127.0.0.1:8765/stream.aac    AAC, remuxed without re-encoding

The MP3 and AAC endpoints shell out to ffmpeg; the HLS endpoint has no
dependencies beyond the standard library.

Usage:
    python3 hls_proxy.py
    python3 hls_proxy.py --stream-url https://example.com/live.m3u8 \
                         --referer https://example.com/
"""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import os
import re
import select
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_STREAM_URL = "https://radio.wish1075.com/web/stream/wish.m3u8"
DEFAULT_REFERER = "https://www.wish1075.com/"

# Origins that gate on Referer usually gate on User-Agent too, or start to
# later. Presenting a browser costs nothing and removes one failure mode.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# URI="..." inside tags such as #EXT-X-KEY and #EXT-X-MAP has to be rewritten
# as well, otherwise the player would fetch decryption keys straight from the
# origin and hit the Referer check again.
TAG_URI = re.compile(r'(URI=")([^"]+)(")')

# Outages at the origin are routine: certificates expire, nginx gets restarted,
# home connections drop. None of that should end a listener's session, so the
# transcode endpoints ride it out instead of closing the response.
STALL_TIMEOUT = 20.0        # no audio out of ffmpeg for this long -> restart it
MAX_OUTAGE = 600.0          # no audio at all for this long -> give up, close
RECONNECT_DELAY_MAX = 15.0  # ceiling for the backoff between restarts


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Build a context that can verify the origin's certificate chain.

    The python.org macOS builds ship without a CA bundle unless the bundled
    "Install Certificates.command" has been run, and then every HTTPS request
    dies with CERTIFICATE_VERIFY_FAILED. certifi and the system bundle are the
    two usual ways out, so try both before giving up on the default.
    """
    for cafile in (_certifi_path(), "/etc/ssl/cert.pem"):
        if cafile and os.path.exists(cafile):
            try:
                return ssl.create_default_context(cafile=cafile)
            except Exception:
                pass
    return ssl.create_default_context()


def _certifi_path() -> str | None:
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


class Upstream:
    """Fetches from the origin with whatever headers it insists on."""

    def __init__(self, stream_url: str, referer: str):
        self.stream_url = stream_url
        self.referer = referer
        self.host = urllib.parse.urlsplit(stream_url).netloc

    def get(self, url: str, timeout: float = 15.0, retries: int = 2) -> bytes:
        """Fetch from the origin, retrying briefly on transient failures.

        A dropped connection or a 5xx is usually gone again a second later, and
        riding those out here keeps a momentary blip from surfacing to the
        player as a hard error. Deterministic answers are not retried: a 418
        means the Referer was rejected and a 404 means a segment has rolled out
        of the live window, and hammering either just adds delay.
        """
        request = urllib.request.Request(
            url,
            headers={
                "Referer": self.referer,
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
            },
        )
        delay = 0.5
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=timeout, context=ssl_context()
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == retries:
                    raise
            except urllib.error.URLError as exc:
                # An expired or otherwise invalid certificate is not going to
                # fix itself within the retry window either.
                if isinstance(exc.reason, ssl.SSLCertVerificationError) or attempt == retries:
                    raise
            except (TimeoutError, OSError):
                if attempt == retries:
                    raise
            time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # the loop either returns or raises

    def resolve(self, reference: str) -> str:
        """Turn a playlist entry into an absolute URL on the origin.

        Entries may be relative or absolute. Anything pointing off the origin
        host is rejected: the /seg/ endpoint would otherwise let a crafted
        playlist use this proxy to reach arbitrary hosts.
        """
        url = urllib.parse.urljoin(self.stream_url, reference)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.netloc != self.host:
            raise ValueError(f"refusing off-origin reference: {url}")
        return url


SAFE_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
B64URL = re.compile(r"[A-Za-z0-9_-]+")


def encode_ref(url: str) -> str:
    """Encode an origin URL into one opaque path component.

    The original file extension is appended because ffmpeg's HLS demuxer
    refuses to open segments whose extension it does not recognise
    ("not in allowed_segment_extensions"). The base64url alphabet contains no
    dot, so the suffix stays unambiguous when decoding.
    """
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    ext = SAFE_EXT.search(urllib.parse.urlsplit(url).path)
    return token + ext.group(0) if ext else token


def decode_ref(token: str) -> str:
    # The alphabet has to be checked before decoding: b64decode silently drops
    # characters outside it, so a garbage token would decode to an empty string
    # and then resolve back to the playlist URL instead of being rejected.
    token = token.split(".", 1)[0]
    if not B64URL.fullmatch(token):
        raise ValueError("malformed segment reference")
    padded = token + "=" * (-len(token) % 4)
    url = base64.urlsafe_b64decode(padded.encode()).decode()
    if not url:
        raise ValueError("empty segment reference")
    return url


def rewrite_playlist(playlist: bytes, upstream: Upstream) -> bytes:
    """Point every URL in the playlist back at this proxy.

    Players resolve relative entries against the URL they fetched the playlist
    from, which here is localhost, so most streams would work untouched. Some
    players instead follow redirects or absolute entries back to the origin,
    and those requests would arrive without a Referer and get a 418. Rewriting
    every reference explicitly removes the guesswork.
    """
    lines = []
    for raw in playlist.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        try:
            if line.startswith("#"):
                line = TAG_URI.sub(
                    lambda m: m.group(1) + "/seg/" + encode_ref(upstream.resolve(m.group(2))) + m.group(3),
                    line,
                )
            elif line:
                line = "/seg/" + encode_ref(upstream.resolve(line))
        except ValueError:
            # Keep the original line; the player will fail on that one entry
            # rather than the whole playlist.
            line = raw.strip()
        lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hls-proxy"

    @property
    def upstream(self) -> Upstream:
        return self.server.upstream

    def log_message(self, fmt, *args):
        # One line per segment would drown the log; a live stream pulls one
        # every few seconds.
        if not self.path.startswith("/seg/") and not self.path.endswith(".m3u8"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def handle(self):
        # Players drop keep-alive connections abruptly when the user switches
        # stations. That surfaces while reading the next request line, outside
        # do_GET, and would otherwise print a traceback on every disconnect.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self.serve_info()
            elif path in ("/stream.m3u8", "/wish.m3u8"):
                self.serve_playlist()
            elif path.startswith("/seg/"):
                self.serve_segment(path[len("/seg/"):])
            elif path in ("/stream.mp3", "/wish.mp3"):
                self.serve_transcode("mp3")
            elif path in ("/stream.aac", "/wish.aac"):
                self.serve_transcode("aac")
            else:
                self.send_error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            # Without this an unexpected error kills the worker thread and the
            # client just sees the connection drop, which is a miserable thing
            # to debug from the player's side.
            sys.stderr.write(f"error handling {self.path}: {exc!r}\n")
            try:
                self.send_error(500, "Internal proxy error")
            except Exception:
                pass

    def send_bytes(self, payload: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def serve_info(self):
        host = self.headers.get("Host", "127.0.0.1")
        body = (
            "hls-referer-proxy is running.\n\n"
            f"  HLS : http://{host}/stream.m3u8\n"
            f"  MP3 : http://{host}/stream.mp3\n"
            f"  AAC : http://{host}/stream.aac\n\n"
            f"upstream: {self.upstream.stream_url}\n"
        ).encode("utf-8")
        self.send_bytes(body, "text/plain; charset=utf-8")

    def serve_playlist(self):
        try:
            playlist = self.upstream.get(self.upstream.stream_url)
        except urllib.error.HTTPError as exc:
            hint = " (Referer rejected?)" if exc.code == 418 else ""
            self.send_error(502, f"Origin returned {exc.code}{hint}")
            return
        except Exception as exc:
            self.send_error(502, f"Origin unreachable: {exc}")
            return
        self.send_bytes(rewrite_playlist(playlist, self.upstream),
                        "application/vnd.apple.mpegurl")

    def serve_segment(self, token: str):
        try:
            url = self.upstream.resolve(decode_ref(token))
        except (ValueError, UnicodeDecodeError, binascii.Error):
            self.send_error(400, "Bad segment reference")
            return

        try:
            body = self.upstream.get(url)
        except urllib.error.HTTPError as exc:
            # Segments fall out of a live window continuously, so a 404 here is
            # routine rather than a failure.
            self.send_error(404 if exc.code == 404 else 502, "Segment unavailable")
            return
        except Exception as exc:
            self.send_error(502, f"Segment fetch failed: {exc}")
            return

        # A master playlist points at more playlists; those need rewriting too.
        if urllib.parse.urlsplit(url).path.endswith(".m3u8"):
            self.send_bytes(rewrite_playlist(body, self.upstream),
                            "application/vnd.apple.mpegurl")
        else:
            self.send_bytes(body, "video/mp2t")

    def serve_transcode(self, fmt: str):
        """Serve one endless response body, for players that can't do HLS."""
        if not shutil.which("ffmpeg"):
            self.send_error(501, "ffmpeg is not installed")
            return

        # Point ffmpeg at our own playlist endpoint so it inherits the Referer
        # handling and never has to speak TLS either.
        source = f"http://127.0.0.1:{self.server.server_address[1]}/stream.m3u8"
        if fmt == "mp3":
            codec = ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"]
            content_type = "audio/mpeg"
        else:
            codec = ["-c:a", "copy", "-f", "adts"]
            content_type = "audio/aac"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            # Plain -reconnect does not cover HTTP error responses, so while the
            # origin is down ffmpeg would see one 502 from our own playlist
            # endpoint and exit. That ended the listener's session for good.
            "-reconnect_on_http_error", "4xx,5xx",
            "-i", source,
            "-vn", *codec, "-",
        ]

        # No Content-Length is possible for an open-ended stream, and chunked
        # encoding confuses some radio apps, so close the connection at the end.
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("icy-name", self.server.station_name)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        # Both formats are a bare sequence of frames, so a listener that joins
        # a restarted ffmpeg mid-stream just carries on. That is what lets the
        # response survive an outage rather than ending at the first error.
        outage_since = None
        backoff = 1.0
        while True:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            try:
                delivered, client_gone = self.pump(proc)
            finally:
                proc.kill()
                proc.wait()

            if client_gone:
                return
            if delivered:
                outage_since = None
                backoff = 1.0
            elif outage_since is None:
                outage_since = time.monotonic()
                sys.stderr.write("upstream unavailable, holding the connection open\n")
            elif time.monotonic() - outage_since > MAX_OUTAGE:
                sys.stderr.write("upstream still down, closing the connection\n")
                return

            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_DELAY_MAX)

    def pump(self, proc) -> tuple[bool, bool]:
        """Forward one ffmpeg run to the client.

        Returns (delivered anything, client hung up). Watching the client
        socket alongside ffmpeg's output matters during an outage: with no
        audio to write there is nothing that would raise BrokenPipeError, so
        without this a listener who gave up would leave the retry loop running.
        """
        delivered = False
        last_data = time.monotonic()
        while True:
            ready, _, _ = select.select([proc.stdout, self.connection], [], [], 1.0)

            if self.connection in ready:
                try:
                    if not self.connection.recv(1024, socket.MSG_PEEK):
                        return delivered, True
                except (BrokenPipeError, ConnectionResetError):
                    return delivered, True
                except OSError:
                    pass

            if proc.stdout in ready:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return delivered, False        # ffmpeg exited
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return delivered, True
                delivered = True
                last_data = time.monotonic()
            elif time.monotonic() - last_data > STALL_TIMEOUT:
                return delivered, False            # wedged, let the caller restart it

            if proc.poll() is not None and proc.stdout not in ready:
                return delivered, False


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, upstream: Upstream, station_name: str):
        super().__init__(address, Handler)
        self.upstream = upstream
        self.station_name = station_name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-serve a Referer-locked HLS stream on localhost.",
    )
    parser.add_argument("--stream-url", default=DEFAULT_STREAM_URL,
                        help="upstream .m3u8 URL (default: WISH 107.5)")
    parser.add_argument("--referer", default=DEFAULT_REFERER,
                        help="Referer header the origin expects")
    parser.add_argument("--name", default="WISH 107.5",
                        help="station name reported to players")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                        help="use 0.0.0.0 to share the stream on your LAN")
    args = parser.parse_args()

    upstream = Upstream(args.stream_url, args.referer)
    try:
        upstream.get(upstream.stream_url, timeout=10)
    except Exception as exc:
        # Not fatal: the origin may just be briefly down, and the proxy will
        # recover on its own once it is back.
        print(f"Warning: origin not reachable right now ({exc})", file=sys.stderr)

    server = ProxyServer((args.host, args.port), upstream, args.name)
    shown = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"hls-referer-proxy listening on http://{shown}:{args.port}")
    print(f"  HLS : http://{shown}:{args.port}/stream.m3u8")
    print(f"  MP3 : http://{shown}:{args.port}/stream.mp3")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
