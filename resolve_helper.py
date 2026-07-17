#!/usr/bin/env python3
"""
resolve_helper.py — Post-production pipeline
─────────────────────────────────────────────────────────────────────
1. Download the latest clip from the Lumix S5 over WiFi
2. Decode LTC timecode from the audio track of each clip (ffmpeg + ltcdump)
3. Generate an FCPXML multicam timeline synced by LTC timecode
4. Open it in DaVinci Resolve (free or Studio)

Requires (install once):
    brew install ffmpeg ltc-tools
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# `requests` is imported lazily inside the functions that need it (camera/
# WiFi calls), not at module level — this file is also imported by the
# script installed into DaVinci Resolve's own Workspace > Scripts menu,
# which runs under Resolve's separate python.org Python install that
# doesn't have third-party packages like `requests` available, and doesn't
# need them for the queue-import logic it actually uses.

FPS = 30  # default; passed through from server.py


# ══════════════════════════════════════════════════════════════════════════════
# LTC decode
# ══════════════════════════════════════════════════════════════════════════════

def decode_ltc(video_path: str, fps: int = FPS, channel: int = 1) -> str | None:
    """
    Extract audio from *video_path*, decode LTC, return the first timecode
    as "HH:MM:SS:FF".  Returns None if decode fails.

    Requires ffmpeg and ltcdump (brew install ffmpeg ltc-tools).
    Only the first 10 seconds of audio are read for speed.
    *channel* is the 1-based audio channel that carries LTC (default: 1).
    """
    tmp_wav = video_path + ".__ltc_tmp.wav"
    try:
        # ── 1. Extract mono audio from the LTC channel ────────────────────
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-map", "0:a:0",               # first audio stream
                "-af", f"pan=mono|c0=c{channel - 1}",  # pick the LTC channel
                "-ar", "48000",
                "-acodec", "pcm_s16le",
                "-t", "10",                    # first 10 s is plenty
                tmp_wav,
            ],
            capture_output=True,
            check=True,
        )

        # ── 2. Decode LTC ─────────────────────────────────────────────────
        result = subprocess.run(
            ["ltcdump", "-f", str(fps), tmp_wav],
            capture_output=True,
            text=True,
        )

        # ── 3. Parse first timecode line ──────────────────────────────────
        for line in result.stdout.splitlines():
            m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})[:.](\d{2})", line)
            if m:
                h, mn, s, f = (int(x) for x in m.groups())
                return f"{h:02d}:{mn:02d}:{s:02d}:{f:02d}"

        print(f"[ltc] No timecode found in ltcdump output:\n{result.stdout[:300]}")
        return None

    except FileNotFoundError as exc:
        print(f"[ltc] ✗ Missing tool — {exc}")
        print("[ltc]   Install with:  brew install ffmpeg ltc-tools")
        return None
    except subprocess.CalledProcessError as exc:
        print(f"[ltc] ✗ ffmpeg error: {exc.stderr.decode()[-300:]}")
        return None
    except Exception as exc:
        print(f"[ltc] ✗ {exc}")
        return None
    finally:
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)


# ══════════════════════════════════════════════════════════════════════════════
# Lumix S5 file download
# ══════════════════════════════════════════════════════════════════════════════

# Panasonic cameras expose clip metadata over UPnP/DLNA, not the cam.cgi REST
# API — the root ContentDirectory container ("0") lists the most recent items
# (photos + videos) with direct download URLs, confirmed via SSDP + the
# camera's own DeviceDescription.xml for this S5.
_CDS_BROWSE_BODY = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
<ObjectID>0</ObjectID>
<BrowseFlag>BrowseDirectChildren</BrowseFlag>
<Filter>*</Filter>
<StartingIndex>0</StartingIndex>
<RequestedCount>0</RequestedCount>
<SortCriteria></SortCriteria>
</u:Browse>
</s:Body>
</s:Envelope>"""

_DIDL_NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
    "dc":   "http://purl.org/dc/elements/1.1/",
}


# Observed on this camera's firmware (S5, VD4.30): freshly-recorded video
# clips can show up in the UPnP listing as class="object.item.imageItem"
# with no <res> populated at all (only .MP4 recordings that the DLNA server
# has fully indexed get a correct videoItem class + <res>). For any item
# missing <res>, probe the known download URL patterns directly and check
# the file's magic bytes — this is the only reliable way to tell a
# mis-tagged video from a genuine photo on this firmware.
def _lumix_probe_video(camera_ip: str, item_id: str) -> dict | None:
    import requests
    for ext in ("MOV", "MP4"):
        url = f"http://{camera_ip}:50001/DO{item_id}.{ext}"
        try:
            resp = requests.get(url, headers={"Range": "bytes=0-15"}, timeout=4)
            if resp.status_code not in (200, 206):
                continue
            head = resp.content
            if len(head) < 8 or head[4:8] != b"ftyp":
                continue  # not a QuickTime/MP4 container -> not a video
            size = int(resp.headers.get("X-FILE_SIZE") or 0)
            return {
                "url":      url,
                "filename": f"DO{item_id}.{ext}",
                "size":     size,
                "date":     resp.headers.get("X-REC_DATE_TIME", ""),
            }
        except Exception:
            continue
    return None


