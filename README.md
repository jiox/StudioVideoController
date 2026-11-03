# Lumix S5 Controller

A Python-based recording controller that synchronises a **Panasonic Lumix S5**, **OBS**, and **Cubase** — then automatically builds a timecode-synced multicam project in **DaVinci Resolve** after every take.

---

## What it does

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Mac (this machine)                         │
│                                                                     │
│   ┌──────────┐   HTTP callbacks   ┌────────────────────────────┐   │
│   │  Cubase  │ ─────────────────► │                            │   │
│   └──────────┘  (started/stopped) │      server.py             │   │
│                                   │      (Flask :5050)         │   │
│   ┌──────────┐   WebSocket API    │                            │   │
│   │   OBS    │ ◄────────────────► │  • ARM gate                │   │
│   └──────────┘                    │  • Session / take tracking │   │
│                                   │  • Post-production pipeline│   │
│   ┌──────────┐   virtual MIDI     └────────────┬───────────────┘   │
│   │  Cubase  │ ◄──────────────────────────────┘                    │
│   └──────────┘  (Record/Stop notes, Motion Control CC)              │
└────────────────────────────────────┬────────────────────────────────┘
                                     │ WiFi HTTP API
                          ┌──────────▼──────────┐
                          │   Lumix S5 Camera   │
                          │   192.168.x.x:80    │
                          └─────────────────────┘

  iPhone  ──── Safari ────► https://<mac-ip>:5050   (video recording UI at `/`, audio/Cubase UI with motion control at `/audio`)
