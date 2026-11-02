#!/usr/bin/env python3
"""Mirror the Mac screen to a TV or other DLNA media player over Wi-Fi.

The Mac captures its screen with ffmpeg, encodes it to H.264 in an MPEG-TS
stream, serves that stream over HTTP, and tells the TV (via UPnP AVTransport)
to play it. Mac and TV must be on the same network.
"""

import argparse
import http.server
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
# Description URLs to try when --tv is an IP that didn't answer discovery (Sony TVs use this one).
FALLBACK_DESCRIPTION_URLS = ["http://{ip}:52323/dmr.xml"]
# Live stream: no seeking (OP=00), streaming transfer mode, DLNA 1.5 flags.
DLNA_FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
STREAM_PATH = "/screen.ts"
HERE = Path(__file__).resolve().parent
HELPER_SOURCE = HERE / "audiotap.swift"
HELPER = HERE / "audiotap"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class Renderer:
    def __init__(self, name, location, control_url, service_type):
        self.name = name
        self.location = location
        self.control_url = control_url
        self.service_type = service_type
        self.host = urllib.parse.urlparse(location).hostname

    @classmethod
    def from_location(cls, location):
        with urllib.request.urlopen(location, timeout=5) as resp:
            root = ET.fromstring(resp.read())
        name, base = None, location
        for el in root.iter():
            tag = _local(el.tag)
            if tag == "friendlyName" and name is None:
                name = (el.text or "").strip()
            elif tag == "URLBase" and el.text:
                base = el.text.strip()
        for svc in root.iter():
            if _local(svc.tag) != "service":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in svc}
            if "AVTransport" in fields.get("serviceType", ""):
                control = urllib.parse.urljoin(base, fields["controlURL"])
                return cls(name or location, location, control, fields["serviceType"])
        raise ValueError(f"{location} has no AVTransport service")

    def soap(self, action, args, timeout=10):
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args)
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{self.service_type}">{body}</u:{action}>'
            "</s:Body></s:Envelope>"
        )
        req = urllib.request.Request(
            self.control_url,
            data=envelope.encode(),
            method="POST",
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{self.service_type}#{action}"',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{action} failed ({e.code}): {detail}") from None

    def play_url(self, url, mime, title):
        protocol_info = f"http-get:*:{mime}:{DLNA_FEATURES}"
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
            "<upnp:class>object.item.videoItem</upnp:class>"
            f'<res protocolInfo="{protocol_info}">{escape(url)}</res></item></DIDL-Lite>'
        )
        # Some players start reading the stream before answering,
        # so allow them plenty of time.
        self.soap("SetAVTransportURI", [("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", didl)],
                  timeout=30)
        try:
            self.soap("Play", [("InstanceID", 0), ("Speed", 1)], timeout=30)
        except (socket.timeout, TimeoutError, urllib.error.URLError, RuntimeError) as e:
            # Some players start on their own after SetAVTransportURI and then time out
            # or reject Play. Playback usually continues anyway, so keep streaming.
            reason = re.search(r"<errorDescription>(.*?)</errorDescription>", str(e))
            print(f"The TV didn't confirm Play ({reason.group(1) if reason else e}); "
                  "continuing to stream.")

    def stop(self):
        try:
            self.soap("Stop", [("InstanceID", 0)])
        except Exception:
            pass


def discover(timeout=4.0):
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 2",
        f"ST: {AVTRANSPORT}",
        "", "",
    ]).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    for _ in range(2):
        sock.sendto(msg, SSDP_ADDR)
    locations = set()
    deadline = time.time() + timeout
    while (remaining := deadline - time.time()) > 0:
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(65507)
        except socket.timeout:
            break
        m = re.search(rb"(?im)^location:\s*(\S+)", data)
        if m:
            locations.add(m.group(1).decode())
    sock.close()

    renderers = []
    for loc in sorted(locations):
        try:
            renderers.append(Renderer.from_location(loc))
        except Exception as e:
            print(f"  skipping {loc}: {e}", file=sys.stderr)
    return renderers


