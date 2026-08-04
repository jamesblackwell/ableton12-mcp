# ableton12 — full-control MCP server for Ableton Live 12

A Python + [`uv`](https://docs.astral.sh/uv/) **MCP server** that lets an LLM (Claude Code,
Claude Desktop, any MCP client) drive **Ableton Live 12** through the **Live Object Model** —
and, uniquely, *measure what it just made*.

It is a **drop-in superset** of the upstream
[`ahujasid/ableton-mcp`](https://github.com/ahujasid/ableton-mcp): it keeps the 16 original tools
(identical names, params and return values, so existing prompts/permissions keep working) and adds
everything upstream can't do — **62 tools** in total:

- **Device & instrument parameter control** — read every parameter and actually *change the tone*:
  filter cutoff, oscillator type, reverb decay, macros, device bypass.
- **Mixer** — volume, pan, sends, mute, solo, arm, color. Addresses the **master bus** and
  **return tracks**, not just regular tracks.
- **Tracks / clips / scenes / transport** — create audio & return tracks, read & remove notes, loop
  settings, duplicate, delete, color, fire scenes, time signature, metronome, playhead.
- **Session → Arrangement capture** — record a Session scene into the Arrangement so Live's
  exporter (Arrangement-only) can render it.
- **Ears** — capture any single track (or the master / a return) to a WAV via a loopback device and
  analyze it: LUFS, true peak, 10-band spectrum, spectral centroid, transient punch, stereo width.
  Plus `compare_audio` for before/after A-B of a parameter change. This is what makes an
  autonomous *measure → adjust → re-measure* tuning loop possible instead of guessing.
- **Host automation (macOS)** — launch Live, new Set, Save, Save As, open the Export dialog.

> Scope: this repo is the **MCP server + Remote Script** only. The composition prompt-layer built on
> top of it (slash commands like `/clip`, `/master`, `/dial`, `/export`) lives elsewhere; every tool
> here is usable from any MCP client on its own.

## Architecture

Two halves talk over TCP `localhost:9877` using **4-byte length-prefixed JSON frames**:

```
LLM ⇄ (stdio) ⇄  ableton12 MCP server  ⇄ (TCP 9877) ⇄  Remote Script (inside Live)
                 src/ableton12_mcp/                     remote_script/AbletonMCP/
                        │
                        └─ ffmpeg + loopback device ─► WAV ─► analysis (numpy/scipy/pyloudnorm)
```

- **MCP server** (`src/ableton12_mcp/server.py`): FastMCP server named `AbletonMCP`. Each
  `@mcp.tool()` sends one command frame and returns the result. `_host.py` does the macOS-level
  automation, `_record.py` the loopback capture, `_analysis.py` the audio metrics.
- **Remote Script** (`remote_script/AbletonMCP/__init__.py`): a `ControlSurface` running inside
  Live's embedded Python. A socket thread does pure I/O; **every** command — reads included — is
  marshaled onto Live's main thread via `schedule_message` + a reply queue (`_run_on_main`). That is
  the key correctness fix: upstream's device-parameter attempts crashed because reads ran off the
  main thread while writes ran on it.

## Requirements

| | |
|---|---|
| **Ableton Live 12** | the Remote Script targets Live 12's Object Model |
| **macOS** | required for host automation (`launch_ableton`, `save_set*`, `open_export_dialog`) and for loopback capture (`avfoundation`). The Live-control tools themselves are OS-agnostic. |
| **Python ≥ 3.10** + [`uv`](https://docs.astral.sh/uv/) | `brew install uv` |
| **ffmpeg** *(optional)* | needed by `record_audio` / `record_and_analyze` — `brew install ffmpeg` |
| **BlackHole** *(optional)* | loopback device for the "ears" — `brew install blackhole-2ch` |

## Install

```bash
# 1. Clone and sync dependencies
git clone https://github.com/verove-jordan/ableton12-mcp.git
cd ableton12-mcp
uv sync

# 2. Install the Remote Script into Ableton's User Library
#    (quit Live first; the script backs up any existing AbletonMCP install)
./install.sh

# 3. In Ableton: Settings → Link, Tempo & MIDI → Control Surfaces → pick "AbletonMCP"
#    with Input = None and Output = None, then RESTART Ableton.
#    The status bar should read:  AbletonMCP (ableton12): listening on port 9877

# 4. Register the server with your MCP client — e.g. Claude Code, at user scope:
claude mcp add AbletonMCP -s user -- uvx --from "$(pwd)" ableton12
#    Then run /mcp to connect and call the `health` tool to verify.
```

Generic MCP client config (Claude Desktop, `.mcp.json`, …) — use an **absolute** path:

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/ableton12-mcp", "ableton12"]
    }
  }
}
```

Only one process may bind port 9877 — make sure no old `uvx ableton-mcp` server is still running.

### Optional: the "ears" (per-track capture)

`record_audio` records Live's output in realtime from a **loopback** audio input, so you need one:

1. `brew install ffmpeg blackhole-2ch`
2. **Audio MIDI Setup** → create a **Multi-Output Device** containing **both** your speakers/
   headphones **and** BlackHole; set it as Live's output (Preferences → Audio) or the system output —
   that way you still *hear* playback while it is captured.
3. macOS will prompt once for **Microphone** access for the app running the MCP server (capturing an
   audio input counts as mic use): System Settings → Privacy & Security → Microphone.

Capture is realtime — a window's wall-clock equals its musical length, so keep windows short (2–4 bars).

### Optional: host automation permissions

`save_set_as` and `open_export_dialog` drive Live's menus with AppleScript keystrokes, which needs a
one-time **Accessibility** grant for the app running the MCP server (Terminal / iTerm / your editor):
System Settings → Privacy & Security → Accessibility. These routines are best-effort and depend on
the running Live version's dialog layout — always verify the resulting file on disk rather than
trusting the return value.

## Tool catalog (62)

**Session & transport:** `get_session_info`, `set_tempo`, `start_playback`, `stop_playback`,
`set_time_signature`, `set_metronome`, `set_song_position`, `get_transport`, `health`.

**Tracks:** `get_track_info`, `create_midi_track`, `create_audio_track`, `create_return_track`,
`set_track_name`, `delete_track`, `duplicate_track`.

**Mixer:** `set_track_volume`, `set_track_pan`, `set_track_send`, `set_track_mute`,
`set_track_solo`, `set_track_arm`, `set_track_color`.

**Devices & parameters:** `get_device_parameters`, `set_device_parameter`, `set_device_enabled`,
`delete_device`, `get_macros`, `set_macro`.

**Browser / loading:** `get_browser_tree`, `get_browser_items_at_path`, `load_instrument_or_effect`,
`load_drum_kit`.

**Clips:** `create_clip`, `add_notes_to_clip`, `get_clip_notes`, `remove_notes_from_clip`,
`set_clip_name`, `set_clip_loop`, `duplicate_clip`, `delete_clip`, `set_clip_color`, `fire_clip`,
`stop_clip`.

**Scenes:** `create_scene`, `fire_scene`, `set_scene_name`, `delete_scene`.

**Session → Arrangement capture:** `set_arrangement_record`, `set_clip_trigger_quantization`,
`capture_session_to_arrangement`.

**Metering:** `get_track_meter`, `get_master_meter`  *(levels 0..1, only while playing)*.

**Listening & analysis:** `record_audio`, `record_and_analyze`, `analyze_audio`, `compare_audio`.

**Host automation (macOS):** `launch_ableton`, `new_live_set`, `save_set`, `save_set_as`,
`open_export_dialog`.

### Addressing the master bus and return tracks

Track-scoped tools (`get_track_info`, `load_instrument_or_effect`, `get_device_parameters`,
`set_device_parameter`, `set_device_enabled`, `delete_device`, `set_track_volume`, `set_track_pan`,
`get_track_meter`, `record_audio`, …) take an optional **`track`** argument that overrides the
integer `track_index`:

- `track='master'` → the master bus (this is how you build a mastering chain),
- `track='return:0'` → return track 0 (`return:N`), fed by `set_track_send`.

Plain integer `track_index` calls are unchanged.

### Parameter value semantics (read this before "changing the tone")

`get_device_parameters(track, device)` returns each parameter with `name`, `value`, `min`, `max`,
`is_quantized`, `value_items` (enum labels) and `display_value` (e.g. `"440 Hz"`, `"Lowpass"`).
**Always read before you write** — ranges are device- and install-specific. Then
`set_device_parameter`:

- **Continuous** params: pass `value` in native units (within `[min, max]`), or `value_normalized`
  (0..1 → `min + x*(max-min)`). Values are clamped.
- **Quantized** params (`is_quantized=true`): pass `value` as either the integer index or the exact
  `value_items` label (case-insensitive), or `value_normalized` to pick by fraction.
- A parameter mapped to a macro reports `is_enabled=false`; writes to it are skipped and the reply
  says so (`applied=false`). Change it through the macro (`set_macro`) instead.

**Mixer ranges:** volume is a **fader position 0..1, not dB** (~0.85 ≈ 0 dB, 1.0 ≈ +6 dB — the reply
reports the resulting dB). Pan is **−1..+1**. Sends are **0..1**.

Typical workflow: `load_instrument_or_effect` → `get_device_parameters` → `set_device_parameter`.
After structural edits (`delete_track`, `create_*`, `delete_scene`) re-read `get_session_info`,
because indices shift.

### What `analyze_audio` returns

Peak/RMS dBFS, crest factor, clipping; **LUFS** (`lufs_integrated`, `lufs_short_term_max`,
`lufs_range_lra`) and **true peak** (`true_peak_dbtp`); a **10-band spectrum** (`spectrum_bands` /
`spectrum_bands_db`) plus a legacy 4-band; brightness (`spectral_centroid_hz`,
`spectral_rolloff_hz`, `spectral_flatness`); transients (`onset_strength`, `onset_rate_per_sec`,
`band_crest_db` per band — low-band crest ≈ kick punch); stereo (`stereo_correlation`,
`stereo_width`, `band_stereo_width`); plus noise floor, DC offset and silence detection.
`loudness_method` flags whether pyloudnorm or the numpy fallback produced the LUFS figures.

`compare_audio(a, b)` turns two of those into per-metric deltas in human terms with a one-line
`headline`, suppressing sub-perceptual changes — the progress meter for a tuning loop.

## Known limitation — no automation over time

The server can set a parameter to a **static** value, but it cannot write **clip/device automation
envelopes**, so moves that evolve across an arrangement (a filter opening over 16 bars, a
progressive L↔R pan sweep) are not directly authorable. Workarounds and the plan to close the gap
are in [`docs/AUTOMATION_TODO.md`](docs/AUTOMATION_TODO.md).

## Debugging

- **"Connection refused" / can't connect:** Ableton isn't running, wasn't restarted after a script
  change, or the control surface isn't selected. Don't retry blindly — check those three.
- Confirm Live holds the port: `lsof -nP -iTCP:9877 -sTCP:LISTEN` should show `Live`. If it shows a
  `python`/`uvx` process, a stale server has the port.
- Remote Script logs: `~/Library/Preferences/Ableton/Live 12*/Log.txt` — look for
  `AbletonMCP (ableton12) ... initializing` and `listening on port`. Use `self.log_message`;
  `print()` from the script is invisible.
- After editing the Remote Script you must restart Ableton (or re-select the control surface).
  `install.sh` clears `__pycache__`; the `health` tool reports the loaded version so you can confirm
  the new build is active.

## Credits

API-compatible with, and inspired by, [`ahujasid/ableton-mcp`](https://github.com/ahujasid/ableton-mcp).
Not affiliated with Ableton AG.
