# Mac Mirror

Mirror your Mac's screen over Wi-Fi to any DLNA media player: a smart TV,
set-top box, streaming box or similar device.

macOS can't send to a TV's built-in "Screen mirroring" (Miracast) input. This tool
uses DLNA instead: the Mac encodes its screen to H.264 and the device plays it as a
live stream. Expect a 2–5 second delay. It works well for video and presentations,
but not for typing or games.

## Setup

```sh
brew install ffmpeg
```

Grant your terminal app **Screen Recording** permission in
System Settings → Privacy & Security, then restart the terminal.

On the TV or player: turn it on, connect it to the **same Wi-Fi/router as the Mac**,
and enable DLNA playback if it has a setting for it. It is often called *Renderer*,
*Media sharing*, *DMR* or *Allow remote control/playback*.

## Usage

```sh
python3 mac_mirror.py                 # find a player and start mirroring
python3 mac_mirror.py --tv 192.168.1.20   # skip discovery
python3 mac_mirror.py --list          # show players, screens and audio devices
```

Press Ctrl+C to stop.

| Option | Default | Notes |
|---|---|---|
| `--fit` | `stretch` | If your Mac screen is 16:10 and the TV is 16:9: `stretch` fills the TV, `crop` keeps proportions but cuts off the top and bottom edges, `pad` keeps everything with black bars at the sides |
| `--resolution` | `1920x1080` | Use `1280x720` if the picture stutters |
| `--fps` | `30` | |
| `--bitrate` | `8M` | Lower it (e.g. `4M`) on weak Wi-Fi; raise it (e.g. `12M`) for sharper text |
| `--audio` | `system` | Plays the Mac's sound on the TV and mutes the Mac while mirroring. `none` sends silence |
| `--mime` | `video/mpeg` | Try `video/mp2t` if the player says the file type is unsupported |

## System audio

`audiotap.swift` is a small helper that captures the Mac's sound using a Core Audio
process tap (macOS 14.2+). The script compiles it the first time you run it; this needs
the Xcode command line tools (`xcode-select --install`). It uses your terminal's
**Screen & System Audio Recording** permission. If the TV shows the picture but plays
no sound, check that your terminal is allowed there.

Some players open several connections to the stream and reject or never confirm
the Play command. The script handles this: all connections share one capture, and
a failed Play only prints a warning.

## Troubleshooting

- **No player found**: check that the device and Mac are on the same network (not a guest network) and that DLNA playback is enabled, or pass `--tv <ip>`. The device's network settings show its IP address.
- **Edges of the picture are cut off**: the TV is zooming in slightly (overscan). Set its picture size or aspect to *Full Pixel*, *Just Scan*, *Screen Fit* or similar.
- **Error or black screen**: run with `--resolution 1280x720 --bitrate 4M`, then try `--mime video/mp2t`.
- **The player can't reach the stream**: allow incoming connections for Python in the macOS firewall (it serves on port 8090).
