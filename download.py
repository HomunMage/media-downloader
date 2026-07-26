#!/usr/bin/env python3
"""media-downloader: HLS (m3u8) stream downloader.

Workflow: use Video DownloadHelper / DevTools in Firefox to sniff the real
.m3u8 URL, then:

    ./download.py "https://example.com/video/master.m3u8" -o out.mp4

Direct-file URLs (mp4 etc.) are better served by a normal download manager;
this tool focuses on segmented streams those managers can't handle.

How it works (same idea as Video DownloadHelper + its CoApp):
  1. fetch master playlist, pick a quality variant
  2. fetch media playlist, collect segment URLs
  3. download all segments in parallel (much faster than ffmpeg alone)
  4. rewrite a local playlist, let ffmpeg decrypt/merge with `-c copy`
Anything exotic (separate audio renditions, SAMPLE-AES, byte ranges) falls
back to running ffmpeg directly on the URL — slower but correct.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


def make_headers(referer=None, cookie=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if cookie:
        h["Cookie"] = cookie
    return h


def fetch(url, headers, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 4xx won't fix itself; only 408/429 are worth another try.
            if e.code not in (408, 429):
                raise RuntimeError(f"HTTP {e.code} {e.reason}: {url}") from None
            last = e
        except Exception as e:  # noqa: BLE001 - retry transient network errors
            last = e
    raise RuntimeError(f"failed to fetch {url}: {last}")


def parse_master(text):
    """Return list of variants: (height, bandwidth, uri, audio_group)."""
    variants = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = line.split(":", 1)[1]
        m = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
        height = int(m.group(1)) if m else 0
        m = re.search(r"BANDWIDTH=(\d+)", attrs)
        bw = int(m.group(1)) if m else 0
        m = re.search(r'AUDIO="([^"]+)"', attrs)
        audio = m.group(1) if m else None
        for nxt in lines[i + 1:]:
            if nxt and not nxt.startswith("#"):
                variants.append((height, bw, nxt.strip(), audio))
                break
    return variants


def audio_has_separate_uri(text, group_id):
    for line in text.splitlines():
        if (
            line.startswith("#EXT-X-MEDIA:")
            and "TYPE=AUDIO" in line
            and f'GROUP-ID="{group_id}"' in line
            and "URI=" in line
        ):
            return True
    return False


def ffmpeg_direct(url, output, headers):
    """Fallback: let ffmpeg handle the whole stream (sequential, but correct)."""
    require_ffmpeg()
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "Accept")
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-headers", hdr,
        "-i", url,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        output,
    ]
    print("+ " + " ".join(cmd))
    if subprocess.call(cmd) != 0:
        sys.exit("error: ffmpeg failed")
    print(f"saved {output}")


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        sys.exit("error: ffmpeg is required (sudo apt install ffmpeg)")


def download_hls(url, output, headers, quality, jobs, limit):
    require_ffmpeg()
    text = fetch(url, headers).decode("utf-8", "replace")

    # Master playlist: pick a variant.
    if "#EXT-X-STREAM-INF" in text:
        variants = sorted(parse_master(text), key=lambda v: (v[0], v[1]))
        if not variants:
            sys.exit("error: master playlist has no variants")
        names = ", ".join(f"{h}p" if h else f"{bw//1000}kbps"
                          for h, bw, _, _ in variants)
        print(f"variants: {names}")
        if quality:
            chosen = min(variants, key=lambda v: abs(v[0] - quality))
        else:
            chosen = variants[-1]
        height, bw, uri, audio = chosen
        print(f"picked: {height}p" if height else f"picked: {bw//1000}kbps")
        if audio and audio_has_separate_uri(text, audio):
            print("separate audio rendition detected -> ffmpeg direct mode")
            return ffmpeg_direct(url, output, headers)
        url = urllib.parse.urljoin(url, uri)
        text = fetch(url, headers).decode("utf-8", "replace")

    if "SAMPLE-AES" in text or "#EXT-X-BYTERANGE" in text:
        print("unsupported playlist feature -> ffmpeg direct mode")
        return ffmpeg_direct(url, output, headers)

    # Media playlist: collect remote resources (segments, keys, init maps)
    # and build a local playlist pointing at downloaded files.
    parts_dir = output + ".parts"
    os.makedirs(parts_dir, exist_ok=True)
    remote = []  # (local_name, absolute_url)
    local_lines = []
    seg_idx = 0
    aux_idx = 0

    def add_remote(uri, kind, ext):
        nonlocal aux_idx
        name = f"{kind}{aux_idx:05d}{ext}"
        aux_idx += 1
        remote.append((name, urllib.parse.urljoin(url, uri)))
        return name

    for line in text.splitlines():
        if line.startswith("#EXT-X-KEY") and "URI=" in line:
            uri = re.search(r'URI="([^"]+)"', line).group(1)
            name = add_remote(uri, "key", ".bin")
            local_lines.append(line.replace(uri, name))
        elif line.startswith("#EXT-X-MAP") and "URI=" in line:
            uri = re.search(r'URI="([^"]+)"', line).group(1)
            name = add_remote(uri, "init", ".mp4")
            local_lines.append(line.replace(uri, name))
        elif line and not line.startswith("#"):
            if limit and seg_idx >= limit:
                if local_lines and local_lines[-1].startswith("#EXTINF"):
                    local_lines.pop()
                continue
            ext = ".m4s" if ".m4s" in line or ".mp4" in line else ".ts"
            name = f"seg{seg_idx:05d}{ext}"
            seg_idx += 1
            remote.append((name, urllib.parse.urljoin(url, line.strip())))
            local_lines.append(name)
        else:
            local_lines.append(line)
    if limit:
        local_lines.append("#EXT-X-ENDLIST")

    total = len(remote)
    print(f"{seg_idx} segments, downloading with {jobs} workers...")
    done = 0

    def grab(item):
        name, seg_url = item
        path = os.path.join(parts_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return  # crude resume: skip already-downloaded parts
        data = fetch(seg_url, headers)
        with open(path, "wb") as f:
            f.write(data)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(grab, item) for item in remote]
        for fut in as_completed(futures):
            fut.result()  # re-raise segment errors
            done += 1
            print(f"\r{done}/{total}", end="")
    print()

    playlist = os.path.join(parts_dir, "local.m3u8")
    with open(playlist, "w") as f:
        f.write("\n".join(local_lines) + "\n")

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-protocol_whitelist", "file,crypto,data",
        "-allowed_extensions", "ALL",
        "-i", playlist,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        output,
    ]
    print("+ " + " ".join(cmd))
    if subprocess.call(cmd) != 0:
        sys.exit(f"error: ffmpeg merge failed (segments kept in {parts_dir})")
    shutil.rmtree(parts_dir)
    print(f"saved {output}")


def main():
    ap = argparse.ArgumentParser(description="HLS (m3u8) stream downloader")
    ap.add_argument("url", help=".m3u8 URL sniffed from the browser")
    ap.add_argument("-o", "--output", default="video.mp4", help="output file")
    ap.add_argument("-r", "--referer", help="Referer header (page the video was on)")
    ap.add_argument("-c", "--cookie", help='Cookie header, e.g. "k=v; k2=v2"')
    ap.add_argument("-q", "--quality", type=int,
                    help="preferred height, e.g. 720 (default: best)")
    ap.add_argument("-j", "--jobs", type=int, default=16,
                    help="parallel segment downloads (default 16)")
    ap.add_argument("--limit", type=int,
                    help="only download first N segments (preview/testing)")
    args = ap.parse_args()

    headers = make_headers(args.referer, args.cookie)
    try:
        download_hls(args.url, args.output, headers, args.quality,
                     args.jobs, args.limit)
    except RuntimeError as e:
        msg = str(e)
        sys.exit(
            f"error: {msg}\n"
            "hint: 403/401 usually means the URL needs the same session your\n"
            "      browser had. In DevTools > Network, right-click the request\n"
            "      > Copy as cURL, then pass its Referer and Cookie via -r / -c.\n"
            "      Signed URLs also expire (look for expires=/token= params)."
            if "HTTP 40" in msg else f"error: {msg}"
        )


if __name__ == "__main__":
    main()
