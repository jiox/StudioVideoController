/**
 * lumix_transport.js — Cubase MIDI Remote Script
 * ─────────────────────────────────────────────────────────────────
 * Triggers Cubase Record/Stop from the Lumix Controller server
 * via a virtual MIDI port (created by server.py using python-rtmidi,
 * named "Lumix Controller"). Unlike the old Generic Remote setup, the
 * note-to-transport mapping is declared directly in this script via
 * mMidiBinding + makeValueBinding — no manual mapping in Cubase's UI,
 * just install this file once.
 *
 * HTTP isn't viable from inside a Cubase script on this install —
 * require('http') and require('child_process') both throw "invalid
 * module load callback return value" (confirmed by direct testing), so
 * this uses real MIDI, following the exact pattern from Cubase's own
 * bundled ExampleCompany_RealWorldDevice.js reference script.
 *
 * Install: copy this file to
 *   ~/Documents/Cubase MIDI Remote/Driver Scripts/Local/
 * then in Cubase open the MIDI Remote Manager (bottom zone) and
 * click the refresh/rescan button. The script activates automatically.
 * ─────────────────────────────────────────────────────────────────
 */

var midiremote_api = require('midiremote_api_v1')

var driver = midiremote_api.makeDeviceDriver('Lumix', 'Camera Transport', 'Studio')

// ── Port detection ───────────────────────────────────────────────────
//
// server.py creates a single virtual port named "Lumix Controller" via
// both rtmidi.MidiOut and rtmidi.MidiIn — Python's "out" side is what
// Cubase sees as its MIDI input (that's the direction we actually use;
// the input port pair is only declared because detectPortPair() needs
// both to match).

var midiInput  = driver.mPorts.makeMidiInput()
var midiOutput = driver.mPorts.makeMidiOutput()

driver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameEquals('Lumix Controller')
    .expectOutputNameEquals('Lumix Controller')

// ── Surface + MIDI bindings ─────────────────────────────────────────
//
// Note 20 (channel 1) -> Record, Note 21 (channel 1) -> Stop. Must match
// NOTE_PLAY/NOTE_STOP in server.py. mRecord (not mStart) so this actually
// arms + starts recording, not just playback.

var recordBtn = driver.mSurface.makeButton(0, 0, 1, 1)
recordBtn.mSurfaceValue.mMidiBinding.setInputPort(midiInput).bindToNote(0, 20)

var stopBtn = driver.mSurface.makeButton(1, 0, 1, 1)
stopBtn.mSurfaceValue.mMidiBinding.setInputPort(midiInput).bindToNote(0, 21)

// ── Host mapping ──────────────────────────────────────────────────────

var page = driver.mMapping.makePage('Transport Sync')
page.makeValueBinding(recordBtn.mSurfaceValue, page.mHostAccess.mTransport.mValue.mRecord).setTypeToggle()
page.makeValueBinding(stopBtn.mSurfaceValue, page.mHostAccess.mTransport.mValue.mStop).setTypeToggle()

driver.mOnActivate = function (activeDevice) {
    console.log('[Lumix] Camera Transport script active')
}
