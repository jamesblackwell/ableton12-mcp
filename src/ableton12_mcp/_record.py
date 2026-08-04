"""Loopback audio capture — give Claude per-track "ears".

The Live Object Model can't render a single track to a buffer, and Live's
exporter is master-bus + whole-Arrangement only (and human-gated). To let the
tuning loop *measure* any one track on demand, we capture Live's audio output
in realtime from a **loopback audio device** (BlackHole / Loopback / Soundflower)
with ``ffmpeg``'s ``avfoundation`` input, then analyze the WAV (see ``_analysis``).

This module is pure host/OS: device discovery + the ffmpeg record + a window
trim. The musical orchestration (solo the track, position the playhead, start
playback) lives in ``server.record_audio`` so a multi-second capture never blocks
Live's main thread.

Setup (one-time, surfaced in errors):
  * ``brew install blackhole-2ch``
  * Audio MIDI Setup → a **Multi-Output Device** containing BOTH your physical
    output AND BlackHole, set as Live's (or the system's) output — so you still
    hear playback while it routes to BlackHole for capture.
  * macOS will prompt once for **Microphone** access for the app running the MCP
    server (capturing an avfoundation audio input counts as mic use).
Capture is realtime: a window's wall-clock == its musical length, so keep windows
short (2–4 bars).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import List, Optional, Tuple

# Substrings that identify a loopback (system-audio capture) device by name.
LOOPBACK_NAME_HINTS = ("blackhole", "loopback", "soundflower", "existential")

MICROPHONE_HINT = (
    "macOS may also require Microphone permission for the app running this MCP "
    "server (capturing an audio input device counts as mic use): System Settings "
    "→ Privacy & Security → Microphone."
)

_INSTALL_HINT = (
    "No loopback audio device found. To let Claude capture & analyze individual "
    "tracks:\n"
    "  1. brew install blackhole-2ch\n"
    "  2. Audio MIDI Setup → create a Multi-Output Device with BOTH your speakers/"
    "headphones AND BlackHole; set it as Live's Output (Preferences → Audio) or "
    "the system output, so you still hear playback while it's captured.\n"
    "  3. Retry. " + MICROPHONE_HINT + "\n"
    "Fallback without a loopback device: the manual export path "
    "(capture_session_to_arrangement → open_export_dialog → analyze_audio)."
)


class LoopbackNotFound(RuntimeError):
    """Raised when no loopback capture device can be located."""


def _ensure_ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found on PATH — install it (brew install ffmpeg).")
    return ff


def list_avfoundation_audio_devices() -> List[Tuple[int, str]]:
    """Return ``[(index, name), …]`` for avfoundation AUDIO inputs.

    ``ffmpeg -list_devices`` writes the list to **stderr** and exits non-zero, so
    we read stderr and ignore the return code. Audio indices are a separate list
    from video — we only parse lines under the 'audio devices:' banner."""
    ff = _ensure_ffmpeg()
    proc = subprocess.run(
        [ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, timeout=20,
    )
    devices: List[Tuple[int, str]] = []
    in_audio = False
    for line in proc.stderr.splitlines():
        low = line.lower()
        if "audio devices:" in low:
            in_audio = True
            continue
        if "video devices:" in low:
            in_audio = False
            continue
        if not in_audio:
            continue
        # The line still carries the "[AVFoundation indev @ 0x..]" prefix; the
        # device entry is the LAST "[<digits>] <name>" on the line.
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m:
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def find_loopback_device(preferred: Optional[str] = None) -> Tuple[int, str]:
    """Locate the loopback input. Match ``preferred`` (name substring) if given,
    else the first device whose name matches a known loopback hint. Raises
    ``LoopbackNotFound`` (with install steps + the devices actually seen)."""
    devices = list_avfoundation_audio_devices()
    if preferred:
        pl = preferred.lower()
        for idx, name in devices:
            if pl in name.lower():
                return idx, name
        seen = ", ".join("[%d] %s" % (i, n) for i, n in devices) or "(none)"
        raise LoopbackNotFound(
            "No audio device matching '%s'. Devices seen: %s\n%s" % (preferred, seen, _INSTALL_HINT)
        )
    for idx, name in devices:
        if any(h in name.lower() for h in LOOPBACK_NAME_HINTS):
            return idx, name
    seen = ", ".join("[%d] %s" % (i, n) for i, n in devices) or "(none)"
    raise LoopbackNotFound("Devices seen: %s\n%s" % (seen, _INSTALL_HINT))


def ffmpeg_record(device_index: int, seconds: float, out_path: str) -> str:
    """Record ``seconds`` of the loopback audio input to ``out_path`` (24-bit/
    44.1k stereo WAV). Blocks for ~``seconds``. Raises on ffmpeg failure."""
    ff = _ensure_ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation",
        "-i", ":%d" % device_index,      # ":<idx>" = no video, audio input <idx>
        "-t", "%.3f" % seconds,
        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s24le",
        out_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 20.0)
    if proc.returncode != 0 or not os.path.exists(out_path):
        err = (proc.stderr or "").strip()
        hint = (" — " + MICROPHONE_HINT) if ("permission" in err.lower() or not err) else ""
        raise RuntimeError("ffmpeg capture failed (code %d): %s%s" % (proc.returncode, err or "no output", hint))
    return out_path


def default_capture_path(tag: str = "cap") -> str:
    """A scratch WAV path in the temp dir (git-ignored; never under projects/)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tag))
    return os.path.join(tempfile.gettempdir(), "ableton_%s_%d.wav" % (safe, int(time.time() * 1000)))


def trim_to_window(path: str, keep_seconds: float, max_lead_trim_seconds: float = 1.5,
                   silence_dbfs: float = -50.0) -> dict:
    """Trim leading near-silence (up to ``max_lead_trim_seconds``) then keep
    exactly ``keep_seconds`` — makes the window deterministic despite ffmpeg's
    device-open latency. Best-effort: silently returns if numpy/soundfile absent."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return {"trimmed": False, "reason": "numpy/soundfile unavailable"}

    data, sr = sf.read(path, always_2d=True)
    if data.shape[0] == 0:
        return {"trimmed": False, "reason": "empty file"}
    mono = data.mean(axis=1)
    thresh = 10 ** (silence_dbfs / 20.0)
    max_lead = int(max_lead_trim_seconds * sr)
    loud = np.where(np.abs(mono) > thresh)[0]
    start = int(loud[0]) if loud.size else 0
    start = min(start, max_lead)
    keep = int(keep_seconds * sr)
    end = min(start + keep, data.shape[0]) if keep > 0 else data.shape[0]
    sf.write(path, data[start:end], sr, subtype="PCM_24")
    return {"trimmed": True, "lead_trimmed_sec": round(start / float(sr), 3),
            "kept_sec": round((end - start) / float(sr), 3)}
