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

## Install

Requires Python 3.9 or newer. The proxy runs on the standard library alone, so
if you are in a hurry this is genuinely all you need:

```sh
git clone https://github.com/mntxsn/hls-referer-proxy.git
cd hls-referer-proxy
python3 hls_proxy.py
```

### With a virtual environment (recommended)

A venv keeps `certifi` out of your system Python and fixes the
`CERTIFICATE_VERIFY_FAILED` error that the python.org macOS builds run into:

```sh
git clone https://github.com/mntxsn/hls-referer-proxy.git
cd hls-referer-proxy

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python hls_proxy.py
```

`deactivate` leaves the venv again. To start it later without activating
anything, call the interpreter inside the venv directly — this is also the form
to use in the launch agent below:

```sh
.venv/bin/python hls_proxy.py
```

### ffmpeg

Only the MP3 and AAC endpoints need it; the HLS endpoint does not. It is a
system package rather than a Python one, so pip will not install it:

```sh
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian/Ubuntu
```

## Usage

```sh
python3 hls_proxy.py
```

Then point any player at one of:

| URL | What it is |
| --- | --- |
| `http://127.0.0.1:8765/stream.m3u8` | HLS, byte-for-byte the original (AAC 132 kbit/s). Best quality. |
| `http://127.0.0.1:8765/stream.mp3` | Continuous MP3. Looks like an ordinary Icecast station to apps that cannot do HLS. |
| `http://127.0.0.1:8765/stream.aac` | AAC in ADTS, remuxed without re-encoding. |

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
ffplay -referer "https://www.wish1075.com/" -nodisp -vn -infbuf \
  "https://radio.wish1075.com/web/stream/wish.m3u8"

# record
ffmpeg -referer "https://www.wish1075.com/" \
  -i "https://radio.wish1075.com/web/stream/wish.m3u8" \
  -vn -c:a copy recording.aac
```

`-infbuf` is not optional in practice. Without it ffplay caps its input buffer
at a size meant for local files, and on this stream the audio queue sits around
13 KB — under a second of audio — so every hiccup in fetching the next
4-second segment is audible as a dropout. With `-infbuf` the same stream buffers
around 180 KB, roughly ten seconds, and plays through cleanly.

`-vn` drops the 256x144 video track, which ffplay would otherwise decode at
30 fps just to throw the frames away. `-nodisp` is still needed alongside it,
because ffplay opens a window to draw an audio visualisation when there is no
video to show.

The recording command needs neither: it writes as fast as the origin delivers
rather than in real time, so buffer depth does not matter.

## Run it permanently

Neither launchd nor systemd runs your shell profile, so every path in the files
below has to be absolute. Use `.venv/bin/python` from your clone if you set up a
virtual environment, otherwise whatever `command -v python3` reports.

### Linux and Raspberry Pi (systemd)

A Pi makes a good permanent home for this: leave it running and every device in
the house can reach the stream. Python 3.9 is enough, so both Bullseye and
Bookworm work as shipped.

```sh
sudo apt install python3-venv ffmpeg
git clone https://github.com/mntxsn/hls-referer-proxy.git
cd hls-referer-proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Edit `hls-proxy.service` — replace every `CHANGEME` with your username, which
`whoami` will tell you — then install it:

```sh
sudo cp hls-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hls-proxy
```

`enable` is what makes it come back after a reboot; `--now` also starts it
immediately. To check on it:

```sh
systemctl status hls-proxy
journalctl -u hls-proxy -f      # live log
sudo systemctl restart hls-proxy
```

The unit passes `--host 0.0.0.0` so other devices can reach it. Point them at
`http://<pi-ip>:8765/stream.m3u8`, or `/stream.mp3` for apps that cannot do
HLS. Find the address with `hostname -I`, and consider giving the Pi a static
lease in your router so the URL stays put.

Keep this on your own network. Do not forward the port from your router: it
would expose an open relay to the station's origin, which is a good way to get
the endpoint locked down for everyone.

On very old hardware such as a Pi Zero, prefer `/stream.m3u8` or `/stream.aac`.
Both pass the audio through untouched, whereas `/stream.mp3` re-encodes and is
the only endpoint that costs real CPU.

### macOS (launchd)

Edit the paths in `com.hlsproxy.plist` to match your system, then:

```sh
cp com.hlsproxy.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hlsproxy.plist
```

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED`** — python.org's macOS builds ship without a CA
bundle until you run their bundled *Install Certificates.command*. The proxy
falls back to `certifi` or `/etc/ssl/cert.pem` on its own, so the usual fix is
`pip install -r requirements.txt` in a venv. Running that installer works too.

**`502 Origin returned 418`** — the origin rejected the Referer. Either the
station changed what it expects, or `--referer` is wrong.

**Playback stops after a few seconds** — usually the origin being briefly
unavailable. The proxy holds no state, so restarting the player is enough.

**The systemd service will not start** — `journalctl -u hls-proxy -n 50` shows
why. `status=203/EXEC` means a path in `ExecStart` is wrong; `status=200/CHDIR`
means `WorkingDirectory` is. Both are usually a `CHANGEME` left in the file, or
a clone in a different directory than the unit expects.

**Audio stutters or drops out** — this is a player-side buffering setting, not
the proxy. ffplay in particular needs `-infbuf` on any live stream (see
[Without the proxy](#without-the-proxy) for the numbers). In VLC, raise
*Preferences → Show All → Input/Codecs → Network caching* from the default
1000 ms to 3000 ms. Either way, prefer the `/stream.mp3` endpoint if your player
handles plain streams better than HLS.

## A note on use

This is an interoperability tool. It does not remove authentication, bypass a
paywall, or strip advertising; it sends the exact request the station's own web
player sends, and passes the audio through unmodified, ads included. It exists
so you can listen on a player of your choosing instead of leaving a browser tab
open.

Please keep it to personal use. Do not use it to rebroadcast the stream
publicly or to hammer the origin — that costs the station money and is the
fastest way to get the endpoint locked down for everyone.

## Sponsor

If this got your station playing in VLC and you would like to say thanks, you
can sponsor the work at [github.com/sponsors/mntxsn](https://github.com/sponsors/mntxsn).

Entirely optional — the project is MIT and stays that way, with no paid tier
and nothing held back. A star or a bug report is just as welcome, and if you
get it working with another station, a note about which one is genuinely
useful: it tells me whether the playlist rewriting holds up beyond the stream
it was written for.

## License

MIT — see [LICENSE](LICENSE).
