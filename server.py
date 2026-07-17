#!/usr/bin/env python3
"""
Lumix S5 Camera Controller
────────────────────────────────────────────────────────────────────
- Flask web server → iPhone UI at http://<mac-ip>:5050
- Cubase transport → virtual MIDI port; lumix_transport.js binds notes to it directly (no manual Cubase mapping)
- Camera control  → Lumix WiFi HTTP API (video_recstart / video_recstop)
────────────────────────────────────────────────────────────────────
Edit CAMERA_IP below, then run:  python server.py
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import socket
import requests
from flask import Flask, jsonify, request

# ─── CONFIG ─────────────────────────────────────────────────────────────────
CAMERA_IP    = "192.168.178.122"   # ← S5's IP address
PORT         = 5050                # web server port
OBS_HOST     = "localhost"         # OBS WebSocket host
OBS_PORT     = 4455                # OBS WebSocket port (Tools → WebSocket Server Settings)
OBS_PASSWORD = ""                  # leave empty if no password set in OBS
MEDIA_DIR    = os.path.expanduser("~/Movies/LumixSessions")  # where S5 clips are saved
OBS_RECORDING_DIR = os.path.expanduser("~/Movies")            # OBS's own output folder
# ────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
_lock                 = threading.Lock()
_recording            = False
_armed                = False   # when True, Cubase play/stop triggers the camera
_cubase_playing       = False   # tracks Cubase transport state
_download_from_camera = False   # WiFi download over the S5 is slow — off by default;
                                 # use the SD card import flow instead when disabled
_sdcard_only_latest   = True    # SD import: only the most recent new clip by default
                                 # ("record several takes, only the last matters"); unchecking
                                 # imports every new clip found on the card

# ── Session / take tracking ──────────────────────────────────────────────────
_SESSION_NAME_FILE = os.path.join(MEDIA_DIR, ".last_session_name.txt")


def _load_session_name() -> str:
    try:
        with open(_SESSION_NAME_FILE) as f:
            name = f.read().strip()
            if name:
                return name
    except Exception:
        pass
    return "Session"


def _save_session_name(name: str):
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with open(_SESSION_NAME_FILE, "w") as f:
            f.write(name)
    except Exception as exc:
        print(f"[session] ⚠ Could not persist session name: {exc}")


def _next_take_number(session_name: str) -> int:
    """
    Highest existing "Take NN" folder under this session, so numbering
    survives server restarts and stays correct per-session (switching
    sessions picks up wherever that session's own folder state left off)
    without needing a separate counter file that could drift from disk.
    """
    session_dir = os.path.join(MEDIA_DIR, session_name)
    highest = 0
    try:
        for entry in os.listdir(session_dir):
            m = re.match(r"Take (\d+)$", entry)
            if m:
                highest = max(highest, int(m.group(1)))
    except FileNotFoundError:
        pass
    return highest


_session_name  = _load_session_name()   # persists across server restarts; set via /session endpoint
_take_number   = _next_take_number(_session_name)

# ── Post-production pipeline state ───────────────────────────────────────────
_pipeline = {
    "state":   "idle",   # idle | downloading | decoding | resolve | done | error
    "message": "",
    "take":    0,
}


# ── Camera control ───────────────────────────────────────────────────────────

def _cam(value: str) -> bool:
    """Send a single command to the camera. Returns True on success."""
    try:
        resp = requests.get(
            f"http://{CAMERA_IP}/cam.cgi",
            params={"mode": "camcmd", "value": value},
            timeout=3,
        )
        ok = "<result>ok</result>" in resp.text
        if not ok:
            print(f"[camera] ⚠ command '{value}' response: {resp.text.strip()}")
        return ok
    except requests.exceptions.ConnectionError:
        print(f"[camera] ✗ Cannot reach {CAMERA_IP} — is the camera on the same WiFi?")
        return False
    except Exception as e:
        print(f"[camera] ✗ {e}")
        return False


# ── Camera battery / storage status (background-polled, cached) ──────────────
#
# Polled on a timer rather than fetched inside /status directly, so the
# iPhone UI's frequent polling never blocks on a camera HTTP round-trip —
# /status just reads whatever this thread last cached.

_CAMERA_STATE_POLL_INTERVAL = 15  # seconds
_camera_state = {"battery": None, "video_remaining_min": None, "reachable": False}


def _poll_camera_state():
    while True:
        try:
            resp = requests.get(
                f"http://{CAMERA_IP}/cam.cgi", params={"mode": "getstate"}, timeout=3
            )
            text = resp.text
            batt   = re.search(r"<batt>([^<]*)</batt>", text)
            remain = re.search(r"<video_remaincapacity>([^<]*)</video_remaincapacity>", text)
            _camera_state["battery"]             = batt.group(1) if batt else None
            _camera_state["video_remaining_min"] = int(remain.group(1)) if remain else None
            _camera_state["reachable"]           = True
        except Exception:
            _camera_state["reachable"] = False
        time.sleep(_CAMERA_STATE_POLL_INTERVAL)


# ── Cubase transport control (virtual MIDI port) ─────────────────────────────
#
# HTTP isn't viable from inside a Cubase MIDI Remote script on this install —
# require('http') and require('child_process') both throw ("invalid module
# load callback return value"), confirmed by directly testing both. MIDI is
# the one thing guaranteed to work, so server.py creates a virtual port and
# lumix_transport.js binds these two notes to Transport Record/Stop
# directly in code (via mMidiBinding + makeValueBinding) — unlike the old
# Generic Remote setup, there's no manual per-note mapping step in
# Cubase's UI. Bound to mRecord, not mStart, so this actually arms +
# starts recording rather than just playback.

NOTE_RECORD = 20   # channel 1 (0-based: 0) — must match lumix_transport.js
NOTE_STOP   = 21

_midi_out = None   # rtmidi.MidiOut — Python's "out" is Cubase's "in"
_midi_in  = None   # rtmidi.MidiIn — unused for data, but Cubase's
                    # detectPortPair() needs a real in+out port pair to match


def _setup_virtual_midi_out():
    global _midi_out, _midi_in
    try:
        import rtmidi
        _midi_out = rtmidi.MidiOut()
        _midi_out.open_virtual_port("Lumix Controller")
        _midi_in = rtmidi.MidiIn()
        _midi_in.open_virtual_port("Lumix Controller")
        print("[midi] ✓ Virtual MIDI port 'Lumix Controller' created (in + out)")
    except Exception as e:
        print(f"[midi] ✗ Could not create virtual MIDI port: {e}")
        print("[midi]   Cubase transport control disabled; iPhone manual button still works.")


def _cubase_record():
    if _midi_out is None:
        return
    try:
        _midi_out.send_message([0x90, NOTE_RECORD, 0x7F])  # Note On
        _midi_out.send_message([0x80, NOTE_RECORD, 0x00])  # Note Off
        print("[cubase] ● record MIDI sent")
    except Exception as e:
        print(f"[cubase] ✗ record failed: {e}")


def _cubase_stop():
    if _midi_out is None:
        return
    try:
        _midi_out.send_message([0x90, NOTE_STOP, 0x7F])  # Note On
        _midi_out.send_message([0x80, NOTE_STOP, 0x00])  # Note Off
        print("[cubase] ■ stop MIDI sent")
    except Exception as e:
        print(f"[cubase] ✗ stop failed: {e}")


# Known real locations found on this machine for Cubase's MIDI Remote local
# scripts folder (varies by Cubase version/install — discovered via mdfind
# since neither matched the commonly-documented path). Copied, not
# symlinked — unlike Resolve's Scripts folder, Cubase's script scanner
# doesn't appear to pick up symlinked files (confirmed: it silently
# skipped one entirely rather than loading or erroring on it). So this
# needs a server restart to push edits, not live like the Resolve ones.
_CUBASE_SCRIPT_INSTALL_PATHS = [
    os.path.expanduser("~/Cubase MIDI Remote/Driver Scripts/Local/lumix_transport.js"),
    os.path.expanduser(
        "~/Documents/Steinberg/Cubase/MIDI Remote/Driver Scripts/Local/lumix/transport/lumix_transport.js"
    ),
]


def install_cubase_script():
    source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lumix_transport.js")
    for dest_path in _CUBASE_SCRIPT_INSTALL_PATHS:
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if os.path.islink(dest_path) or os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.copy2(source_path, dest_path)
            print(f"[cubase] ✓ lumix_transport.js -> {dest_path}")
        except Exception as exc:
            print(f"[cubase] ⚠ Could not install script to {dest_path}: {exc}")


# ── OBS control ──────────────────────────────────────────────────────────────

_obs = None   # obsws_python ReqClient, set up at startup


def _setup_obs():
    global _obs
    try:
        import obsws_python as obs
        _obs = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        print(f"[obs] ✓ Connected to OBS WebSocket at {OBS_HOST}:{OBS_PORT}")
    except Exception as e:
        print(f"[obs] ✗ Could not connect to OBS: {e}")
        print("[obs]   Make sure OBS is running and WebSocket server is enabled.")


def _obs_is_connected() -> bool:
    """
    Live liveness check, not just "was _obs set once at startup" — a
    stale/dropped connection object stays non-None, so a real round-trip
    call is the only way to know OBS is actually responding right now.
    """
    if _obs is None:
        return False
    try:
        _obs.get_version()
        return True
    except Exception:
        return False


def _obs_start():
    if _obs is None:
        return
    try:
        _obs.start_record()
        print("[obs] ▶ Recording started")
    except Exception as e:
        print(f"[obs] ✗ start_record failed: {e}")


def _obs_stop() -> str | None:
    """Stop OBS recording. Returns the output file path, or None."""
    if _obs is None:
        return None
    try:
        resp = _obs.stop_record()
        path = getattr(resp, "output_path", None)
        print(f"[obs] ■ Recording stopped → {path}")
        return path
    except Exception as e:
        print(f"[obs] ✗ stop_record failed: {e}")
        return None


RECORD_START_LTC_SETTLE_S = 3.0  # Cubase must be playing LTC before the camera/OBS
                                  # start recording, or the first moment of the take has
                                  # no valid timecode to decode


def record_start(trigger_cubase: bool = False) -> tuple[bool, str]:
    """Returns (ok, reason) — reason is only meaningful when ok is False."""
    global _recording
    with _lock:
        if _recording:
            if trigger_cubase:
                _cubase_record()
            return True, ""                # already recording, nothing to do

        # Pre-flight: don't trigger Cubase or the camera at all if OBS won't
        # actually produce a clip — a silent OBS failure previously left the
        # UI reporting a successful take with no way to multicam-sync it.
        if not _obs_is_connected():
            msg = "OBS not connected — recording aborted (no OBS clip would be produced)"
            print(f"[obs] ✗ {msg}")
            return False, msg

        if trigger_cubase:
            _cubase_record()               # start Cubase (and its LTC generator) first...
            time.sleep(RECORD_START_LTC_SETTLE_S)  # ...and let it settle before the camera starts

        _cam("recmode")                    # ensure camera is in record mode
        ok = _cam("video_recstart")
        if ok:
            _recording = True
            print("▶  REC started")
            threading.Thread(target=_obs_start, daemon=True).start()
            return True, ""
        return False, "Camera did not confirm recording start — check WiFi connection"


def record_stop(trigger_cubase: bool = False) -> bool:
    global _recording, _take_number
    with _lock:
        if not _recording:
            if trigger_cubase:
                _cubase_stop()
            return True                    # already stopped, nothing to do
        ok = _cam("video_recstop")
        if ok:
            _recording = False
            _take_number += 1
            take_snap    = _take_number
            session_snap = _session_name
            print(f"■  REC stopped — session '{session_snap}' take {take_snap}")

            # Stop OBS and collect its output path, then run the full pipeline
            def _stop_and_pipeline():
                obs_path = _obs_stop()
                _run_pipeline(session_snap, take_snap, obs_path)

            threading.Thread(target=_stop_and_pipeline, daemon=True).start()
            if trigger_cubase:
                _cubase_stop()
        return ok


# ── Post-production pipeline ─────────────────────────────────────────────────

def _set_pipeline(state: str, message: str):
    _pipeline["state"]   = state
    _pipeline["message"] = message
    print(f"[pipeline] [{state}] {message}")


def _run_pipeline(session_name: str, take_num: int, obs_path: str | None):
    """
    Background thread: download Lumix clip, decode LTC on both clips,
    then create the multicam take in DaVinci Resolve.
    """
    from resolve_helper import decode_ltc, download_lumix_clip, build_fcpxml, open_in_resolve, mark_obs_consumed

    _pipeline["take"] = take_num

    if not _download_from_camera:
        _set_pipeline(
            "idle",
            "S5 WiFi download disabled — insert the SD card and use IMPORT SD CARD",
        )
        return

    session_dir = os.path.join(MEDIA_DIR, session_name, f"Take {take_num:02d}")
    os.makedirs(session_dir, exist_ok=True)

    clips = []

    # ── OBS clip ─────────────────────────────────────────────────────────
    if obs_path and os.path.exists(obs_path):
        clips.append({"path": obs_path, "label": "OBS", "timecode": None})
    else:
        print(f"[pipeline] ⚠ OBS file not found: {obs_path}")

    # ── Lumix clip (WiFi download) ────────────────────────────────────────
    _set_pipeline("downloading", "Downloading S5 footage over WiFi …")

    def _lumix_progress(done, total):
        if total:
            pct = int(done / total * 100)
            _set_pipeline("downloading", f"Downloading S5 footage … {pct}%")

    lumix_path = download_lumix_clip(CAMERA_IP, session_dir, progress_cb=_lumix_progress)
    if lumix_path:
        clips.append({"path": lumix_path, "label": "Lumix S5", "timecode": None})
    else:
        _set_pipeline("error", "S5 download failed — check WiFi connection")
        return

    # ── Decode LTC from each clip ─────────────────────────────────────────
    _set_pipeline("decoding", "Decoding LTC timecode …")
    for clip in clips:
        tc = decode_ltc(clip["path"])
        clip["timecode"] = tc
        status = tc if tc else "⚠ not found"
        print(f"[pipeline] LTC {clip['label']}: {status}")

    if not any(c["timecode"] for c in clips):
        _set_pipeline("error", "LTC decode failed on all clips — check audio channel")
        return

    # ── Build FCPXML and open in DaVinci Resolve ──────────────────────────
    _set_pipeline("resolve", "Building FCPXML timeline …")
    fcpxml_path = build_fcpxml(session_name, take_num, clips, output_dir=session_dir)

    if fcpxml_path:
        open_in_resolve(fcpxml_path, session_name=session_name, media_dir=MEDIA_DIR)
        if obs_path:
            mark_obs_consumed(MEDIA_DIR, obs_path)
        _set_pipeline("done", f"Take {take_num:02d} ready — opening in Resolve ✓")
    else:
        _set_pipeline("error", "FCPXML generation failed — see terminal for details")


# ── iPhone web UI ────────────────────────────────────────────────────────────

_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Lumix S5</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: #0d0d0d;
    color: #fff;
    font-family: -apple-system, 'Helvetica Neue', sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    -webkit-tap-highlight-color: transparent;
    user-select: none;
    gap: 0;
  }}

  /* ── ARM button ── */
  #arm-btn {{
    border: 1.5px solid #2a2a2a;
    background: transparent;
    color: #333;
    font-family: inherit;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 3px;
    text-transform: uppercase;
    padding: 10px 28px;
    border-radius: 100px;
    cursor: pointer;
    margin-bottom: 14px;
    transition: color .25s, border-color .25s, box-shadow .25s;
    -webkit-appearance: none;
  }}
  #arm-btn:active {{ opacity: 0.6; }}
  #arm-btn.armed {{
    color: #ff9500;
    border-color: #ff9500;
    box-shadow: 0 0 18px rgba(255,149,0,0.25);
  }}

  #arm-label {{
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #252525;
    margin-bottom: 48px;
    height: 14px;
    transition: color .25s;
  }}
  #arm-label.armed {{
    color: #7a4700;
  }}

  /* ── SD card import button ── */
  #sdcard-btn {{
    border: 1.5px solid #2a2a2a;
    background: transparent;
    color: #333;
    font-family: inherit;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
    text-transform: uppercase;
    padding: 9px 22px;
    border-radius: 100px;
    cursor: pointer;
    margin-top: 32px;
    transition: color .25s, border-color .25s, box-shadow .25s;
    -webkit-appearance: none;
  }}
  #sdcard-btn:active {{ opacity: 0.6; }}
  #sdcard-btn.busy {{
    color: #0a84ff;
    border-color: #0a84ff;
    box-shadow: 0 0 18px rgba(10,132,255,0.25);
  }}
  #sdcard-btn:disabled {{
    opacity: 0.4;
    cursor: default;
  }}

  /* ── Toggle checkboxes (download, sdcard only-latest, ...) ── */
  .toggle-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 18px;
  }}
  .toggle-checkbox {{
    width: 15px;
    height: 15px;
    accent-color: #0a84ff;
    cursor: pointer;
  }}
  .toggle-label {{
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: #444;
    cursor: pointer;
    user-select: none;
  }}

  /* ── Status ── */
  #status-text {{
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #444;
    height: 16px;
    margin-bottom: 48px;
    transition: color .3s;
  }}
  #status-text.rec {{
    color: #ff3b30;
    animation: blink 1s ease-in-out infinite;
  }}
  @keyframes blink {{ 0%, 100% {{ opacity: 1 }} 50% {{ opacity: 0.25 }} }}

  /* ── Record button ── */
  #btn-wrap {{
    width: 200px;
    height: 200px;
    border-radius: 50%;
    border: 3px solid #1a1a1a;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: border-color .3s, box-shadow .3s;
  }}
  #btn-wrap.rec {{
    border-color: rgba(255,59,48,0.3);
    box-shadow: 0 0 60px rgba(255,59,48,0.12);
  }}

  #btn {{
    width: 164px;
    height: 164px;
    border-radius: 50%;
    border: none;
    cursor: pointer;
    background: #ff3b30;
    position: relative;
    transition: transform .12s, background .3s;
    -webkit-appearance: none;
  }}
  #btn:active {{ transform: scale(0.91); }}
  #btn.rec {{ background: #1c1c1e; }}

  #btn::after {{
    content: '';
    display: block;
    width: 58px;
    height: 58px;
    background: #fff;
    border-radius: 50%;
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    transition: border-radius .25s, width .25s, height .25s, background .3s;
  }}
  #btn.rec::after {{
    border-radius: 10px;
    width: 52px;
    height: 52px;
    background: #ff3b30;
  }}

  #label {{
    margin-top: 44px;
    font-size: 12px;
    color: #2a2a2a;
    letter-spacing: 2px;
    text-transform: uppercase;
    transition: color .3s;
    height: 16px;
  }}
  #label.rec {{ color: #555; }}

  /* ── Session input ── */
  #session-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 28px;
  }}
  #session-input {{
    background: #1a1a1a;
    border: 1.5px solid #2a2a2a;
    color: #ccc;
    font-family: inherit;
    font-size: 13px;
    letter-spacing: 1px;
    padding: 8px 14px;
    border-radius: 100px;
    outline: none;
    width: 180px;
    text-align: center;
    -webkit-appearance: none;
  }}
  #session-input:focus {{ border-color: #555; color: #fff; }}
  #session-save {{
    background: #1a1a1a;
    border: 1.5px solid #2a2a2a;
    color: #555;
    font-family: inherit;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
    padding: 8px 16px;
    border-radius: 100px;
    cursor: pointer;
    -webkit-appearance: none;
  }}
  #session-save:active {{ opacity: 0.6; }}
  #take-counter {{
    font-size: 10px;
    letter-spacing: 2px;
    color: #2a2a2a;
    margin-bottom: 10px;
    height: 14px;
    transition: color .3s;
  }}
  #take-counter.active {{ color: #444; }}

  /* ── Pipeline status ── */
  #pipeline-wrap {{
    margin-top: 36px;
    min-height: 52px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    width: 260px;
  }}
  #pipeline-bar-bg {{
    width: 100%;
    height: 2px;
    background: #1a1a1a;
    border-radius: 2px;
    overflow: hidden;
    display: none;
  }}
  #pipeline-bar {{
    height: 100%;
    background: #ff9500;
    border-radius: 2px;
    width: 0%;
    transition: width .5s;
  }}
  #pipeline-msg {{
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #333;
    text-align: center;
    line-height: 1.6;
    transition: color .3s;
  }}
  #pipeline-msg.active {{ color: #777; }}
  #pipeline-msg.done   {{ color: #30d158; }}
  #pipeline-msg.error  {{ color: #ff3b30; }}

  #cam-addr {{
    position: fixed;
    bottom: 28px;
    font-size: 10px;
    color: #1e1e1e;
    letter-spacing: 1px;
  }}

  #cam-status {{
    position: fixed;
    bottom: 44px;
    font-size: 10px;
    letter-spacing: 1px;
    color: #444;
  }}
  #cam-status.warn {{ color: #ff9500; }}
</style>
</head>
<body>

<!-- Session name input -->
<div id="session-wrap">
  <input id="session-input" type="text" placeholder="Session Name" maxlength="40"
         onkeydown="if(event.key==='Enter'){{ saveSession(); this.blur(); }}">
  <button id="session-save" onclick="saveSession()">SET</button>
</div>
<div id="take-counter">TAKE —</div>

<button id="arm-btn" onclick="toggleArm()">ARM</button>
<div id="arm-label">CUBASE SYNC OFF</div>

<div id="status-text">READY</div>
<div id="btn-wrap"><button id="btn" onclick="toggleRec()"></button></div>
<div id="label">TAP TO RECORD</div>

<!-- Post-production pipeline status -->
<div id="pipeline-wrap">
  <div id="pipeline-bar-bg"><div id="pipeline-bar"></div></div>
  <div id="pipeline-msg"></div>
</div>

<button id="sdcard-btn" onclick="importSdCard()">IMPORT SD CARD</button>

<div class="toggle-wrap">
  <input type="checkbox" class="toggle-checkbox" id="download-toggle" onchange="toggleDownload()">
  <label class="toggle-label" for="download-toggle">Download from S5 over WiFi</label>
</div>

<div class="toggle-wrap">
  <input type="checkbox" class="toggle-checkbox" id="sdcard-only-latest-toggle" checked onchange="toggleSdcardOnlyLatest()">
  <label class="toggle-label" for="sdcard-only-latest-toggle">Only latest take from SD card</label>
</div>

<div id="cam-status"></div>
<div id="cam-addr">CAM {CAMERA_IP}</div>

<script>
let rec   = false;
let armed = false;
let pipelineState = 'idle';

// ── Pipeline status steps → progress bar width ──────────────────────────────
const PIPELINE_STEPS = {{ idle:0, downloading:25, decoding:60, resolve:85, done:100, error:100 }};

function applyArmed(a) {{
  armed = a;
  const ab  = document.getElementById('arm-btn');
  const alb = document.getElementById('arm-label');
  if (a) {{
    ab.classList.add('armed');
    alb.textContent = 'CUBASE SYNC ON';
    alb.classList.add('armed');
  }} else {{
    ab.classList.remove('armed');
    alb.textContent = 'CUBASE SYNC OFF';
    alb.classList.remove('armed');
  }}
}}

function applyRec(r) {{
  rec = r;
  const btn  = document.getElementById('btn');
  const wrap = document.getElementById('btn-wrap');
  const st   = document.getElementById('status-text');
  const lbl  = document.getElementById('label');
  if (r) {{
    btn.classList.add('rec');
    wrap.classList.add('rec');
    st.textContent = '● REC';
    st.classList.add('rec');
    lbl.textContent = 'TAP TO STOP';
    lbl.classList.add('rec');
  }} else {{
    btn.classList.remove('rec');
    wrap.classList.remove('rec');
    st.textContent = 'READY';
    st.classList.remove('rec');
    lbl.textContent = 'TAP TO RECORD';
    lbl.classList.remove('rec');
  }}
}}

function applyPipeline(p) {{
  if (!p) return;
  pipelineState = p.state;
  const msg  = document.getElementById('pipeline-msg');
  const bar  = document.getElementById('pipeline-bar');
  const barBg = document.getElementById('pipeline-bar-bg');

  msg.className = 'pipeline-msg';   // reset

  if (p.state === 'idle') {{
    barBg.style.display = 'none';
    msg.textContent = '';
    return;
  }}

  barBg.style.display = 'block';
  bar.style.width = (PIPELINE_STEPS[p.state] || 0) + '%';
  msg.textContent = p.message || p.state;

  if (p.state === 'done')       {{ msg.classList.add('done'); }}
  else if (p.state === 'error') {{ msg.classList.add('error'); }}
  else                          {{ msg.classList.add('active'); }}
}}

function applySession(session, take) {{
  const tc = document.getElementById('take-counter');
  if (session && take > 0) {{
    tc.textContent = session.toUpperCase() + ' — TAKE ' + take;
    tc.classList.add('active');
  }} else if (take > 0) {{
    tc.textContent = 'TAKE ' + take;
    tc.classList.add('active');
  }} else {{
    tc.textContent = 'TAKE —';
    tc.classList.remove('active');
  }}
}}

async function saveSession() {{
  const inp = document.getElementById('session-input');
  const name = inp.value.trim();
  if (!name) return;
  try {{
    await fetch('/session', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ name }})
    }});
  }} catch(e) {{ console.error(e); }}
}}

async function toggleArm() {{
  try {{
    const r = await fetch('/arm/toggle', {{ method: 'POST' }});
    const d = await r.json();
    applyArmed(d.armed);
    applyRec(d.recording);
  }} catch(e) {{ console.error(e); }}
}}

async function toggleRec() {{
  try {{
    const r = await fetch(rec ? '/record/stop' : '/record/start', {{ method: 'POST' }});
    const d = await r.json();
    applyRec(d.recording);
    applyArmed(d.armed);
    if (!d.ok && d.message) {{ alert(d.message); }}
  }} catch(e) {{ console.error(e); }}
}}

async function importSdCard() {{
  try {{
    const r = await fetch('/sdcard/import', {{ method: 'POST' }});
    if (r.status === 409) {{ return; }}  // pipeline already busy
  }} catch(e) {{ console.error(e); }}
}}

function applySdcardBusy(busy) {{
  const btn = document.getElementById('sdcard-btn');
  btn.classList.toggle('busy', busy);
  btn.disabled = busy;
}}

function applyDownloadEnabled(enabled) {{
  document.getElementById('download-toggle').checked = enabled;
}}

async function toggleDownload() {{
  try {{
    const r = await fetch('/download/toggle', {{ method: 'POST' }});
    const d = await r.json();
    applyDownloadEnabled(d.download_enabled);
  }} catch(e) {{ console.error(e); }}
}}

function applySdcardOnlyLatest(enabled) {{
  document.getElementById('sdcard-only-latest-toggle').checked = enabled;
}}

async function toggleSdcardOnlyLatest() {{
  try {{
    const r = await fetch('/sdcard/only_latest/toggle', {{ method: 'POST' }});
    const d = await r.json();
    applySdcardOnlyLatest(d.sdcard_only_latest);
  }} catch(e) {{ console.error(e); }}
}}

function applyCameraStatus(reachable, battery, videoRemainMin) {{
  const el = document.getElementById('cam-status');
  if (!reachable) {{
    el.textContent = 'CAMERA UNREACHABLE';
    el.classList.add('warn');
    return;
  }}
  const parts = [];
  let low = false;
  if (battery) {{
    parts.push('BATT ' + battery);
    const bits = battery.split('/');
    if (bits.length === 2 && parseInt(bits[0], 10) <= 1) low = true;
  }}
  if (typeof videoRemainMin === 'number' && videoRemainMin >= 0) {{
    parts.push(videoRemainMin + ' MIN LEFT');
    if (videoRemainMin <= 5) low = true;
  }}
  el.textContent = parts.join(' · ');
  el.classList.toggle('warn', low);
}}

async function poll() {{
  try {{
    const r = await fetch('/status');
    const d = await r.json();
    applyRec(d.recording);
    applyArmed(d.armed);
    applySession(d.session, d.take);
    applyPipeline(d.pipeline);
    applySdcardBusy(d.pipeline && !['idle', 'done', 'error'].includes(d.pipeline.state));
    applyDownloadEnabled(d.download_enabled);
    applySdcardOnlyLatest(d.sdcard_only_latest);
    applyCameraStatus(d.camera_reachable, d.camera_battery, d.camera_video_remaining_min);

    // Keep session input in sync if server has a name set
    const inp = document.getElementById('session-input');
    if (!inp.value && d.session && d.session !== 'Session') {{
      inp.value = d.session;
    }}
  }} catch(e) {{}}
}}

// Poll faster (every second) while pipeline is running, normal otherwise
function schedulePoll() {{
  const interval = (pipelineState !== 'idle' && pipelineState !== 'done' && pipelineState !== 'error')
    ? 800 : 1500;
  setTimeout(async () => {{ await poll(); schedulePoll(); }}, interval);
}}
poll().then(schedulePoll);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return _HTML


@app.route("/record/start", methods=["POST"])
def api_start():
    # Called from iPhone — also triggers Cubase playback
    ok, reason = record_start(trigger_cubase=True)
    return jsonify({"ok": ok, "recording": _recording, "armed": _armed, "message": reason})


@app.route("/record/stop", methods=["POST"])
def api_stop():
    # Called from iPhone — also stops Cubase
    ok = record_stop(trigger_cubase=True)
    return jsonify({"ok": ok, "recording": _recording, "armed": _armed})


@app.route("/cubase/started", methods=["POST"])
def api_cubase_started():
    """Called by the Cubase MIDI Remote script when transport starts."""
    global _cubase_playing
    _cubase_playing = True
    print("[cubase] transport started")
    if _armed:
        ok, reason = record_start(trigger_cubase=False)  # camera only, no feedback loop
        if not ok:
            print(f"[cubase] ✗ Record start aborted: {reason}")
    return jsonify({"ok": True, "recording": _recording, "armed": _armed})


@app.route("/cubase/stopped", methods=["POST"])
def api_cubase_stopped():
    """Called by the Cubase MIDI Remote script when transport stops."""
    global _cubase_playing
    _cubase_playing = False
    print("[cubase] transport stopped")
    if _armed:
        record_stop(trigger_cubase=False)   # camera only, no feedback loop
    return jsonify({"ok": True, "recording": _recording, "armed": _armed})


@app.route("/arm/toggle", methods=["POST"])
def api_arm_toggle():
    global _armed
    with _lock:
        _armed = not _armed
        state  = _armed
    print(f"[arm] {'ARMED — Cubase will trigger camera' if state else 'DISARMED'}")
    return jsonify({"armed": state, "recording": _recording})


@app.route("/download/toggle", methods=["POST"])
def api_download_toggle():
    global _download_from_camera
    with _lock:
        _download_from_camera = not _download_from_camera
        state = _download_from_camera
    print(f"[download] {'ENABLED — will download S5 footage over WiFi' if state else 'DISABLED — use SD card import instead'}")
    return jsonify({"download_enabled": state})


@app.route("/sdcard/only_latest/toggle", methods=["POST"])
def api_sdcard_only_latest_toggle():
    global _sdcard_only_latest
    with _lock:
        _sdcard_only_latest = not _sdcard_only_latest
        state = _sdcard_only_latest
    print(f"[sdcard] only_latest {'ENABLED — importing just the most recent new clip' if state else 'DISABLED — importing every new clip on the card'}")
    return jsonify({"sdcard_only_latest": state})


@app.route("/session", methods=["GET", "POST"])
def api_session():
    global _session_name, _take_number
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if name and name != _session_name:
            _session_name = name
            _take_number  = _next_take_number(name)
            _save_session_name(name)
            print(f"[session] name set to '{_session_name}' (take {_take_number})")
    return jsonify({"session": _session_name, "take": _take_number})


@app.route("/sdcard/import", methods=["POST"])
def api_sdcard_import():
    """
    Manual fallback for when the WiFi transfer is too slow: scan a plugged-in
    SD card for clips not yet copied, pair each with the closest not-yet-used
    OBS recording by creation timestamp, and run them through the normal
    post-production pipeline.
    """
    if _pipeline["state"] not in ("idle", "done", "error"):
        return jsonify({"ok": False, "message": "Pipeline already busy"}), 409

    def _do_import():
        from resolve_helper import import_from_sdcard

        global _take_number
        with _lock:
            take_start    = _take_number
            session_snap  = _session_name
            only_latest   = _sdcard_only_latest

        _set_pipeline("downloading", "Scanning SD card …")

        def _progress(take_num, filename):
            _set_pipeline("resolve", f"Importing {filename} (Take {take_num:02d}) …")

        result = import_from_sdcard(
            session_snap, take_start, MEDIA_DIR, OBS_RECORDING_DIR,
            progress_cb=_progress, only_latest=only_latest,
        )

        # Both successful AND LTC-failed takes consumed a take number in the
        # loop (copied a file into "Take NN"), so both must advance the
        # counter or the next real take would collide with a failed one's folder.
        takes_processed = result["imported"] + result["ltc_failed"]
        if takes_processed:
            with _lock:
                _take_number = take_start + takes_processed

        if not result["ok"]:
            _set_pipeline("error", result.get("message", "SD card import failed"))
        elif result["imported"] == 0 and result["ltc_failed"] == 0 and result["skipped"] == 0:
            _set_pipeline("idle", result.get("message", "No new clips found on SD card"))
        elif result["imported"] == 0 and result["ltc_failed"] == 0:
            # Clips existed but every one was skipped — a completely different
            # situation from "nothing new on the card" (import_from_sdcard's
            # own return dict has no "message" key in this branch, so the old
            # code silently fell back to the misleading "no new clips" text).
            from resolve_helper import SD_IMPORT_MAX_TIME_DELTA
            _set_pipeline(
                "error",
                f"{result['skipped']} new clip(s) found on SD card, but none had a matching "
                f"OBS recording within {SD_IMPORT_MAX_TIME_DELTA}s — check that OBS was running "
                f"for these takes, or that its clips weren't already used by another import",
            )
        elif result["ltc_failed"]:
            failed_takes = ", ".join(f"Take {f['take']:02d}" for f in result["failures"])
            msg = f"LTC decode failed on both clips for {failed_takes} — check the LTC feed/cable"
            if result["imported"]:
                msg = f"Imported {result['imported']} take(s), but {msg}"
            _set_pipeline("error", msg)
        else:
            msg = f"Imported {result['imported']} take(s) from SD card"
            if result["skipped"]:
                msg += f", skipped {result['skipped']} (no OBS match)"
            _set_pipeline("done", msg)

    threading.Thread(target=_do_import, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/pipeline/status")
def api_pipeline_status():
    return jsonify(_pipeline)


@app.route("/status")
def api_status():
    return jsonify({
        "recording":        _recording,
        "armed":            _armed,
        "cubase_playing":   _cubase_playing,
        "camera_ip":        CAMERA_IP,
        "session":          _session_name,
        "take":             _take_number,
        "pipeline":          _pipeline,
        "download_enabled":  _download_from_camera,
        "sdcard_only_latest": _sdcard_only_latest,
        "camera_battery":             _camera_state["battery"],
        "camera_video_remaining_min": _camera_state["video_remaining_min"],
        "camera_reachable":           _camera_state["reachable"],
    })


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "localhost"

    print()
    print("─" * 54)
    print("  Lumix S5 Controller")
    print("─" * 54)
    print(f"  Camera IP  :  {CAMERA_IP}")
    print(f"  iPhone URL :  http://{local_ip}:{PORT}")
    print(f"  (Make sure Mac, iPhone, and camera are on the same WiFi)")
    print("─" * 54)
    print()

    _setup_virtual_midi_out()   # create 'Lumix Controller' MIDI port for Cubase
    _setup_obs()                # connect to OBS WebSocket
    install_cubase_script()     # copy lumix_transport.js into Cubase's Scripts folder(s)
    threading.Thread(target=_poll_camera_state, daemon=True).start()

    from resolve_helper import install_resolve_scripts
    install_resolve_scripts(MEDIA_DIR)  # Workspace → Scripts → Utility, for Resolve Free

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