```

### Recording flow

1. Press **▶** in Cubase (or tap the iPhone button)
2. Camera starts recording via WiFi HTTP command
3. OBS starts recording via WebSocket
4. Press **■** in Cubase (or tap the iPhone button again)
5. Camera stops; OBS stops
6. Pipeline runs automatically in the background:
   - Downloads the S5 clip over WiFi
   - Decodes LTC timecode from the audio track of each clip
   - Generates an FCPXML multicam timeline aligned by LTC
   - Opens it in DaVinci Resolve
7. iPhone shows live progress; DaVinci Resolve opens to the finished multicam

---

## Architecture

### Components

| File | Role |
|------|------|
| `server.py` | Central Flask server. Camera control, OBS control, ARM gate, session tracking, pipeline orchestration, iPhone UIs (video page `/`, audio page `/audio`), Motion Control CC pipeline |
| `lumix_transport.js` | Cubase MIDI Remote script. Triggers Record/Stop via a virtual MIDI port, and hosts three CC-bound Quick Control knobs for iPhone Motion Control |
| `resolve_helper.py` | Post-production pipeline. LTC decode (ffmpeg + ltcdump), S5 clip download, FCPXML generation |
| `requirements.txt` | Python dependencies |

### Key design decisions

**Virtual MIDI port for Cubase transport control.** `require('http')` and `require('child_process')` both throw inside the Cubase MIDI Remote script sandbox, so HTTP isn't viable from that side. Instead, `server.py` opens a virtual MIDI port named `"Lumix Controller"` (via `python-rtmidi`) at startup; `lumix_transport.js` binds Note 20/21 to Record/Stop directly via `mMidiBinding` + `makeValueBinding` — no IAC Driver, no Generic Remote mapping, just install the script once. The same port now also carries three MIDI CC channels (21/22/23) for Motion Control, bound to Cubase Quick Controls.

**ARM gate.** The ARM toggle decouples "Cubase is playing" from "record the camera". You can freely scrub and play back in Cubase without triggering the camera. Only when ARM is enabled does Cubase transport control the camera.

**FCPXML instead of DaVinci Resolve Python API.** The Python scripting API requires DaVinci Resolve Studio (paid). This system generates a standards-compliant FCPXML 1.9 file that DaVinci Resolve Free can import directly. The multicam clip is built with per-angle LTC offsets so sync is frame-accurate without any manual alignment.

**LTC timecode as the sync anchor.** Both the S5 and OBS record an LTC audio track. After every take, `ffmpeg` extracts the LTC channel, `ltcdump` reads the first valid timecode frame, and that value is used to position each clip in the FCPXML multicam. This is robust to network jitter, recording start delays, and any drift between trigger times.

---

## Requirements

### Hardware
- Panasonic Lumix S5 (firmware with WiFi HTTP API)
- Mac (M1/M2/Intel)
- iPhone on the same WiFi network

### Software
- macOS 12+
- Python 3.10+
- Cubase 12 or 13
- OBS (any recent version with WebSocket server support)
- DaVinci Resolve 18+ (free version is fine)
- Homebrew CLI tools:

```bash
brew install ffmpeg ltc-tools
```

### Python packages

```bash
pip3 install flask requests obsws-python python-rtmidi python-dotenv flask-sock --break-system-packages
```

Or from the project directory:

```bash
pip3 install -r requirements.txt --break-system-packages
```

---

## One-time setup

### 1. Connect the S5 to your home WiFi

1. On the camera: **MENU → Setup (wrench) → Wi-Fi → Wi-Fi Function → Via Network**
2. Connect to your home router (same network as Mac and iPhone)
3. Find the IP: **MENU → Setup → Wi-Fi → Wi-Fi Function → Check Wi-Fi connection status**

> **Tip:** Set a DHCP reservation on your router so the camera always gets the same IP.

The camera connects to your home WiFi automatically on power-up once configured. Make sure the camera is in **Creative Video mode** (the cine camera icon on the mode dial) — this is required for the `video_recstart` command to work.

### 2. Set the camera IP

Copy `.env.example` to `.env` and fill in your camera's IP from step 1:

```bash
cp .env.example .env
```

```
CAMERA_IP=192.168.1.100   # ← your camera's IP from step 1
PORT=5050                 # web server port (change if already in use)
OBS_HOST=localhost
OBS_PORT=4455
OBS_PASSWORD=             # set if you configured a password in OBS
MEDIA_DIR=~/Movies/LumixSessions   # where S5 clips are saved
SSL_CERT_FILE=            # only needed for Motion Control — see step 5
SSL_KEY_FILE=
MOTION_MAX_DEG=45         # ± tilt range mapped to MIDI CC 0-127
```

`.env` is gitignored, so machine-specific values never end up in source control.

### 3. Enable OBS WebSocket

In OBS: **Tools → WebSocket Server Settings → Enable WebSocket server**

Leave the port as 4455 and the password blank unless you changed them (update `.env` if you did).

### 4. Install the Cubase MIDI Remote script (one-time)

Copy `lumix_transport.js` to:

```
~/Documents/Cubase MIDI Remote/Driver Scripts/Local/
```

In Cubase, open the **MIDI Remote Manager** (bottom zone → MIDI Remote tab) and click the rescan button. The script activates automatically. That's the entire setup — no IAC Driver, no Generic Remote mapping, no start-order dependency between `server.py` and Cubase:

- Cubase → server: the script detects transport play/stop and POSTs to the server (`/cubase/started`/`/cubase/stopped`).
- Server → Cubase: `server.py` opens a virtual MIDI port (`"Lumix Controller"`) and sends Record/Stop as MIDI notes, which the script binds directly to Cubase's transport. The same port also carries the Motion Control CC data (see step 5).

### 5. Enable HTTPS for Motion Control (optional)

Skip this if you don't need the iPhone-tilt-to-MIDI-CC feature — everything else works over plain HTTP. iOS Safari only exposes the motion-sensor permission API on a secure connection, so Motion Control needs HTTPS:

```bash
brew install mkcert
mkcert -install
mkdir certs
mkcert -key-file certs/lumix-key.pem -cert-file certs/lumix.pem <mac-lan-ip> localhost
```

Set `SSL_CERT_FILE=certs/lumix.pem` and `SSL_KEY_FILE=certs/lumix-key.pem` in `.env`. Then AirDrop `$(mkcert -CAROOT)/rootCA.pem` to the iPhone, install the profile (**Settings → General → VPN & Device Management**), and enable full trust for it (**Settings → General → About → Certificate Trust Settings**) — full trust is an easy-to-miss extra step. `certs/` is gitignored. See [SETUP.md](SETUP.md) for the full walkthrough.

---

## Running

```bash
cd ~/Documents/obsidian/Studio/Studio/LumixController
python3 server.py
```

You'll see:

```
──────────────────────────────────────────────────────
  Lumix S5 Controller
──────────────────────────────────────────────────────
  Camera IP  :  192.168.1.100
  iPhone URL :  https://192.168.1.10:5050
  HTTPS      :  ON  (cert: certs/lumix.pem)
  (Make sure Mac, iPhone, and camera are on the same WiFi)
──────────────────────────────────────────────────────

[obs]  ✓ Connected to OBS WebSocket at localhost:4455
```
(`HTTPS: OFF` and an `http://` URL if you skipped step 5 — everything except Motion Control still works.)

Open the iPhone URL in Safari. Optional: **Share → Add to Home Screen** for a standalone icon.

### Auto-start on login (optional)

