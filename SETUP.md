# Lumix S5 Controller — Setup Guide

## What this does

A Python server on your MacBook that:
- Serves an iPhone web page (record button + session name + pipeline status)
- Controls Cubase transport from the phone (virtual MIDI port `Lumix Controller` + a MIDI Remote script) — Record/Stop for video takes, plus a separate audio-only page at `/audio`
- Starts/stops OBS recording in sync with the camera
- After stopping, automatically: downloads the S5 clip over WiFi, decodes LTC timecodes from both clips, and creates a synced multicam clip in DaVinci Resolve
- Optionally: turns the iPhone's tilt/rotation into MIDI CC data for Cubase Quick Controls (Motion Control — needs HTTPS, see Step 6)

---

## Prerequisites — install once

```bash
brew install ffmpeg ltc-tools
pip3 install flask requests obsws-python python-rtmidi python-dotenv flask-sock --break-system-packages
```

Only if you want **Motion Control** (see Step 6): `brew install mkcert`. `websocat` (`brew install websocat`) is handy for testing the motion pipeline from the terminal but isn't required for normal use.

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

Then in Cubase, open the **MIDI Remote Manager** (bottom zone) and click the rescan button. The script activates automatically. Cubase listens on the virtual MIDI port `Lumix Controller`, which `server.py` creates itself, so start the server before (or while) Cubase scans for devices.

The link is one-way: server → Cubase over MIDI. Cubase scripts cannot make HTTP requests on this install, so Cubase does not report its transport state back to the server.

---

## Step 6 — Enable HTTPS for Motion Control (optional)

Skip this step if you don't need the iPhone-tilt-to-MIDI-CC feature — everything else works fine over plain HTTP. Motion Control needs HTTPS because iOS Safari only exposes the motion-sensor APIs (and their permission prompt) on a secure connection.

```bash
brew install mkcert
mkcert -install
mkdir certs
mkcert -key-file certs/lumix-key.pem -cert-file certs/lumix.pem <mac-lan-ip> localhost
```

Replace `<mac-lan-ip>` with your Mac's LAN IP (the same one the iPhone uses to reach the server, e.g. `192.168.1.10`). `mkcert -install` will prompt for your Mac password — it adds mkcert's local CA to your system's trust store so Safari won't show certificate warnings.

Then set in `.env`:
```
SSL_CERT_FILE=certs/lumix.pem
SSL_KEY_FILE=certs/lumix-key.pem
```

**One more step, easy to miss:** the iPhone also needs to trust mkcert's CA. Find it with `mkcert -CAROOT`, AirDrop (or email) the `rootCA.pem` file inside that folder to the iPhone, tap it to install the profile (**Settings → General → VPN & Device Management**), then **explicitly enable full trust** for it at **Settings → General → About → Certificate Trust Settings**. Without this last toggle, Safari still shows a warning even after installing the profile.

`certs/` is gitignored — the cert/key are machine-specific and never committed.

---

## Step 7 — Run the server

```bash
cd ~/Documents/obsidian/Studio/Studio/LumixController
python3 server.py
```

Output:
```
──────────────────────────────────────────────────
  Lumix S5 Controller
──────────────────────────────────────────────────
  Camera IP  :  192.168.1.100
  iPhone URL :  https://192.168.1.10:5050
  HTTPS      :  ON  (cert: certs/lumix.pem)
──────────────────────────────────────────────────
[obs]  ✓ Connected to OBS WebSocket at localhost:4455
```
(`HTTPS: OFF` and an `http://` URL if you skipped Step 6 — everything except Motion Control still works.)

---

## Step 8 — Open on iPhone

Open Safari and go to the URL shown in the terminal (e.g. `https://192.168.1.10:5050`).

Optional: **Share → Add to Home Screen** for a standalone icon.

---

## How it works day-to-day

**Before recording:**
1. Start the server (it creates the MIDI port Cubase connects to)
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

**Motion Control (optional, needs HTTPS — see Step 6):**
1. Open the **Audio page** (`/audio`, linked at the top of the main page), open the **MOTION CONTROL** panel and tap **ENABLE MOTION CONTROL** — iOS will ask for motion & orientation permission (must be granted; this prompt reappears on every page reload, that's an iOS limitation)
2. Hold the phone the way you want "centered", then tap **CALIBRATE (ZERO)**
3. Tilt/rotate the phone — the readout shows live α/β/γ degrees, sent over a WebSocket to the server, which sends MIDI CC 21/22/23 to Cubase
4. In Cubase's MIDI Remote Manager, assign the 3 Quick Control slots (from `lumix_transport.js`) to whichever channel/plugin parameters you want to control
5. The **SENSITIVITY** slider adjusts how many degrees of tilt map to the full 0–127 range, live, no restart needed

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
- Check that the MIDI input device `Lumix Controller` is enabled in Cubase for the script, and that the server log shows `Virtual MIDI port 'Lumix Controller' created`

**Pipeline shows error: "S5 download failed"** / **"No clips found on camera"**
- Confirm the camera is still on WiFi after recording (it stays connected)
- Try `curl http://192.168.1.100/cam.cgi?mode=getstate` in Terminal to test basic connectivity (see README.md troubleshooting for the full clip-listing test)
- Note: the camera's root content listing only shows the ~32 most recent items, so shooting a lot of photos after a take can push the video clip out of range

**Pipeline shows error: "LTC decode failed"**
- Make sure `brew install ltc-tools` succeeded (`ltcdump --version`)
- Confirm the LTC track is on audio channel 1 (or adjust `channel` param in `decode_ltc()`)

**DaVinci Resolve step fails**
- Resolve must be **open** before recording stops
- Check that Resolve scripting is enabled: Preferences → System → General → "Enable scripting"

**Port 5050 already in use**
- Change `PORT = 5050` to another value (e.g. 5051) in `server.py`

**Motion Control: "Enable" button does nothing, or permission prompt never appears**
- Confirm the page URL starts with `https://` — iOS silently withholds `DeviceOrientationEvent.requestPermission` on plain HTTP (see Step 6)
- Check the padlock in Safari; if it's not fully trusted, re-check the "explicitly enable full trust" step for the mkcert CA on the iPhone
- The permission prompt only fires on a direct tap — reloading the page always resets it, that's expected

**Motion Control: readout updates but Cubase parameter doesn't move**
- Confirm a Quick Control slot is actually assigned to a parameter in Cubase (the knobs from `lumix_transport.js` feed Quick Controls, not a specific parameter directly)
- Check the terminal for `[motion] client connected` when you tap Enable — if missing, the WebSocket never reached the server (check WiFi / the URL's host)

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