def _lumix_list_clips(camera_ip: str) -> list[dict]:
    """
    Return a list of video clips visible in the camera's UPnP root container,
    newest first. Each entry: {"url": "http://.../DO....MP4", "filename": ..., "size": ...}
    """
    import xml.etree.ElementTree as ET
    import requests
    clips = []
    try:
        resp = requests.post(
            f"http://{camera_ip}:60606/Server0/CDS_control",
            data=_CDS_BROWSE_BODY.encode("utf-8"),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
            },
            timeout=8,
        )
        resp.raise_for_status()
        envelope = ET.fromstring(resp.text)
        result_el = envelope.find(
            ".//{urn:schemas-upnp-org:service:ContentDirectory:1}BrowseResponse/Result"
        )
        if result_el is None or not result_el.text:
            return []

        didl = ET.fromstring(result_el.text)
        for item in didl.findall("didl:item", _DIDL_NS):
            cls    = item.findtext("upnp:class", default="", namespaces=_DIDL_NS)
            res_el = item.find("didl:res", _DIDL_NS)

            if "videoItem" in cls and res_el is not None:
                for res in item.findall("didl:res", _DIDL_NS):
                    if "PANASONIC.COM_PN=CAM_ORG" not in res.get("protocolInfo", ""):
                        continue  # skip thumbnail/preview resources, want the original file
                    url = (res.text or "").strip()
                    if not url:
                        continue
                    clips.append(
                        {
                            "url":      url,
                            "filename": os.path.basename(url),
                            "size":     int(res.get("size") or 0),
                            "date":     item.findtext("dc:date", default="", namespaces=_DIDL_NS),
                        }
                    )
            elif res_el is None:
                item_id = item.get("id")
                if item_id:
                    probed = _lumix_probe_video(camera_ip, item_id)
                    if probed:
                        clips.append(probed)
    except Exception as exc:
        print(f"[lumix] ✗ Content listing failed: {exc}")

    clips.sort(key=lambda c: c["date"], reverse=True)
    return clips


def download_lumix_clip(
    camera_ip: str,
    dest_dir: str,
    progress_cb=None,
) -> str | None:
    """
    Download the most recently recorded clip from the Lumix S5 over WiFi.
    Saves to *dest_dir* and returns the local file path, or None on failure.
    *progress_cb(bytes_downloaded, total_bytes)* is called periodically if given.
    """
    import requests
    os.makedirs(dest_dir, exist_ok=True)
    clips = _lumix_list_clips(camera_ip)
    if not clips:
        print("[lumix] ✗ No clips found on camera")
        return None

    newest = clips[0]
    url  = newest["url"]
    dest = os.path.join(dest_dir, newest["filename"])

    print(f"[lumix] Downloading {newest['filename']} ({newest['size'] // 1_000_000} MB) …")
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done  = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        print(f"[lumix] ✓ Saved to {dest}")
        return dest
    except Exception as exc:
        print(f"[lumix] ✗ Download failed: {exc}")
        if os.path.exists(dest):
            os.remove(dest)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# FCPXML multicam generation
# ══════════════════════════════════════════════════════════════════════════════

def _tc_to_frames(tc: str, fps: int) -> int:
    """Convert HH:MM:SS:FF to total frame count."""
    h, m, s, f = (int(x) for x in re.split(r"[:.]", tc))
    return (h * 3600 + m * 60 + s) * fps + f


def _duration_frames(video_path: str, fps: int) -> int:
    """Get video duration in frames via ffprobe. Falls back to 1 hour on error."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, check=True,
        )
        return int(float(r.stdout.strip()) * fps)
    except Exception as exc:
        print(f"[fcpxml] ⚠ ffprobe failed for {Path(video_path).name}: {exc}")
        return fps * 3600   # 1-hour safe fallback


def _native_frame_rate(video_path: str, fallback_fps: int) -> tuple[int, int]:
    """
    Return (num, den) such that num/den is the clip's real video frame rate,
    e.g. (30000, 1001) for 29.97 fps or (30, 1) for a clean 30 fps.
    Declaring a clip's own real rate (rather than forcing every asset onto
    the same flat "fps" grid) matters because e.g. the Lumix S5 shoots
    30000/1001 while OBS records a clean 30/1 — telling Resolve an asset is
    30/1 when it's actually 29.97 makes the declared duration disagree with
    the file's real duration, which Resolve can reject as unreadable.
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, check=True,
        )
        num_s, den_s = r.stdout.strip().split("/")
        num, den = int(num_s), int(den_s)
        if num > 0 and den > 0:
            return num, den
    except Exception as exc:
        print(f"[fcpxml] ⚠ frame rate probe failed for {Path(video_path).name}: {exc}")
    return fallback_fps, 1


_EMBEDDED_TC_RE = re.compile(r"(\d{1,2})[:;](\d{2})[:;](\d{2})[:;.](\d{2})")


def _tc_string_to_frames(tc_str: str, fps: int) -> int:
    """
    Convert a timecode string ('HH:MM:SS:FF' or SMPTE drop-frame
    'HH:MM:SS;FF') to a nominal-fps frame count. Handles drop-frame's
    2-frames-dropped-per-minute-except-every-10th convention.
    """
    m = _EMBEDDED_TC_RE.search(tc_str)
    if not m:
        return 0
    h, mi, s, f = (int(x) for x in m.groups())
    frame_number = (h * 3600 + mi * 60 + s) * fps + f
    if ";" in tc_str:  # SMPTE drop-frame notation
        total_minutes = h * 60 + mi
        frame_number -= 2 * (total_minutes - total_minutes // 10)
    return frame_number


def _embedded_start_frames(video_path: str, native_num: int, native_den: int) -> int:
    """
    Read the QuickTime "tmcd" track's embedded starting timecode, if the
    file has one, and convert it to a frame count in the asset's own native
    rate. Real camera files (e.g. the Lumix S5's .MOV) carry this track;
    plain OBS .mp4 output does not. Declaring an asset's FCPXML "start" as
    anything other than what's actually embedded in its tmcd track makes
    Resolve refuse to open the file ("file not found") even though the
    path is correct — so this must match the real embedded value exactly.
    Returns 0 if the file has no embedded timecode track.
    """
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "d",
                "-show_entries", "stream_tags=timecode",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, check=True,
        )
        raw = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    except Exception:
        return 0

    nominal_fps = round(native_num / native_den)  # 30 for both 30/1 and 30000/1001 (NTSC)
    return _tc_string_to_frames(raw, nominal_fps)


def _t(frames: int, fps: int) -> str:
    """Frame count → FCPXML rational time string, e.g. '900/30s'."""
    return f"{frames}/{fps}s"


