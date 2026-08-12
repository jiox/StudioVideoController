# Lumix S5 Controller — Setup Guide

## What this does

A Python server on your MacBook that:
- Serves an iPhone web page (record button + session name + pipeline status)
- Listens for Cubase play/stop (via a MIDI Remote script, HTTP only — no MIDI ports) and triggers the camera when armed
- Starts/stops OBS recording in sync with the camera
- After stopping, automatically: downloads the S5 clip over WiFi, decodes LTC timecodes from both clips, and creates a synced multicam clip in DaVinci Resolve

---

## Prerequisites — install once

```bash
brew install ffmpeg ltc-tools
pip3 install flask requests obsws-python python-rtmidi python-dotenv --break-system-packages
```

---

## Step 1 — Connect the S5 to your home WiFi

The camera must be on the same network as your Mac and iPhone.

1. On the S5: **MENU → Setup (wrench) → Wi-Fi → Wi-Fi Function → Via Network**
2. Connect to your home router
3. Find the IP: **MENU → Setup → Wi-Fi → Wi-Fi Function → Check Wi-Fi connection status**

> **Tip:** Set a static DHCP reservation on your router so the camera always gets the same IP.

---

## Step 2 — Set camera mode

Put the camera in **Creative Video mode** (the cine camera icon on the mode dial). This is required for the `video_recstart` command to work.

---

## Step 3 — Set the camera IP

Copy `.env.example` to `.env` and set your camera's IP:

```bash
cp .env.example .env
```

```
CAMERA_IP=192.168.1.100   # ← replace with your camera's actual IP
```

---

## Step 4 — Enable OBS WebSocket

In OBS: **Tools → WebSocket Server Settings → Enable WebSocket server**

Default port is 4455 with no password — matches the server defaults. If you set a password, update `OBS_PASSWORD` in `.env`.

---

## Step 5 — Install the Cubase MIDI Remote script (one-time)

Copy `lumix_transport.js` to:

```
~/Documents/Cubase MIDI Remote/Driver Scripts/Local/
```

Then in Cubase, open the **MIDI Remote Manager** (bottom zone) and click the rescan button. The script activates automatically. That's the whole setup — no MIDI ports, no Generic Remote mapping, no start-order dependency between the server and Cubase. It works both ways over plain HTTP:

- Cubase → server: the script detects transport play/stop and calls the server directly.
- Server → Cubase: the script holds an HTTP request open against the server (long-polling) and writes to Cubase's own transport the moment the server has a start/stop command — no timer, no MIDI, no manual mapping.

---

## Step 6 — Run the server

```bash
cd ~/Documents/obsidian/Studio/Studio/LumixController
python3 server.py
```

Output:
```
──────────────────────────────────────────────────
  Lumix S5 Controller
──────────────────────────────────────────────────
  Camera IP  :  192.168.178.122
  iPhone URL :  http://192.168.1.10:5050
──────────────────────────────────────────────────
[obs]  ✓ Connected to OBS WebSocket at localhost:4455
```

---

## Step 7 — Open on iPhone

Open Safari and go to the URL shown in the terminal (e.g. `http://192.168.1.10:5050`).

Optional: **Share → Add to Home Screen** for a standalone icon.

---

## How it works day-to-day

**Before recording:**
1. Start the server (any order relative to Cubase — no MIDI port to race against)
2. Make sure Cubase and OBS are running

**Recording a take:**
1. On the iPhone, type a session name and tap **SET**
2. Tap **ARM** to enable Cubase sync (glows orange)
3. Press **▶** in Cubase → camera + OBS start automatically
4. Press **■** in Cubase → camera + OBS stop; pipeline begins automatically

**After stopping, the iPhone shows pipeline progress:**
- Downloading S5 footage over WiFi …
- Decoding LTC timecode …
- Creating multicam in DaVinci Resolve …
- Take XX ready in Resolve ✓

DaVinci Resolve opens to the Media page with the multicam clip ready.

**Manual control (without Cubase):**
- Tap the record button on the iPhone at any time (ARM not required for manual use)
- This also starts/stops Cubase transport

---

## Pipeline — what happens behind the scenes

After every take:
1. **Download:** the most recently recorded clip is pulled from the camera over WiFi (port 50001) to `~/Movies/LumixSessions/<session>/<take>/`
2. **LTC decode:** `ffmpeg` extracts the LTC audio channel; `ltcdump` reads the first valid timecode frame from each clip (Lumix + OBS)
3. **DaVinci Resolve:** the script opens (or creates) a project named after the session, creates a "Take NN" bin, imports both clips, sets each clip's timecode from the decoded LTC value, and calls `CreateMultiCamClip` to build a timecode-synced multicam clip

---

## Troubleshooting

**"Cannot reach camera"**
- Check `CAMERA_IP` in `.env`
- Camera must be in Creative Video mode
- WiFi icon must be visible in the camera status bar

**Cubase transport doesn't trigger camera (or vice versa)**
- Check MIDI Remote Manager: `lumix_transport.js` must show as active (green)
- Check that ARM is enabled (glowing orange) on the iPhone
- Check Cubase's script console for `[Lumix]` log lines — `poll error`/`longPoll setup failed` means it can't reach `server.py` (confirm `SERVER` at the top of `lumix_transport.js` matches `PORT` in `server.py`, and that the server is running)

**Pipeline shows error: "S5 download failed"** / **"No clips found on camera"**
- Confirm the camera is still on WiFi after recording (it stays connected)
- Try `curl http://192.168.178.122/cam.cgi?mode=getstate` in Terminal to test basic connectivity (see README.md troubleshooting for the full clip-listing test)
- Note: the camera's root content listing only shows the ~32 most recent items, so shooting a lot of photos after a take can push the video clip out of range

**Pipeline shows error: "LTC decode failed"**
- Make sure `brew install ltc-tools` succeeded (`ltcdump --version`)
- Confirm the LTC track is on audio channel 1 (or adjust `channel` param in `decode_ltc()`)

**DaVinci Resolve step fails**
- Resolve must be **open** before recording stops
- Check that Resolve scripting is enabled: Preferences → System → General → "Enable scripting"

**Port 5050 already in use**
- Change `PORT = 5050` to another value (e.g. 5051) in `server.py`

---

## Auto-start on login (optional)

```bash
cat > ~/Library/LaunchAgents/com.lumix.controller.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.lumix.controller</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/yourname/path/to/LumixController/server.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/lumix_controller.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/lumix_controller.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.lumix.controller.plist
```