```bash
cat > ~/Library/LaunchAgents/com.lumix.controller.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.lumix.controller</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/yourname/path/to/LumixController/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/lumix_controller.log</string>
  <key>StandardErrorPath</key><string>/tmp/lumix_controller.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.lumix.controller.plist
```

Check logs with: `tail -f /tmp/lumix_controller.log`

---

## Day-to-day workflow

### Before your session

1. Start `server.py` (or confirm it's running via launchd) — it creates the `Lumix Controller` MIDI port that Cubase connects to
2. Open Cubase and OBS

### Per-take

| Step | What you do | What happens |
|------|-------------|--------------|
| Name the session | Type in the session name field on the iPhone, tap **SET** | Server remembers the name; clips go into `~/Movies/LumixSessions/<name>/` |
| Arm sync | Tap **ARM** on the iPhone (glows orange) | Cubase transport now controls the camera |
| Record | Press **▶** in Cubase | Camera + OBS start simultaneously |
| Stop | Press **■** in Cubase | Camera + OBS stop; pipeline begins |
| Wait | iPhone shows progress bar | S5 clip downloads, LTC decoded, FCPXML built |
| Edit | DaVinci Resolve opens automatically | Multicam clip is ready — LTC-synced, no manual alignment needed |

### Manual recording (without Cubase)

Tap the record button on the iPhone at any time. ARM is not required for manual use. The iPhone button also starts/stops Cubase transport via MIDI notes sent to the virtual `Lumix Controller` port (bound to Cubase's transport in `lumix_transport.js`).

### ARM behaviour

| ARM state | Cubase ▶ | Cubase ■ | iPhone button |
|-----------|----------|----------|---------------|
| Off       | nothing  | nothing  | records + triggers Cubase |
| On        | records  | stops    | records + triggers Cubase |

---

## iPhone UI

```
┌─────────────────────────────────┐
│                                 │
│  [ Session Name ]   [ SET ]     │  ← type before recording
│                                 │
│  SONG A — TAKE 03               │  ← updates after each take
│                                 │
│         [ ARM ]                 │  ← orange = Cubase sync on
│       CUBASE SYNC ON            │
│                                 │
│           ● REC                 │  ← blinks red while recording
│                                 │
│         (  ■  )                 │  ← big button, tap to toggle
│                                 │
│       TAP TO STOP               │
│                                 │
│  ████████████░░░░  DECODING…    │  ← pipeline progress after stop
│                                 │
│       CAM 192.168.1.100       │
└─────────────────────────────────┘
```

The page polls the server every 1.5 seconds (every 0.8 seconds while pipeline is running) so state stays in sync across devices.

The collapsible **MOTION CONTROL** panel (Enable / Calibrate / sensitivity slider / live α/β/γ readout) lives on the separate Audio page (`/audio`), not here — see [Motion Control](#motion-control) below. Unlike the rest of the UI, motion data is pushed over its own WebSocket, not the `/status` poll loop.

---

## Post-production pipeline

After every take, a background thread runs the following sequence:

### 1. OBS stop

`obsws-python` calls `stop_record()` and captures the output file path returned by OBS.

### 2. S5 clip download

The server browses the camera's UPnP/DLNA `ContentDirectory` service (SOAP `Browse` on `http://<camera-ip>:60606/Server0/CDS_control`), picks the newest video item, and downloads it from the direct URL given in that item's `<res>` entry, e.g.:

```
http://<camera-ip>:50001/DO11015278.MP4
```

Downloaded to: `~/Movies/LumixSessions/<session>/Take XX/`

### 3. LTC decode

For each clip (OBS + S5), `resolve_helper.decode_ltc()` runs:

```
ffmpeg -i clip.mp4 -map 0:a:0 -af "pan=mono|c0=c0" -ar 48000 -t 10 tmp.wav
ltcdump -f 30 tmp.wav
```

The first valid timecode line (e.g. `1:00:10:00`) is parsed and stored. The temp WAV is deleted immediately after.

> **Channel:** LTC is assumed to be on audio channel 1. Edit the `channel` parameter in `decode_ltc()` in `resolve_helper.py` if yours is on a different channel.

### 4. FCPXML generation

`build_fcpxml()` writes a multicam FCPXML 1.9 file:

- **`<format>`** — 1920×1080, 1/30s frame duration
- **`<asset>`** per clip — absolute file URI, LTC start timecode, duration from ffprobe
- **`<multicam>`** — one `<mc-angle>` per clip; each angle's clip is offset by `(its_LTC − earliest_LTC)` frames so all angles are aligned to the same moment in real time
- **`<sequence>`** — a timeline containing a single `<mc-clip>` spanning the full duration; angle 1 is set to `srcEnable="all"` (video + audio active), subsequent angles to `srcEnable="audio"` by default

The file is saved alongside the clips:
```
~/Movies/LumixSessions/<session>/Take XX/<session> — Take XX.fcpxml
```

### 5. Open in Resolve

```bash
open -a "DaVinci Resolve" "path/to/file.fcpxml"
```

DaVinci Resolve imports the FCPXML as a new timeline. The multicam clip appears in the Media Pool, already synced. You can also re-import the file manually at any time via **File → Import → Timeline**.

---

## Motion Control

Turns the iPhone into a wireless 3-axis MIDI controller: tilting/rotating the phone streams live orientation data to Cubase, mapped to MIDI CC on three axes.

- **Requires HTTPS** ([one-time setup](#5-enable-https-for-motion-control-optional)) — iOS Safari only exposes the `DeviceOrientationEvent` sensor API (and its permission prompt) on a secure connection.
- The browser reads `alpha`/`beta`/`gamma` (yaw/pitch/roll) from the `deviceorientation` event, computes deltas from a user-set calibration zero-point, throttles to ~20 Hz, and streams them over a WebSocket at `wss://<mac-ip>:5050/motion/ws`.
- `server.py` maps each axis's calibrated degrees (clamped to a configurable ± range, default 45°, adjustable live via the UI's sensitivity slider or `MOTION_MAX_DEG` in `.env`) linearly to 0–127 and sends it as MIDI CC on the same virtual `"Lumix Controller"` port used for transport control:

  | Axis | Meaning | MIDI CC (channel 1) |
  |------|---------|----------------------|
  | γ (gamma) | roll — left/right tilt | 21 |
  | β (beta)  | pitch — front/back tilt | 22 |
  | α (alpha) | yaw — rotation around vertical axis | 23 |

- `lumix_transport.js` binds each CC to a knob wired to Cubase's **Quick Controls** (slots 0–2), so you assign whatever channel/plugin parameter you want directly inside Cubase — no server-side reconfiguration needed.
- The WebSocket reconnects automatically (~2s backoff) if the connection drops (WiFi hiccup, phone screen lock); the iOS permission grant itself doesn't need to be re-requested until the page is reloaded.

---

## HTTP API

All endpoints accept and return JSON. Called by the iPhone UI and by `lumix_transport.js` in Cubase.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/record/start` | Start recording (camera + OBS + Cubase play) |
| `POST` | `/record/stop` | Stop recording (camera + OBS + Cubase stop); triggers pipeline |
| `POST` | `/arm/toggle` | Toggle ARM state |
| `POST` | `/cubase/started` | Called by `lumix_transport.js` when Cubase transport starts |
| `POST` | `/cubase/stopped` | Called by `lumix_transport.js` when Cubase transport stops |
| `GET`  | `/session` | Get current session name and take number |
| `POST` | `/session` | Set session name — body: `{"name": "Song A"}` |
| `GET`  | `/pipeline/status` | Get pipeline state — see states below |
| `GET`  | `/status` | Full server state (recording, armed, session, take, pipeline) |
| `GET`  | `/` | iPhone web UI (HTML) |

Motion Control uses a separate **WebSocket** (not plain HTTP JSON): `wss://<mac-ip>:5050/motion/ws` — each text message is `{"gamma_deg": <float>, "beta_deg": <float>, "alpha_deg": <float>, "max_deg": <float, optional>}`, streamed continuously rather than request/response. See [Motion Control](#motion-control).

### Pipeline states

| State | Meaning |
|-------|---------|
| `idle` | No pipeline running |
| `downloading` | Downloading S5 clip over WiFi |
| `decoding` | Running ffmpeg + ltcdump on both clips |
| `resolve` | Generating FCPXML file |
| `done` | FCPXML opened in DaVinci Resolve |
| `error` | Something failed — check terminal output |

---

## File structure

```
LumixController/
├── server.py              # Main Flask server
├── lumix_transport.js     # Cubase MIDI Remote script
├── resolve_helper.py      # LTC decode + download + FCPXML generation
├── requirements.txt       # Python dependencies
├── SETUP.md               # Quick-start checklist
└── README.md              # This file

~/Movies/LumixSessions/    # Where recordings land (set via MEDIA_DIR)
└── Song A/
    └── Take 01/
        ├── S5001234.MP4                  # S5 footage (downloaded)
        ├── 2024-01-01 12-00-00.mkv      # OBS recording
        └── Song A — Take 01.fcpxml      # DaVinci Resolve multicam
```

---

## Troubleshooting

### Camera

**"Cannot reach camera"**
- Confirm `CAMERA_IP` in `.env` matches the camera (check MENU → Setup → Wi-Fi → connection status)
- Camera must be in **Creative Video mode**
- The WiFi icon must be visible in the camera status bar

**Camera doesn't start recording**
- Pair the camera with the server on first connection — a dialog appears on the camera screen asking to allow the connection. Accept it once; it's remembered.
- Check the terminal for `<result>ok</result>` vs error responses

### Cubase

**Cubase transport doesn't trigger camera, or iPhone button doesn't start Cubase**
- MIDI Remote Manager: `lumix_transport.js` must show as active (no red error), and the detected port pair must be named `Lumix Controller`
- ARM must be enabled (orange) on the iPhone
- Check the terminal for `[midi] ✓ Virtual MIDI port 'Lumix Controller' created` at startup — if it's missing/failed instead, `python-rtmidi` couldn't open the port (check the error, e.g. a stale port from a previous run)
- If `server.py` was restarted while Cubase was open, rescan devices in the MIDI Remote Manager so Cubase reconnects to the newly created virtual port

**Motion Control: permission prompt doesn't appear, or Cubase parameter doesn't move**
- URL must start with `https://` — iOS silently withholds the motion permission API on plain HTTP (see [Motion Control](#motion-control))
- Confirm the terminal shows `[motion] client connected` when you tap Enable; if not, the WebSocket never reached the server
- A Quick Control slot must be assigned to an actual parameter in Cubase — the knobs in `lumix_transport.js` feed Quick Controls generically, they don't target a parameter by themselves

### OBS

**"Could not connect to OBS"**
- OBS must be open when `server.py` starts
- WebSocket must be enabled: Tools → WebSocket Server Settings
- Confirm port (4455) and password match `server.py` config

### Pipeline

**"S5 download failed"** / **"No clips found on camera"**
- Camera must remain on WiFi after recording stops (it does by default)
- Test connectivity: `curl http://192.168.1.100/cam.cgi?mode=getstate` should return `<result>ok</result>`
- Test content listing (UPnP `ContentDirectory` `Browse` on `ObjectID=0`):
  ```
  curl -X POST http://192.168.1.100:60606/Server0/CDS_control \
    -H 'Content-Type: text/xml; charset="utf-8"' \
    -H 'SOAPACTION: "urn:schemas-upnp-org:service:ContentDirectory:1#Browse"' \
    --data '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1"><ObjectID>0</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag><Filter>*</Filter><StartingIndex>0</StartingIndex><RequestedCount>0</RequestedCount><SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>'
  ```
  The root container only exposes the camera's most recent ~32 items (photos + videos), so an old take may age out if many photos are shot afterward.

**"LTC decode failed on all clips"**
- Confirm `ltcdump` is installed: `ltcdump --version`
- The LTC signal must be on audio channel 1. If it's on channel 2, edit `resolve_helper.py`:
  ```python
  tc = decode_ltc(clip["path"], channel=2)
  ```
- Check that the LTC signal is present in the first 10 seconds of the recording

**DaVinci Resolve doesn't open / FCPXML not imported**
- The FCPXML file is always saved locally — import it manually via **File → Import → Timeline**
- File location: `~/Movies/LumixSessions/<session>/Take XX/<session> — Take XX.fcpxml`
- Confirm DaVinci Resolve is installed in `/Applications`

---

## Configuration reference

All config is set via `.env` (copy `.env.example` to get started; falls back to the defaults below if a variable isn't set):

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMERA_IP` | `192.168.1.100` | S5 IP address on your network |
| `PORT` | `5050` | Flask server port |
| `OBS_HOST` | `localhost` | OBS WebSocket host |
| `OBS_PORT` | `4455` | OBS WebSocket port |
| `OBS_PASSWORD` | (empty) | OBS WebSocket password (blank = none) |
| `MEDIA_DIR` | `~/Movies/LumixSessions` | Where S5 clips are downloaded |
| `SSL_CERT_FILE` | (empty) | Path to TLS cert — required for Motion Control (HTTPS); server runs plain HTTP if unset |
| `SSL_KEY_FILE` | (empty) | Path to TLS key, paired with `SSL_CERT_FILE` |
| `MOTION_MAX_DEG` | `45` | Default ± tilt range (degrees) mapped to MIDI CC 0–127; also adjustable live via the UI sensitivity slider |

LTC channel and FPS are in `resolve_helper.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `FPS` | `30` | Project frame rate (must match camera and OBS settings) |
| `channel` param in `decode_ltc()` | `1` | Audio channel carrying LTC (1-based) |
