# linuxtochromecast

Cast a video to your Chromecast with subtitles already working, in one command:

```bash
ltc movie.mkv
```

That's it. It finds the Chromecast, picks up the subtitles (embedded or the
`.srt` sitting next to the file), works out whether anything needs converting,
and starts playing.

> The command-line output and the web panel are currently in Spanish. Everything
> else — flags, file names, this document — is in English.

---

## Why it exists

With VLC you pick the device by hand, cross your fingers about the format, and
the subtitles simply don't make it: VLC renders them locally, so casting drops
them. The usual workaround is burning them into the video, which takes forever
and can't be turned off afterwards.

Here the subtitles are sent to the Chromecast as **real text tracks**. You turn
them on, off, and switch languages instantly, without re-encoding anything.

---

## Installation

```bash
./install.sh
```

It sets up the environment, puts `ltc` on your PATH, and adds a **Send to
Chromecast** entry to your file manager's context menu. No sudo required. The
only prerequisites are `python3` (3.10+) and `ffmpeg`.

---

## Usage

```bash
ltc movie.mkv                  # cast, picking subtitles automatically
ltc show.mkv -s 2              # use subtitle track number 2
ltc movie.mp4 -s other.srt     # use a specific subtitle file
ltc movie.mkv --list-tracks    # show what's inside the file and exit
ltc --list-devices             # show the Chromecasts on your network
ltc movie.mkv -d "Living"      # pick the device by name
ltc movie.mkv -t 45:30         # start at 45 minutes 30 seconds
```

While it runs, the terminal accepts:

| Key | Action |
|---|---|
| `space` | pause / resume |
| `←` `→` | ±10 seconds |
| `↓` `↑` | ±30 seconds |
| `j` `k` | volume |
| `m` | mute |
| `s` | cycle subtitle track |
| `[` `]` | nudge subtitle sync |
| `0` | reset subtitle sync |
| `q` | quit |

### The web panel

It also opens a control panel in your browser with the same controls. The
address it prints on startup (`http://192.168.x.x:PORT`) **works from your
phone** if it's on the same network, so it doubles as a remote control.

---

## How it decides what to do with the file

It always takes the cheapest of three paths:

| Mode | What it does | Cost | Seeking |
|---|---|---|---|
| **direct** | Serves the file untouched | none | native, instant |
| **remux** | Swaps the container without touching the video (`-c copy`) | negligible | restarts the stream |
| **transcode** | Re-encodes whatever is needed | CPU | restarts the stream |

An MP4 with H.264 and stereo AAC plays directly. An MKV with those same codecs
only needs a remux, which is essentially free. Re-encoding kicks in only when
the device genuinely can't decode something: AC3 or DTS audio, HEVC video on an
older Chromecast, 10-bit, 4:4:4.

**Channel count matters too.** A regular Chromecast decodes AAC but not
multichannel AAC: if you look at the codec alone, the movie starts, plays for a
few seconds, and dies with `MEDIA_SRC_NOT_SUPPORTED`. So when a file carries
more channels than the device handles, the audio is downmixed to stereo. It's
cheap, because the video is copied untouched and only the audio is re-encoded.
`--surround` forces it through as-is.

It tells you which path it chose and why:

```
  modo   transcodificando
  video  hevc 1920x1080 → libx264
  audio  eac3 · Español · 6ch → aac
         video hevc no soportado por Chromecast → recodifico a H.264
         audio eac3 no soportado → convierto a AAC
         bajo 6ch a estéreo (usá --surround para mantenerlo)
```

You can override with `--transcode` or `--direct` if you know better.

### How the stream is delivered

When the output goes through ffmpeg there's no way to know its final size in
advance, so `Transfer-Encoding: chunked` would be the natural choice. **It
doesn't work:** the receiver rejects the MP4 with `LOAD_FAILED` without
rendering a single frame. It needs a `Content-Length`, so it gets an estimated
one, deliberately on the high side — overshooting means the connection drops
once the movie is already over, undershooting would cut off the ending.

### Seeking when ffmpeg is in the path

In remux or transcode mode the output is a pipe, and a pipe can't serve range
requests: the stream always starts at zero. So seeking works by killing ffmpeg
and restarting it at the requested point, while the program keeps track of what
that zero is worth in real movie time. Subtitles are served shifted by the same
amount, so they never drift.

When the video is copied rather than re-encoded, ffmpeg can't start at an
arbitrary point — only at the preceding keyframe. The program looks up where
that is and uses it as the zero for the subtitles too. Without that, they'd be
off by up to several seconds.

---

## Subtitles

Found automatically:

- tracks embedded in the MKV or MP4;
- files next to the video: `movie.srt`, `movie.es.srt`,
  `movie.spa.forced.srt`, and anything inside a `Subs/` folder.

SRT, ASS, SSA, SUB and VTT are converted to WebVTT, the only format the receiver
understands. ASS styling and positioning are dropped; italics and bold survive.

Which one gets enabled: the track in your language (`--sub-lang`, Spanish by
default), preferring regular over forced. If there's exactly one track and it
doesn't even declare a language — the `movie.mp4` + `movie.srt` case — it gets
enabled anyway, on the reasoning that a lone `.srt` next to a movie is there to
be watched. If it declares a language other than the one you asked for, it is
not enabled automatically; press `s` or use the panel.