def list_av_devices():
    """Return (video, audio) lists of (index, name) from ffmpeg's AVFoundation input."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    video, audio, section = [], [], None
    for line in result.stderr.splitlines():
        if "video devices" in line:
            section = video
        elif "audio devices" in line:
            section = audio
        elif section is not None and (m := re.search(r"\] \[(\d+)\] (.+)$", line)):
            section.append((int(m.group(1)), m.group(2).strip()))
    return video, audio


FIT_FILTERS = {
    # Fill the TV, stretching the Mac's 16:10 picture slightly wider. Nothing is cut off.
    "stretch": "scale={w}:{h}:flags=lanczos",
    # Fill the TV with correct proportions, cutting a little off the top and bottom.
    "crop": "scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}",
    # Correct proportions with black bars at the sides.
    "pad": "scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
}


def ffmpeg_command(screen, audio, width, height, fps, bitrate, fit, audio_pipe=None):
    """audio is "system" (read from audio_pipe), "none", or an AVFoundation device index."""
    vf = FIT_FILTERS[fit].format(w=width, h=height) + ",format=yuv420p"
    device_audio = audio if audio not in ("system", "none") else "none"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-thread_queue_size", "512",
           "-f", "avfoundation", "-capture_cursor", "1", "-pixel_format", "nv12",
           "-framerate", str(fps),
           "-i", f"{screen}:{device_audio}"]
    if audio == "system":
        # Opened after the screen, so the helper starts capturing as the video does.
        cmd += ["-thread_queue_size", "1024", "-f", "wav", "-ignore_length", "1", "-i", audio_pipe,
                "-map", "0:v", "-map", "1:a"]
    elif audio == "none":
        # The TV expects an audio track, so send silence when none is captured.
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a"]
    cmd += ["-vf", vf,
            "-c:v", "h264_videotoolbox", "-realtime", "1", "-profile:v", "high",
            "-b:v", bitrate, "-maxrate", bitrate, "-g", str(fps), "-bf", "0",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-f", "mpegts", "-muxdelay", "0", "-flush_packets", "1", "pipe:1"]
    return cmd


def build_audio_helper():
    """Compile audiotap.swift if the binary is missing or older than the source."""
    if HELPER.exists() and HELPER.stat().st_mtime >= HELPER_SOURCE.stat().st_mtime:
        return
    if not shutil.which("swiftc"):
        sys.exit("swiftc is needed to build the audio helper: xcode-select --install "
                 "(or run with --audio none)")
    print("Building the system audio helper...")
    # No embedded Info.plist: the helper then shares the terminal's Screen & System
    # Audio Recording permission instead of needing its own.
    subprocess.run(["swiftc", "-O", str(HELPER_SOURCE), "-o", str(HELPER)], check=True)


TS_PACKET = 188


class Broadcaster:
    """Runs one capture and sends the same live stream to every connected player.

    Players often open several connections (one to probe the format, one to play),
    so a new connection joins the running stream instead of restarting it. Capture
    stops once no player has been connected for IDLE_STOP seconds.
    """

    IDLE_STOP = 15
    CLIENT_BACKLOG = 256  # chunks a slow connection may fall behind before it is dropped

    def __init__(self, cmd, helper_cmd=None):
        self.cmd = cmd
        self.helper_cmd = helper_cmd
        self.lock = threading.Lock()
        self.procs = []
        self.clients = set()
        self.idle_timer = None

    def subscribe(self):
        client = queue.Queue(maxsize=self.CLIENT_BACKLOG)
        with self.lock:
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            if not self.procs:
                self._start()
            self.clients.add(client)
            return client, len(self.clients)

    def unsubscribe(self, client):
        with self.lock:
            self.clients.discard(client)
            if not self.clients and self.procs and not self.idle_timer:
                self.idle_timer = threading.Timer(self.IDLE_STOP, self._stop_if_idle)
                self.idle_timer.daemon = True
                self.idle_timer.start()

    def stop(self):
        with self.lock:
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
            self._kill(self.procs)
            self.procs = []
            self._end_clients()

    def _stop_if_idle(self):
        with self.lock:
            self.idle_timer = None
            if not self.clients:
                self._kill(self.procs)
                self.procs = []

    def _start(self):
        procs = []
        if self.helper_cmd:
            procs.append(subprocess.Popen(self.helper_cmd, stdin=subprocess.DEVNULL))
        proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        threading.Thread(target=self._show_errors, args=(proc.stderr,), daemon=True).start()
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        self.procs = [proc] + procs

    def _pump(self, proc):
        pending = b""
        while chunk := proc.stdout.read1(65536):
            # Send whole MPEG-TS packets only, so players joining mid-stream start cleanly.
            pending += chunk
            cut = len(pending) - len(pending) % TS_PACKET
            if not cut:
                continue
            data, pending = pending[:cut], pending[cut:]
            with self.lock:
                for client in list(self.clients):
                    try:
                        client.put_nowait(data)
                    except queue.Full:
                        self.clients.discard(client)  # hopelessly behind; it will time out
        with self.lock:
            if self.procs and self.procs[0] is proc:
                self._kill(self.procs)
                self.procs = []
                self._end_clients()

    def _end_clients(self):
        for client in self.clients:
            try:
                client.put_nowait(None)
            except queue.Full:
                pass
        self.clients.clear()

    @staticmethod
    def _show_errors(stream):
        # Skip harmless macOS framework warnings and the message ffmpeg prints when stopped.
        for line in stream:
            text = line.decode(errors="replace").rstrip()
            if text and not text.startswith("objc[") and "Immediate exit requested" not in text:
                print(text, file=sys.stderr)

    @staticmethod
    def _kill(procs):
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


class StreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # stream until the connection closes

    def _send_headers(self):
        if self.path.split("?")[0] != STREAM_PATH:
            self.send_error(404)
            return False
        self.send_response(200)
        self.send_header("Content-Type", self.server.mime)
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", DLNA_FEATURES)
        self.send_header("Connection", "close")
        self.end_headers()
        return True

    def do_HEAD(self):
        self._send_headers()

    def do_GET(self):
        if not self._send_headers():
            return
        client, count = self.server.broadcaster.subscribe()
        print(f"Player connected from {self.client_address[0]} ({count} active)")
        try:
            while True:
                chunk = client.get(timeout=10)
                if chunk is None:
                    break
                self.wfile.write(chunk)
        except (queue.Empty, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.broadcaster.unsubscribe(client)
            print(f"Player disconnected from {self.client_address[0]}")

    def log_message(self, fmt, *args):
        pass


def local_ip_towards(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 80))
        return s.getsockname()[0]


def pick_renderer(args):
    if args.tv and args.tv.startswith("http"):
        return Renderer.from_location(args.tv)

    print("Searching for TVs on the network...")
    renderers = discover()
    if args.tv:
        renderers = [r for r in renderers if r.host == args.tv]
        if not renderers:
            for template in FALLBACK_DESCRIPTION_URLS:
                try:
                    return Renderer.from_location(template.format(ip=args.tv))
                except Exception:
                    pass
    if not renderers:
        sys.exit("No DLNA TV found. Check the TV is on and on the same Wi-Fi, or pass --tv <ip>.")
    if len(renderers) == 1:
        return renderers[0]
    for i, r in enumerate(renderers):
        print(f"  [{i}] {r.name} ({r.host})")
    return renderers[int(input("Choose a TV: "))]


def main():
    p = argparse.ArgumentParser(description="Mirror this Mac's screen to a TV or other DLNA media player.")
    p.add_argument("--tv", help="TV IP address or its device description URL (skips the picker)")
    p.add_argument("--list", action="store_true", help="list TVs and capture devices, then exit")
    p.add_argument("--screen", type=int, help="AVFoundation screen index (default: first screen)")
    p.add_argument("--audio", default="system",
                   help="'system' (Mac sound, default), 'none' for silence, or an input device index/name")
    p.add_argument("--fit", choices=FIT_FILTERS, default="stretch",
                   help="how to fit the Mac screen to the 16:9 TV (default: stretch)")
    p.add_argument("--resolution", default="1920x1080", help="output size (default 1920x1080)")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="8M")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--mime", default="video/mpeg", help="MIME type sent to the TV (try video/mp2t if rejected)")
    args = p.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required: brew install ffmpeg")

    video_devs, audio_devs = list_av_devices()
    screens = [(i, n) for i, n in video_devs if n.startswith("Capture screen")]

    if args.list:
        print("Screens:", *(f"  [{i}] {n}" for i, n in screens), sep="\n")
        print("Audio devices:", *(f"  [{i}] {n}" for i, n in audio_devs), sep="\n")
        print("TVs:")
        for r in discover():
            print(f"  {r.name} ({r.host})  {r.location}")
        return

    if args.screen is None:
        if not screens:
            sys.exit("No screen capture device found. Grant Screen Recording permission to your terminal "
                     "in System Settings > Privacy & Security, then try again.")
        args.screen = screens[0][0]

    audio = args.audio.lower()
    if audio not in ("system", "none") and not audio.isdigit():
        match = [i for i, n in audio_devs if n.lower() == audio]
        if not match:
            sys.exit(f"Audio device '{args.audio}' not found. Run with --list to see devices.")
        audio = match[0]

    helper_cmd = audio_pipe = None
    if audio == "system":
        build_audio_helper()
        audio_pipe = os.path.join(tempfile.mkdtemp(prefix="mac_mirror_"), "audio.wav")
        os.mkfifo(audio_pipe)
        helper_cmd = [str(HELPER), audio_pipe]

    width, height = (int(x) for x in args.resolution.lower().split("x"))
    tv = pick_renderer(args)
    host_ip = local_ip_towards(tv.host)
    url = f"http://{host_ip}:{args.port}{STREAM_PATH}"

    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), StreamHandler)
    server.daemon_threads = True
    server.broadcaster = Broadcaster(
        ffmpeg_command(args.screen, audio, width, height, args.fps, args.bitrate, args.fit, audio_pipe),
        helper_cmd)
    server.mime = args.mime
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Always stop ffmpeg and the audio helper, even after an error, so nothing
    # keeps capturing the screen or muting the Mac in the background.
    try:
        print(f"Sending {url} to {tv.name} ({tv.host})")
        tv.stop()
        tv.play_url(url, args.mime, "Mac Screen")
        print("Mirroring. Press Ctrl+C to stop.")

        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        stop.wait()
        print("\nStopping...")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        tv.stop()
        server.broadcaster.stop()
        server.shutdown()
        if audio_pipe:
            os.unlink(audio_pipe)
            os.rmdir(os.path.dirname(audio_pipe))


if __name__ == "__main__":
    main()
