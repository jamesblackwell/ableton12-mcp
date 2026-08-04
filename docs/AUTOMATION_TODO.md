# Follow-up: parameter automation over time (clip / device envelopes)

**Status:** scoping note (not implemented). Chosen as a follow-up to the per-track-ears + closed-loop
work (see the plan and `CLAUDE.md` "Listening, metering & analysis").

## Why

The closed loop (`/dial`, `/master`) tunes **static** parameter values. The user's recurring
vision needs values that **evolve over time**:

- "the Pad–guitare spacialized progressively L↔R, but not all the time"
- "volume starts low and progressively increases"
- "soft intro building tension → an important silence → BOOM the techno → melody vibing with the kicks"
- per-section filter opens, riser/downlifter sweeps, breakdown automation

Today these are only **approximated** (velocity ramps + note density + an Auto Pan/LFO gated by note
presence). True moves require writing **automation envelopes**, which the MCP does not expose.

## What to investigate

1. **Does the Live Object Model expose writable envelopes from a Remote Script (Live 12)?**
   - Session/Arrangement clip automation: `Clip.automation_envelope(parameter)` /
     `Clip.create_automation_envelope(...)`, `AutomationEnvelope.insert_step` / `clear_range` /
     value-at-time. Confirm which exist in the bundled `Live.Clip` / `Live.DeviceParameter` API for
     this install (introspect from the Remote Script and log to the Ableton Log.txt).
   - Arrangement-lane automation for a `DeviceParameter` (vs clip envelopes).
   - Which parameters are envelope-able (continuous device params, mixer volume/pan, sends).
2. **Granularity & units:** breakpoint time base (beats vs seconds), value units (native vs 0..1),
   curve/interpolation control.
3. **Threading:** same main-thread marshaling as every other handler (`_run_on_main`).

## Proposed tool shape (if the API exists)

Add one handler + dispatch entry (`remote_script/AbletonMCP/__init__.py`) and one `@mcp.tool`
(`server.py`), mirroring the existing 3-edit pattern:

```
write_automation(track, clip_index | "arrangement",
                 device_index, parameter,
                 points=[{beat, value}|{beat, value_normalized}],
                 start_beat=None, end_beat=None, clear_first=True,
                 curve="linear")
```

- Resolve the parameter exactly like `set_device_parameter` (index/name, quantized/continuous,
  clamp to min/max, honor macro-locked `is_enabled`).
- Insert breakpoints over `[start_beat, end_beat]`; `clear_first` wipes that range.
- Helper presets a command could build on: `fade(param, t0, t1, v0, v1)`,
  `sweep(param, t0, t1, v0, v1)`, `lfo(param, t0, t1, shape, rate, depth)`.

## Payoff

Unlocks real progressive sweeps, per-section dynamics, and intro→drop builds — and lets `/dial`
extend from "tune a static value" to "tune the *shape* of a move" (measure each section by capturing
at different `position_beats`, then write the envelope). Until built, keep the honest approximation +
caveats in every report.
