# hls-referer-proxy

Play a Referer-locked HLS radio stream in VLC, or in any app that expects a
plain stream URL.

Written for [WISH 107.5](https://www.wish1075.com/), a station you can only
listen to through the play button on its own website. It is not in any radio
app, and pasting the stream URL into VLC does not work. This proxy fixes that
in about 300 lines of standard-library Python.

## The problem

The station's player is not hiding anything. Open the page source and the
stream URL is right there in plain text:

```js
const audio = document.getElementById('audio');
const audioSrc = 'https://radio.wish1075.com/web/stream/wish.m3u8';
```

Paste that into VLC, though, and nothing plays. There are two entirely separate
reasons, and either one alone is enough to break it.

### 1. The origin requires a Referer

```console
$ curl -sI https://radio.wish1075.com/web/stream/wish.m3u8
HTTP/1.1 418
```

`418 I'm a teapot` — the joke status code, used here as a "no". Adding the
Referer that a browser on the station's website would send makes it work:

```console
$ curl -s -e "https://www.wish1075.com/" https://radio.wish1075.com/web/stream/wish.m3u8
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:18052
#EXT-X-TARGETDURATION:4
#EXTINF:4.004,
wish-18052.ts
```

Testing the headers one at a time shows that the Referer is the only thing
checked. User-Agent and Origin make no difference — a browser UA still gets a
418, and a VLC UA with a valid Referer gets a 200:

| Headers sent | Response |
| --- | --- |
| none | 418 |
| browser User-Agent only | 418 |
| `Origin` only | 418 |
| **`Referer` only** | **200** |
| `VLC/3.0.20 LibVLC/3.0.20` UA + Referer | 200 |

The check also applies to every individual media segment, not just the
playlist, so a player has to send the Referer on each of the ~4-second chunks
it fetches. Ordinary radio apps send no Referer at all, which is exactly why
this station shows up in none of them.

### 2. The origin is TLS 1.3 only

Getting the Referer right still is not enough for VLC, which fails earlier:

```
securetransport tls client error: handshake returned error -9836
main tls client error: TLS session handshake error
access stream error: HTTP connection failure
```

The origin rejects TLS 1.2 outright:

```console
$ echo | openssl s_client -connect radio.wish1075.com:443 -tls1_2
ssl3_read_bytes:tlsv1 alert protocol version:SSL alert number 70
```

Only TLS 1.3 is offered. VLC 3.x on macOS uses SecureTransport, which does not
do TLS 1.3, so it cannot open the URL at all. VLC's own `--http-referrer`
option is therefore a dead end here — the connection dies before the header
matters. Falling back to port 80 does not help either; it is a 301 to HTTPS
with HSTS preload.

## How this proxy solves it

It sits between the player and the origin:

- upstream, it speaks HTTPS with Python's OpenSSL (TLS 1.3 is fine) and attaches
  the required Referer to the playlist and to every segment;
- downstream, it serves plain HTTP on localhost, so the player needs neither
  TLS 1.3 nor any special headers.

It also rewrites every URL inside the playlist to point back at itself, so
segments, nested playlists and `#EXT-X-KEY` decryption keys all come through
the proxy rather than being fetched directly from the origin.

## Usage

Requires Python 3.9+. No third-party packages.

```sh
python3 hls_proxy.py
```

Then point any player at one of:

| URL | What it is |
| --- | --- |
| `http://127.0.0.1:8765/stream.m3u8` | HLS, byte-for-byte the original (AAC 132 kbit/s). Best quality. |
| `http://127.0.0.1:8765/stream.mp3` | Continuous MP3. Looks like an ordinary Icecast station to apps that cannot do HLS. |
| `http://127.0.0.1:8765/stream.aac` | AAC in ADTS, remuxed without re-encoding. |

The MP3 and AAC endpoints require `ffmpeg` on your PATH. The HLS endpoint does
not.

### In VLC

*File → Open Network* and paste `http://127.0.0.1:8765/stream.m3u8`, or just
double-click the included `wish1075.m3u` to add the station permanently.

The stream carries an H.264 track (a station logo) alongside the audio. Use
`--no-video` if you would rather not have a video window pop up.

### Other stations

The defaults are WISH 107.5, but nothing is hardcoded to it:

```sh
python3 hls_proxy.py \
  --stream-url https://example.com/live/playlist.m3u8 \
  --referer    https://example.com/ \
  --name       "Example FM"
```

`--port` changes the port. `--host 0.0.0.0` exposes the stream to your local
network, which is handy for a phone or a networked speaker — do not expose it
to the internet.

## Without the proxy

If you only want to listen or record from a terminal, ffmpeg brings its own TLS
and can set the Referer itself, so it needs no proxy:

```sh
# listen
ffplay -referer "https://www.wish1075.com/" -nodisp \
  "https://radio.wish1075.com/web/stream/wish.m3u8"

# record
ffmpeg -referer "https://www.wish1075.com/" \
  -i "https://radio.wish1075.com/web/stream/wish.m3u8" \
  -vn -c:a copy recording.aac
```

## Run it at login (macOS)

Edit the paths in `com.hlsproxy.plist` to match your system, then:

```sh
cp com.hlsproxy.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hlsproxy.plist
```

`command -v python3` tells you the interpreter path to use.

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED`** — python.org's macOS builds ship without a CA
bundle until you run their bundled *Install Certificates.command*. The proxy
works around this by falling back to `certifi` or `/etc/ssl/cert.pem`, but if
both are missing you will need to run that installer.

**`502 Origin returned 418`** — the origin rejected the Referer. Either the
station changed what it expects, or `--referer` is wrong.

**Playback stops after a few seconds** — usually the origin being briefly
unavailable. The proxy holds no state, so restarting the player is enough.

## A note on use

This is an interoperability tool. It does not remove authentication, bypass a
paywall, or strip advertising; it sends the exact request the station's own web
player sends, and passes the audio through unmodified, ads included. It exists
so you can listen on a player of your choosing instead of leaving a browser tab
open.

Please keep it to personal use. Do not use it to rebroadcast the stream
publicly or to hammer the origin — that costs the station money and is the
fastest way to get the endpoint locked down for everyone.

## License

MIT