def _frames_to_tc(frames: int, fps: int) -> str:
    """Frame count → 'HH:MM:SS:FF' timecode string (non-drop)."""
    f = frames % fps
    total_s = frames // fps
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _sequence_tc_start(fcpxml_path: str, fps: int = FPS) -> str | None:
    """
    Read the <sequence tcStart="num/den s"> attribute back out of a
    generated FCPXML and convert it to a plain timecode string. Resolve's
    ImportTimelineFromFile doesn't honor tcStart itself (it always imports
    at 00:00:00:00), so the caller has to set it explicitly afterward via
    Timeline.SetStartTimecode().
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(fcpxml_path).getroot()
        seq = root.find(".//sequence")
        if seq is None:
            return None
        tc_start = seq.get("tcStart", "")
        num_s, _, rest = tc_start.partition("/")
        den_s = rest.rstrip("s")
        frames = round(int(num_s) / int(den_s) * fps)
        return _frames_to_tc(frames, fps)
    except Exception as exc:
        print(f"[fcpxml] ⚠ Could not read tcStart from {fcpxml_path}: {exc}")
        return None


def build_fcpxml(
    session_name: str,
    take_num: int,
    clips: list[dict],
    fps: int = FPS,
    output_dir: str | None = None,
) -> str | None:
    """
    Generate a multicam FCPXML file aligned by LTC timecodes.

    clips — list of dicts:
        {"path": "/abs/path.mp4", "timecode": "HH:MM:SS:FF", "label": "OBS"}

    Returns the path to the generated .fcpxml file, or None on failure.
    Open in DaVinci Resolve via File → Import → Timeline, or call open_in_resolve().
    """
    valid = [c for c in clips if c.get("timecode") and c.get("path")]
    if not valid:
        print("[fcpxml] ✗ No clips with timecode — cannot build multicam")
        return None

    take_label   = f"Take {take_num:02d}"
    project_name = f"{session_name} — {take_label}"

    # ── Derive per-clip data ───────────────────────────────────────────────
    # Each clip's own native frame rate is probed and preserved (its <asset>
    # declares its OWN format resource, e.g. 30000/1001 for the Lumix S5)
    # rather than being force-fit onto the canvas's flat "fps" grid — telling
    # Resolve an asset is 30/1 when it's really 29.97 makes the declared
    # duration disagree with the file's real duration, which Resolve can
    # reject outright ("file not found") rather than just misconform it.
    enriched = []
    for c in valid:
        native_num, native_den = _native_frame_rate(c["path"], fps)
        enriched.append({
            **c,
            "start_f":      _tc_to_frames(c["timecode"], fps),
            "dur_f":        _duration_frames(c["path"], fps),
            "native_num":   native_num,
            "native_den":   native_den,
            "embed_start_f": _embedded_start_frames(c["path"], native_num, native_den),
        })

    # LTC timecodes are only used to compute each angle's *relative* sync
    # offset, and are unrelated to any tmcd track embedded in the file
    # itself — assets must declare the REAL embedded start (0 if there is
    # no tmcd track, as with OBS output) or Resolve refuses to open them.
    earliest = min(d["start_f"] for d in enriched)
    mc_dur_f = max(d["start_f"] - earliest + d["dur_f"] for d in enriched)
    mc_dur   = _t(mc_dur_f, fps)
    mc_tc    = _t(earliest, fps)  # the take's real LTC-derived start timecode

    fmt_id   = "r1"
    media_id = f"r{len(enriched) + 2}"

    # ── Build XML ──────────────────────────────────────────────────────────
    L = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE fcpxml>',
        '<fcpxml version="1.9">',
        '  <resources>',
        f'    <format id="{fmt_id}" frameDuration="1/{fps}s" width="1920" height="1080"/>',
    ]

    for i, d in enumerate(enriched):
        d["fmt_id"] = f"fmt{i + 2}"
        L.append(
            f'    <format id="{d["fmt_id"]}" frameDuration="{d["native_den"]}/{d["native_num"]}s" '
            f'width="1920" height="1080"/>'
        )

    for i, d in enumerate(enriched):
        aid            = f"r{i + 2}"
        native_dur_f   = d["dur_f"] * d["native_den"]
        native_dur     = f'{native_dur_f}/{d["native_num"]}s'
        native_start_f = d["embed_start_f"] * d["native_den"]
        native_start   = f'{native_start_f}/{d["native_num"]}s'
        L += [
            f'    <asset id="{aid}" name="{d["label"]}" '
            f'start="{native_start}" '
            f'duration="{native_dur}" '
            f'hasVideo="1" hasAudio="0" format="{d["fmt_id"]}">',
            f'      <media-rep kind="original-media" src="{Path(d["path"]).as_uri()}"/>',
            '    </asset>',
        ]

    L += [
        f'    <media id="{media_id}" name="{project_name}">',
        f'      <multicam name="{project_name}" format="{fmt_id}" tcStart="{mc_tc}">',
    ]
    for i, d in enumerate(enriched):
        aid            = f"r{i + 2}"
        offset         = _t(d["start_f"] - earliest, fps)
        native_dur_f   = d["dur_f"] * d["native_den"]
        native_dur     = f'{native_dur_f}/{d["native_num"]}s'
        native_start_f = d["embed_start_f"] * d["native_den"]
        native_start   = f'{native_start_f}/{d["native_num"]}s'
        L += [
            f'        <mc-angle name="{d["label"]}" angleID="angle{i + 1}">',
            f'          <asset-clip ref="{aid}" name="{d["label"]}" offset="{offset}" '
            f'start="{native_start}" duration="{native_dur}" format="{d["fmt_id"]}"/>',
            f'        </mc-angle>',
        ]
    L += [
        '      </multicam>',
        '    </media>',
        '  </resources>',
        '  <library>',
        f'    <event name="{session_name}">',
        f'      <project name="{project_name}">',
        f'        <sequence format="{fmt_id}" duration="{mc_dur}" tcStart="{mc_tc}">',
        '          <spine>',
        f'            <mc-clip ref="{media_id}" name="{project_name}" offset="0s" duration="{mc_dur}">',
    ]
    # Video only — the camera clips' own audio is just the LTC sync tone
    # (the real program audio lives in Cubase and gets synced separately),
    # so including it here just means muting it by hand on every timeline.
    for i in range(len(enriched)):
        L.append(f'              <mc-source angleID="angle{i + 1}" srcEnable="video"/>')
    L += [
        '            </mc-clip>',
        '          </spine>',
        '        </sequence>',
        '      </project>',
        '    </event>',
        '  </library>',
        '</fcpxml>',
    ]

    # ── Write file ─────────────────────────────────────────────────────────
    if output_dir is None:
        output_dir = os.path.dirname(valid[0]["path"])
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{project_name}.fcpxml")

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        print(f"[fcpxml] ✓ Written: {out_path}")
        return out_path
    except Exception as exc:
        print(f"[fcpxml] ✗ Write failed: {exc}")
        return None


# DaVinci Resolve is not registered as a document handler for .fcpxml, so
# `open -a "DaVinci Resolve" file.fcpxml` only activates the app window —
# it never triggers an actual import. The only way to make Resolve act on
# a file is its scripting API, talking to the already-running instance.
_RESOLVE_SCRIPT_API_DEFAULT = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
_RESOLVE_SCRIPT_LIB_DEFAULT = (
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
)


def _connect_resolve():
    """Return a connected Resolve scripting object, or None if unavailable."""
    api_dir = os.environ.get("RESOLVE_SCRIPT_API", _RESOLVE_SCRIPT_API_DEFAULT)
    os.environ.setdefault("RESOLVE_SCRIPT_API", api_dir)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", _RESOLVE_SCRIPT_LIB_DEFAULT)
    modules_dir = os.path.join(api_dir, "Modules")
    if modules_dir not in sys.path:
        sys.path.append(modules_dir)
    try:
        import DaVinciResolveScript as dvr_script
        return dvr_script.scriptapp("Resolve")
    except Exception as exc:
        print(f"[fcpxml] ⚠ Resolve scripting module import failed: {exc}")
        return None


def _get_or_create_project(resolve, session_name: str | None):
    """
    Return the project to import into. Always prefers whatever project is
    currently open in Resolve — switching to a different project via
    LoadProject/CreateProject closes it out from under the user, replacing
    their whole workspace with just the newly-imported take. A
    session-named project is only created/loaded as a fallback when
    nothing is open at all.
    """
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is not None:
        return project

    if session_name:
        return pm.LoadProject(session_name) or pm.CreateProject(session_name)

    return None


_PENDING_IMPORTS_FILENAME = ".pending_resolve_imports.json"


def _load_pending_imports(media_dir: str) -> list[dict]:
    path = os.path.join(media_dir, _PENDING_IMPORTS_FILENAME)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def _save_pending_imports(media_dir: str, items: list[dict]):
    os.makedirs(media_dir, exist_ok=True)
    path = os.path.join(media_dir, _PENDING_IMPORTS_FILENAME)
    with open(path, "w") as f:
        json.dump(items, f, indent=2)


def queue_for_resolve_import(media_dir: str, fcpxml_path: str, session_name: str | None):
    """
    Queue an FCPXML for later import — used when DaVinci Resolve Free
    blocks the external scripting connection this process would otherwise
    use. Drained by the "Import Pending Lumix Takes" script installed into
    Resolve's own Workspace → Scripts → Utility menu (see
    install_resolve_import_script()), which runs *inside* Resolve and so
    isn't subject to the external-scripting restriction.
    """
    items = _load_pending_imports(media_dir)
    items.append({"fcpxml": fcpxml_path, "session_name": session_name})
    _save_pending_imports(media_dir, items)


def _import_one_fcpxml(resolve, fcpxml_path: str, session_name: str | None, timeline_name: str | None = None) -> bool:
    """
    Import a single FCPXML into Resolve: get-or-create the session's
    project, import the timeline, and set its start timecode (Resolve's
    ImportTimelineFromFile always imports at 00:00:00:00 regardless of the
    FCPXML's own tcStart, so this has to happen as a separate API call).
    """
    project = _get_or_create_project(resolve, session_name)
    if project is None:
        print(f"[fcpxml] ✗ Could not open/create project '{session_name}' for {fcpxml_path}")
        return False

    # Name comes from the fcpxml filename stem (build_fcpxml always writes
    # it as "<project_name>.fcpxml"), so the actual editable Timeline gets
    # a meaningful name instead of Resolve's generic "Timeline N" default.
    # ImportTimelineFromFile requires this name to be unique in the project
    # — if a timeline with that name already exists (e.g. re-importing the
    # same take), retry once with a uniquified name rather than failing.
    clip_name = timeline_name or Path(fcpxml_path).stem
    media_pool = project.GetMediaPool()
    timeline = media_pool.ImportTimelineFromFile(fcpxml_path, {"timelineName": clip_name})
    if not timeline:
        clip_name = f"{clip_name} ({int(time.time())})"
        print(f"[fcpxml] ⚠ Import failed (likely duplicate timeline name) — retrying as {clip_name!r}")
        timeline = media_pool.ImportTimelineFromFile(fcpxml_path, {"timelineName": clip_name})
    if not timeline:
        print(f"[fcpxml] ✗ Resolve's ImportTimelineFromFile failed for {fcpxml_path}")
        return False

    tc = _sequence_tc_start(fcpxml_path)
    if tc:
        ok = timeline.SetStartTimecode(tc)
        print(f"[fcpxml]   SetStartTimecode({tc!r}) -> {ok}; "
              f"GetStartTimecode() now = {timeline.GetStartTimecode()!r}")

    print(f"[fcpxml] ✓ Imported {fcpxml_path} into project '{project.GetName()}'")
    return True


def import_pending_queue(resolve, media_dir: str) -> dict:
    """
    Drain the queue written by queue_for_resolve_import(), importing each
    FCPXML into its session's Resolve project. Meant to be called with a
    `resolve` object that's already connected — either the external
    scripting connection (Studio), or the object Resolve pre-injects into
    scripts run from its own Workspace → Scripts menu (Free-compatible).
    Returns {"imported": int, "failed": int, "remaining": int}.
    """
    items = _load_pending_imports(media_dir)
    remaining = []
    imported = failed = 0

    for item in items:
        if _import_one_fcpxml(resolve, item["fcpxml"], item.get("session_name")):
            imported += 1
        else:
            remaining.append(item)
            failed += 1

    _save_pending_imports(media_dir, remaining)
    return {"imported": imported, "failed": failed, "remaining": len(remaining)}


def find_latest_fcpxml(media_dir: str) -> tuple[str, str] | None:
    """
    Return (fcpxml_path, session_name) for the most recently written
    .fcpxml under media_dir, or None if there isn't one. session_name is
    derived from the standard media_dir/<session>/Take NN/*.fcpxml layout.
    """
    candidates = []
    for root, _dirs, files in os.walk(media_dir):
        for fn in files:
            if fn.endswith(".fcpxml"):
                path = os.path.join(root, fn)
                candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None

    _, latest_path = max(candidates, key=lambda c: c[0])
    take_dir = os.path.dirname(latest_path)
    session_name = os.path.basename(os.path.dirname(take_dir))
    return latest_path, session_name


def import_latest_take(resolve, media_dir: str) -> dict:
    """
    Re-import the most recently generated take's FCPXML, regardless of
    whether it's still in the pending-import queue — handy for re-testing
    the same take repeatedly without needing to requeue it each time.
    Returns {"imported": bool, "fcpxml": str | None}.
    """
    found = find_latest_fcpxml(media_dir)
    if not found:
        print(f"[fcpxml] ✗ No .fcpxml files found under {media_dir}")
        return {"imported": False, "fcpxml": None}

    fcpxml_path, session_name = found
    ok = _import_one_fcpxml(resolve, fcpxml_path, session_name)
    return {"imported": ok, "fcpxml": fcpxml_path}


def _debug_walk_folder(folder, depth: int):
    indent = "  " * depth
    print(f"{indent}[folder] {folder.GetName()}")
    for clip in folder.GetClipList():
        props = clip.GetClipProperty()
        name = props.get("Clip Name", "?")
        ctype = props.get("Type", "?")
        print(f"{indent}  - Clip Name={name!r} Type={ctype!r} all_props={props}")
    for sub in folder.GetSubFolderList():
        _debug_walk_folder(sub, depth + 1)


def debug_list_media_pool(resolve):
    """
    Diagnostic: print every Media Pool item in the current project (name,
    type, all clip properties) so we can see exactly what object the
    multicam clip really is and which property key (if any) actually
    controls its displayed name — SetClipProperty("Clip Name", ...) alone
    wasn't enough to rename it.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("[debug] No current project open")
        return
    print(f"[debug] Project: {project.GetName()}")
    _debug_walk_folder(project.GetMediaPool().GetRootFolder(), 0)


def _find_clips_by_type(folder, clip_type: str) -> list:
    found = [c for c in folder.GetClipList() if c.GetClipProperty().get("Type") == clip_type]
    for sub in folder.GetSubFolderList():
        found += _find_clips_by_type(sub, clip_type)
    return found


def debug_try_rename_multicam(resolve, name: str = "TEST_RENAME"):
    """
    Diagnostic: find every Multicam-type clip in the current project and
    try renaming it via several different property keys, reporting which
    (if any) actually take effect — SetClipProperty("Clip Name", ...)
    alone returned False for this clip type, so this checks the other
    plausible keys instead of guessing again.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("[debug] No current project open")
        return

    clips = _find_clips_by_type(project.GetMediaPool().GetRootFolder(), "Multicam")
    print(f"[debug] Found {len(clips)} Multicam clip(s)")
    for clip in clips:
        for key in ("Clip Name", "File Name", "Name"):
            try:
                result = clip.SetClipProperty(key, name)
                print(f"[debug]   SetClipProperty({key!r}, {name!r}) -> {result}")
            except Exception as exc:
                print(f"[debug]   SetClipProperty({key!r}, ...) raised: {exc}")
        print(f"[debug]   Clip Name is now: {clip.GetClipProperty('Clip Name')!r}")


def insert_multicam_into_current_timeline(resolve, fcpxml_path: str, session_name: str | None = None) -> bool:
    """
    Import fcpxml (which creates the Multicam clip in the Media Pool, plus
    an unavoidable auxiliary standalone Timeline as a side effect of
    ImportTimelineFromFile), then insert *only* the Multicam clip into
    whatever timeline the user already had open, positioned at the frame
    matching its real production timecode.

    This matters because dropping the auxiliary standalone Timeline into
    another timeline instead just nests it as a compound clip, which loses
    multicam angle-switching (the 1-9 keys) entirely — only the
    Multicam-type MediaPoolItem itself keeps that behavior when placed on
    a timeline.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        print("[fcpxml] ✗ No current project open")
        return False

    target_timeline = project.GetCurrentTimeline()
    if target_timeline is None:
        print("[fcpxml] ✗ No active timeline — open the timeline to insert into first")
        return False
    target_name = target_timeline.GetName()
    print(f"[fcpxml] Target timeline: {target_name!r}")

    # This auxiliary timeline is a disposable byproduct — only the Multicam
    # clip it creates in the Media Pool is actually used below — so give it
    # a unique scratch name rather than reusing the take's real name, which
    # may already exist as a timeline from a previous normal import.
    scratch_name = f"_scratch_{Path(fcpxml_path).stem}_{int(time.time())}"
    if not _import_one_fcpxml(resolve, fcpxml_path, session_name, timeline_name=scratch_name):
        return False

    # ImportTimelineFromFile switches the project's current timeline to the
    # new auxiliary one it just created — switch back before inserting.
    after_import_name = project.GetCurrentTimeline().GetName() if project.GetCurrentTimeline() else None
    print(f"[fcpxml] Current timeline right after import: {after_import_name!r}")

    switch_ok = project.SetCurrentTimeline(target_timeline)
    verify_timeline = project.GetCurrentTimeline()
    verify_name = verify_timeline.GetName() if verify_timeline else None
    print(f"[fcpxml] SetCurrentTimeline({target_name!r}) -> {switch_ok}; "
          f"GetCurrentTimeline() now reports {verify_name!r}")

    if verify_name != target_name:
        print(f"[fcpxml] ✗ Current timeline is {verify_name!r}, not the target {target_name!r} — aborting")
        return False

    mc_clips = _find_clips_by_type(project.GetMediaPool().GetRootFolder(), "Multicam")
    if not mc_clips:
        print("[fcpxml] ✗ No Multicam clip found after import")
        return False
    mc_clip = mc_clips[-1]  # the one we just created
    mc_start_tc = mc_clip.GetClipProperty("Start TC")
    mc_frames = int(mc_clip.GetClipProperty("Frames") or 0)
    print(f"[fcpxml] Multicam clip: Start TC={mc_start_tc!r} Frames={mc_frames}")

    target_start_tc = target_timeline.GetStartTimecode()
    mc_start_f = _tc_string_to_frames(mc_start_tc, FPS)
    target_start_f = _tc_string_to_frames(target_start_tc, FPS)
    record_frame = mc_start_f - target_start_f
    print(f"[fcpxml] target Start TC={target_start_tc!r} -> mc_start_f={mc_start_f} "
          f"target_start_f={target_start_f} record_frame={record_frame}")

    if record_frame < 0:
        print(f"[fcpxml] ✗ record_frame is negative ({record_frame}) — this clip starts "
              f"before the target timeline's own start TC, can't position it")
        return False

    video_track_count = target_timeline.GetTrackCount("video")
    print(f"[fcpxml] {target_name!r} has {video_track_count} video track(s) before insert")
    if video_track_count < 1:
        added = target_timeline.AddTrack("video")
        print(f"[fcpxml]   AddTrack('video') -> {added}")

    clip_info = {
        "mediaPoolItem": mc_clip,
        "startFrame": 0,
        "endFrame": mc_frames,
        "mediaType": 1,  # video
        "trackIndex": 1,
        "recordFrame": record_frame,
    }
    pre_append_timeline = project.GetCurrentTimeline()
    pre_append_name = pre_append_timeline.GetName() if pre_append_timeline else None
    print(f"[fcpxml] Current timeline right before AppendToTimeline: {pre_append_name!r}")

    result = project.GetMediaPool().AppendToTimeline([clip_info])
    print(f"[fcpxml]   AppendToTimeline({clip_info}) -> {result}")

    if result:
        print(f"[fcpxml]   Inserted item start frame: {result[0].GetStart()}, "
              f"end frame: {result[0].GetEnd()}")

    items_now = target_timeline.GetItemListInTrack("video", 1)
    print(f"[fcpxml]   {target_name!r} video track 1 now has {len(items_now)} item(s)")

    return bool(result)


def open_in_resolve(fcpxml_path: str, session_name: str | None = None, media_dir: str | None = None) -> bool:
    """
    Import an FCPXML timeline directly into DaVinci Resolve via its
    scripting API (Resolve must already be running).

    DaVinci Resolve Free blocks external scripting connections entirely
    (Studio-only) — Resolve isn't registered as an .fcpxml document handler,
    so there's no way to make it act on a file from outside the app. On
    Free, this instead queues the file (if media_dir is given) for the
    "Import Pending Lumix Takes" Resolve-internal script to pick up — see
    install_resolve_scripts().
    """
    resolve = _connect_resolve()
    if resolve is None:
        print("[fcpxml] ✗ Could not connect to DaVinci Resolve's scripting API "
              "(expected on Resolve Free — this requires Studio).")
        if media_dir:
            queue_for_resolve_import(media_dir, fcpxml_path, session_name)
            print("[fcpxml]   Queued for import — in Resolve: Workspace → Scripts → "
                  "Utility → Import Pending Lumix Takes")
        return False

    return _import_one_fcpxml(resolve, fcpxml_path, session_name)


_RESOLVE_SCRIPTS_UTILITY_DIR = os.path.expanduser(
    "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
)

_RESOLVE_SCRIPT_PREAMBLE = '''\
# Auto-generated by resolve_helper.install_resolve_scripts() — do not edit by hand,
# it's rewritten on every server.py startup. Runs inside DaVinci Resolve (Workspace ->
# Scripts -> Utility) to import FCPXML timelines from the Lumix Controller server,
# working around Resolve Free's external-scripting block.
import sys

sys.path.insert(0, {project_dir!r})
from resolve_helper import (
    import_pending_queue,
    import_latest_take,
    debug_list_media_pool,
    debug_try_rename_multicam,
    find_latest_fcpxml,
    insert_multicam_into_current_timeline,
)

MEDIA_DIR = {media_dir!r}

try:
    resolve
except NameError:
    import DaVinciResolveScript as dvr_script
    resolve = dvr_script.scriptapp("Resolve")
'''

_RESOLVE_SCRIPTS = {
    "Import Pending Lumix Takes.py": '''\
result = import_pending_queue(resolve, MEDIA_DIR)
print(f"[Lumix] Imported {result['imported']}, failed {result['failed']}, "
      f"{result['remaining']} still pending")
''',
    "Import Latest Take Again.py": '''\
result = import_latest_take(resolve, MEDIA_DIR)
if result["imported"]:
    print(f"[Lumix] Re-imported {result['fcpxml']}")
else:
    print(f"[Lumix] Re-import failed for {result['fcpxml']}")
''',
    "Insert Latest Multicam Into Current Timeline.py": '''\
found = find_latest_fcpxml(MEDIA_DIR)
if found:
    fcpxml_path, session_name = found
    ok = insert_multicam_into_current_timeline(resolve, fcpxml_path, session_name)
    print(f"[Lumix] Insert {'succeeded' if ok else 'FAILED'} for {fcpxml_path}")
else:
    print(f"[Lumix] No .fcpxml files found under {MEDIA_DIR}")
''',
}

# Diagnostic-only scripts — written to resolve_scripts/ for reference but not
# installed into Resolve's Scripts menu (used them to debug the Multicam
# clip naming/insert issues; not needed for normal day-to-day use).
_RESOLVE_DEBUG_SCRIPTS = {
    "Debug Media Pool.py": '''\
debug_list_media_pool(resolve)
''',
    "Debug Try Rename Multicam.py": '''\
debug_try_rename_multicam(resolve)
''',
}


_RESOLVE_SCRIPTS_SOURCE_DIRNAME = "resolve_scripts"


def install_resolve_scripts(media_dir: str):
    """
    Write the Lumix Controller's Resolve-internal scripts into
    <project_dir>/resolve_scripts/ (real files — readable/editable with a
    normal editor, version-controllable). _RESOLVE_SCRIPTS get symlinked
    into Resolve's own Workspace -> Scripts -> Utility folder;
    _RESOLVE_DEBUG_SCRIPTS are written locally for reference but not
    installed (and any stale symlink from a previous install is removed).
    Safe to call on every server startup — rewrites/re-links every time.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    scripts_dir = os.path.join(project_dir, _RESOLVE_SCRIPTS_SOURCE_DIRNAME)
    preamble = _RESOLVE_SCRIPT_PREAMBLE.format(project_dir=project_dir, media_dir=media_dir)
    try:
        os.makedirs(scripts_dir, exist_ok=True)
        os.makedirs(_RESOLVE_SCRIPTS_UTILITY_DIR, exist_ok=True)

        for name, body in _RESOLVE_SCRIPTS.items():
            source_path = os.path.join(scripts_dir, name)
            with open(source_path, "w") as f:
                f.write(preamble + "\n" + body)
            os.chmod(source_path, 0o755)

            link_path = os.path.join(_RESOLVE_SCRIPTS_UTILITY_DIR, name)
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(source_path, link_path)
            print(f"[resolve] ✓ {_RESOLVE_SCRIPTS_SOURCE_DIRNAME}/{name} -> "
                  f"Workspace → Scripts → Utility → {name.removesuffix('.py')}")

        for name, body in _RESOLVE_DEBUG_SCRIPTS.items():
            source_path = os.path.join(scripts_dir, name)
            with open(source_path, "w") as f:
                f.write(preamble + "\n" + body)
            os.chmod(source_path, 0o755)

            link_path = os.path.join(_RESOLVE_SCRIPTS_UTILITY_DIR, name)
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            print(f"[resolve] ✓ {_RESOLVE_SCRIPTS_SOURCE_DIRNAME}/{name} (local only, not installed)")
    except Exception as exc:
        print(f"[resolve] ⚠ Could not install Resolve scripts: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# SD card import (fallback when WiFi transfer is too slow)
#
# Scans a mounted SD card for clips not yet copied, pairs each with the
# closest not-yet-used OBS recording by creation timestamp (not LTC — this
# runs standalone, without a live record session to derive LTC-based sync
# offsets from until decode_ltc runs on the paired files below), copies the
# clip into a new Take folder, and runs it through the same decode/FCPXML/
# Resolve pipeline as a normal WiFi take.
# ══════════════════════════════════════════════════════════════════════════════

SD_IMPORT_MAX_TIME_DELTA = 300  # seconds — max gap between an SD clip and an
                                 # OBS clip's creation time to count as a match
_SD_IMPORT_STATE_FILENAME = ".sdcard_import_state.json"


def find_sd_card() -> str | None:
    """Return the mount path of a plugged-in camera SD card (has a DCIM folder), or None."""
    volumes_dir = "/Volumes"
    try:
        for name in os.listdir(volumes_dir):
            mount = os.path.join(volumes_dir, name)
            if os.path.isdir(os.path.join(mount, "DCIM")):
                return mount
    except Exception as exc:
        print(f"[sdcard] ✗ Could not scan {volumes_dir}: {exc}")
    return None


def _file_creation_time(path: str) -> datetime:
    """
    Timestamp used for SD-card/OBS matching: filesystem mtime, NOT the
    embedded QuickTime "creation_time" tag. The Lumix S5 (like many
    cameras) writes its *local* clock reading into that tag but labels it
    with a "Z" (UTC) suffix — a firmware mislabeling bug — which silently
    shifted matches by the local UTC offset (2h for CEST) whenever the OBS
    side didn't have the same tag to (accidentally) cancel it out against.
    mtime is a raw epoch value and isn't subject to that mislabeling.
    """
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


def list_sdcard_clips(mount_path: str) -> list[dict]:
    """Return all video clips under <mount>/DCIM, each with a parsed creation datetime."""
    clips = []
    dcim = os.path.join(mount_path, "DCIM")
    for root, _dirs, files in os.walk(dcim):
        for fn in files:
            # Skip macOS AppleDouble shadow files (e.g. "._P1000123.MOV") that
            # accumulate on FAT32/exFAT cards — same extension, not real media.
            if fn.startswith("._"):
                continue
            if fn.upper().endswith((".MP4", ".MOV")):
                path = os.path.join(root, fn)
                clips.append({"path": path, "filename": fn, "datetime": _file_creation_time(path)})
    return clips


def list_unconsumed_obs_clips(obs_dir: str, consumed_paths: set) -> list[dict]:
    """Return OBS .mp4 files in obs_dir not already marked as consumed by a completed take."""
    clips = []
    try:
        for fn in os.listdir(obs_dir):
            if not fn.lower().endswith(".mp4"):
                continue
            path = os.path.join(obs_dir, fn)
            if path in consumed_paths:
                continue
            clips.append({"path": path, "filename": fn, "datetime": _file_creation_time(path)})
    except Exception as exc:
        print(f"[sdcard] ✗ Could not scan OBS dir {obs_dir}: {exc}")
    return clips


def _load_import_state(media_dir: str) -> dict:
    path = os.path.join(media_dir, _SD_IMPORT_STATE_FILENAME)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"imported_lumix_filenames": [], "consumed_obs_paths": []}


def _save_import_state(media_dir: str, state: dict):
    os.makedirs(media_dir, exist_ok=True)
    path = os.path.join(media_dir, _SD_IMPORT_STATE_FILENAME)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def mark_obs_consumed(media_dir: str, obs_path: str):
    """Record that obs_path was used in a completed take, so the SD-card
    import feature won't try to pair it with a card clip later."""
    state = _load_import_state(media_dir)
    if obs_path not in state["consumed_obs_paths"]:
        state["consumed_obs_paths"].append(obs_path)
        _save_import_state(media_dir, state)


def import_from_sdcard(
    session_name: str,
    take_start: int,
    media_dir: str,
    obs_dir: str,
    progress_cb=None,
    only_latest: bool = True,
) -> dict:
    """
    Scan a mounted SD card for clips not yet copied, pair each with the
    closest not-yet-used OBS recording by creation timestamp, and run them
    through the same decode/FCPXML/Resolve pipeline as a normal WiFi take.
    Clips with no OBS match within SD_IMPORT_MAX_TIME_DELTA seconds are
    skipped (logged, left for the next scan) rather than guessed at.

    *only_latest* (default True): only import the single most recent new
    clip, for the "record several takes, only the last one matters"
    workflow — the other new-but-unprocessed clips are left untouched
    (not marked imported/consumed), so they're still available later if
    the user re-runs with only_latest=False.

    *progress_cb(take_num, filename)* is called before each take starts.
    Returns {"ok": bool, "imported": int, "skipped": int, "ltc_failed": int,
             "takes": [...], "failures": [...]}. A clip whose LTC decode
    fails on both angles is NOT counted in "imported" — it still gets
    copied and consumes its OBS match (retrying won't fix a physically
    absent LTC signal), but is reported separately in "failures" so the
    caller can surface a real error instead of a false "done".
    """
    mount = find_sd_card()
    if not mount:
        return {"ok": False, "imported": 0, "skipped": 0, "ltc_failed": 0, "takes": [], "failures": [],
                 "message": "No SD card found (no mounted volume with a DCIM folder)"}

    state           = _load_import_state(media_dir)
    already_imported = set(state["imported_lumix_filenames"])
    consumed_obs     = set(state["consumed_obs_paths"])

    sd_clips = [c for c in list_sdcard_clips(mount) if c["filename"] not in already_imported]
    if not sd_clips:
        return {"ok": True, "imported": 0, "skipped": 0, "ltc_failed": 0, "takes": [], "failures": [],
                 "message": "No new clips on SD card"}
    sd_clips.sort(key=lambda c: c["datetime"])
    if only_latest:
        sd_clips = sd_clips[-1:]

    obs_clips = list_unconsumed_obs_clips(obs_dir, consumed_obs)

    takes      = []
    failures   = []
    take_num   = take_start
    skipped    = 0

    for sd_clip in sd_clips:
        best, best_delta = None, None
        for obs_clip in obs_clips:
            if obs_clip["path"] in consumed_obs:
                continue
            delta = abs((sd_clip["datetime"] - obs_clip["datetime"]).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = obs_clip, delta

        if best is None or best_delta > SD_IMPORT_MAX_TIME_DELTA:
            reason = (f"closest OBS clip is {best_delta:.0f}s away"
                      if best is not None else "no unconsumed OBS clips")
            print(f"[sdcard] ⚠ Skipping {sd_clip['filename']} — {reason}")
            skipped += 1
            continue

        take_num += 1
        if progress_cb:
            progress_cb(take_num, sd_clip["filename"])

        session_dir = os.path.join(media_dir, session_name, f"Take {take_num:02d}")
        os.makedirs(session_dir, exist_ok=True)

        dest = os.path.join(session_dir, sd_clip["filename"])
        shutil.copy2(sd_clip["path"], dest)
        print(f"[sdcard] ✓ Copied {sd_clip['filename']} → {dest}")

        clip_list = [
            {"path": best["path"], "label": "OBS",      "timecode": decode_ltc(best["path"])},
            {"path": dest,         "label": "Lumix S5", "timecode": decode_ltc(dest)},
        ]

        fcpxml_path = None
        if any(c["timecode"] for c in clip_list):
            fcpxml_path = build_fcpxml(session_name, take_num, clip_list, output_dir=session_dir)
            if not fcpxml_path:
                print(f"[sdcard] ✗ FCPXML generation failed for take {take_num:02d}")
        else:
            print(f"[sdcard] ✗ LTC decode failed on both clips for take {take_num:02d}")

        consumed_obs.add(best["path"])
        already_imported.add(sd_clip["filename"])

        take_info = {
            "take":            take_num,
            "lumix_filename":  sd_clip["filename"],
            "obs_filename":    os.path.basename(best["path"]),
            "time_delta_s":    round(best_delta, 1),
            "fcpxml":          fcpxml_path,
        }

        if fcpxml_path:
            open_in_resolve(fcpxml_path, session_name=session_name, media_dir=media_dir)
            takes.append(take_info)
        else:
            failures.append(take_info)

    state["imported_lumix_filenames"] = sorted(already_imported)
    state["consumed_obs_paths"]       = sorted(consumed_obs)
    _save_import_state(media_dir, state)

    return {
        "ok":         True,
        "imported":   len(takes),
        "skipped":    skipped,
        "ltc_failed": len(failures),
        "takes":      takes,
        "failures":   failures,
    }