### Character encoding

`.srt` files don't record their encoding anywhere, and most of the ones in
circulation for Spanish are latin-1 or cp1252 rather than UTF-8. Automatic
detectors fail exactly here: `cp1250` and `cp1252` differ in a handful of bytes,
and one of them is `0xF1` — `ñ` in cp1252, `ń` in cp1250. Statistically they're
indistinguishable.

Since the track's language is already known (from the MKV tag or from the
`.es.srt` in the filename), it's used as the tie-breaker: the winning encoding is
the one that produces the most letters from the expected alphabet. If it still
comes out wrong:

```bash
ltc movie.mkv --sub-encoding cp1252
```

### Sync

If the subtitles are off, `[` and `]` shift them by 0.1 seconds at a time, and
the web panel has ±0.1 and ±0.5 buttons. You can also start with a fixed offset:

```bash
ltc movie.mkv --delay -2.5
```

### Image-based subtitles

Blu-ray and DVD subtitles (PGS, VobSub) are images, not text, so they can't be
sent as a track. The only option is burning them into the video, which forces a
re-encode:

```bash
ltc movie.mkv --burn 1
```

`--list-tracks` marks which ones are image-based.

---

## Options

```
  -d, --device NAME       Chromecast to use (partial name, or its IP)
  -s, --sub N|FILE        subtitle track, by number or by file
      --no-subs           start with subtitles off
      --burn N            burn track N into the video
      --sub-lang CODE     preferred language when auto-selecting (default: spa)
      --sub-scale F       subtitle size, 1.0 = normal
      --sub-encoding ENC  force the encoding of external .srt files
      --delay SECONDS     initial subtitle offset
  -a, --audio N           audio track
  -t, --start TIME        start at 90, 1:30 or 1:02:03
  -p, --port N            local server port
      --transcode         force re-encoding
      --direct            force direct playback
      --surround          don't downmix multichannel audio to stereo
      --no-browser        don't open the web panel
      --list-devices      list the Chromecasts on the network
      --list-tracks       list the file's tracks
```

---

## Troubleshooting

**It can't find the Chromecast, or takes ages to start.** Discovery uses mDNS,
which doesn't always cross the network (VLANs, guest Wi-Fi, some routers). Get
the IP once with `ltc --list-devices` and then use it directly, which skips
discovery entirely:

```bash
ltc movie.mkv -d 192.168.100.13
```

**Video plays but no subtitles.** Almost always CORS: the receiver fetches the
`.vtt` over XHR and silently discards it if the `Access-Control-Allow-Origin`
header is missing. This program always sends it, so if it happens, check that
your firewall isn't blocking the local server's port:

```bash
sudo firewall-cmd --add-port=8010/tcp   # Fedora, temporary
ltc movie.mkv -p 8010
```

**It starts and cuts out after a few seconds.** That's the classic multichannel
audio symptom: the receiver accepts the file, begins playing, and only fails
when it reaches the audio, with `MEDIA_SRC_NOT_SUPPORTED` (code 104). This is
handled automatically — the audio is downmixed when the device can't take the
channel count — but if you forced it with `--surround`, drop that flag.

**Stuttering while transcoding.** Real-time video re-encoding needs CPU. Lower
the resolution, or if only the audio is the problem, check that it isn't
re-encoding the video unnecessarily — the "modo" line printed at startup tells
you exactly what's being converted and why.

**Subtitles show garbled characters.** See *Character encoding* above;
`--sub-encoding cp1252` fixes it nearly every time.

---

## Tests

```bash
./tests/make_fixtures.sh                              # generate the test videos
.venv/bin/python tests/test_integration.py tests/media
```

This runs the server against a simulated Chromecast and checks what actually
matters: that HTTP ranges are correct, that subtitles are served with the right
CORS headers and MIME type, that the ffmpeg pipeline produces a decodable MP4,
and that seeking shifts the subtitles along with the stream. No hardware needed.

The fixtures are built to force all three paths: an MP4 that plays directly, an
MKV that only needs remuxing, and an MKV with 4:4:4 and AC3 that requires
re-encoding. The external `.srt` files are deliberately written in latin-1.

With a real Chromecast on hand:

```bash
.venv/bin/python tests/test_live.py tests/media ["device name"]
```

That one actually casts (about 30 seconds) and verifies, by inspecting the
server's request log, that the receiver really did fetch the `.vtt` from its own
IP — which is the only way to know the subtitles will show up.

---

## Layout

```
ltc/
├── cli.py          arguments, keyboard controls, terminal output
├── caster.py       discovery and the Chromecast protocol
├── session.py      playback state and position translation
├── server.py       HTTP with Range, CORS, and the ffmpeg pipe
├── probe.py        file analysis via ffprobe
├── compat.py       what each device supports and how to get there
├── subtitles.py    detection, encoding, conversion, and sync
├── transcode.py    building the ffmpeg pipeline
└── webui.py        web control panel
```

Source comments are in Spanish.

## License

MIT — see [LICENSE](LICENSE).
