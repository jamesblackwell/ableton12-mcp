"""Offline audio analysis — the closest thing to "listening".

Claude can't subjectively *hear* audio, but it can capture a track/section to WAV
(loopback capture via ``_record``, or the export path) and reason over objective
measurements: loudness (LUFS / true-peak), peak/RMS, dynamics, a multi-band
spectral balance, spectral shape (centroid/rolloff/flatness), transient/punch,
stereo width and silence. ``analyze_audio`` returns a JSON-able dict of those
measures; ``compare_audio`` diffs two of them in human terms — the primitive a
closed tuning loop uses to know whether a change actually helped.

Requires ``numpy`` + ``soundfile`` (declared in pyproject; imported lazily so the
rest of the server still runs without them). ``pyloudnorm`` (ITU-R BS.1770-4
LUFS) and ``scipy`` (true-peak oversampling) are used when present, with a
pure-numpy fallback otherwise — the ``loudness_method`` field reports which path
ran, so the numbers are never silently approximate.

Backward-compat: every key the original ``analyze_audio`` returned is preserved
(``spectral_balance``, ``peak_dbfs`` …); the richer fields are added alongside,
so ``/export`` and ``/master`` only gain information.
"""

from __future__ import annotations

import math
import os
from typing import Optional


def _db(x) -> Optional[float]:
    if x is None or x <= 0:
        return None
    return round(20.0 * math.log10(x), 2)


# 10 log-spaced bands (Hz edges) for the detailed spectrum.
_SPECTRUM_EDGES = [20, 40, 80, 160, 315, 630, 1250, 2500, 5000, 10000, 20000]
# 4 coarse bands kept verbatim for backward-compat with the original analyze_audio.
_LEGACY_BANDS = {"low_20_120": (20, 120), "lowmid_120_500": (120, 500),
                 "mid_500_2k": (500, 2000), "high_2k_20k": (2000, 20000)}

# Cap the cost of full-length time-domain ops (band crest/width) on long files.
_HEAVY_CAP_SECONDS = 60.0


# --------------------------------------------------------------------------- #
# Spectrum helpers (pure numpy)
# --------------------------------------------------------------------------- #

def _stft_mag(mono, sr, nperseg=4096, hop=1024):
    """Magnitude STFT in pure numpy. Returns (freqs, S) with S shape
    (bins, frames). One zero-padded frame if the signal is shorter than a window."""
    import numpy as np

    n = len(mono)
    win = np.hanning(nperseg)
    freqs = np.fft.rfftfreq(nperseg, 1.0 / sr)
    if n < nperseg:
        frame = np.zeros(nperseg)
        frame[:n] = mono
        return freqs, np.abs(np.fft.rfft(frame * win))[:, None]
    cols = []
    for s in range(0, n - nperseg + 1, hop):
        cols.append(np.abs(np.fft.rfft(mono[s:s + nperseg] * win)))
    return freqs, np.stack(cols, axis=1)


def _band_fraction(freqs, meanmag, lo, hi):
    import numpy as np
    total = float(np.sum(meanmag)) or 1.0
    mask = (freqs >= lo) & (freqs < hi)
    return float(np.sum(meanmag[mask])) / total


def _band_timeseries(sig, sr, lo, hi):
    """Band-limit a 1-D signal to [lo, hi) via rFFT masking; return the band signal."""
    import numpy as np
    n = len(sig)
    spec = np.fft.rfft(sig)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spec = spec * ((freqs >= lo) & (freqs < hi))
    return np.fft.irfft(spec, n=n)


# --------------------------------------------------------------------------- #
# Loudness (LUFS / true peak)
# --------------------------------------------------------------------------- #

def _lufs_pyloudnorm(data, sr):
    """Integrated / short-term-max / LRA (LU) via pyloudnorm (BS.1770-4)."""
    import numpy as np
    import pyloudnorm as pyln

    meter = pyln.Meter(sr)  # default 400 ms block, BS.1770 K-weighting + gating
    try:
        integ = float(meter.integrated_loudness(data))
    except Exception:
        integ = None
    if integ is not None and not math.isfinite(integ):
        integ = None

    # Short-term: 3 s window, 1 s hop. Needs >= 3 s of audio to be meaningful.
    st_vals = []
    n = data.shape[0]
    wlen = int(3.0 * sr)
    hop = int(1.0 * sr)
    if n >= wlen:
        for s in range(0, n - wlen + 1, hop):
            try:
                v = float(meter.integrated_loudness(data[s:s + wlen]))
                if math.isfinite(v):
                    st_vals.append(v)
            except Exception:
                pass
    st_max = round(max(st_vals), 2) if st_vals else (round(integ, 2) if integ is not None else None)

    # LRA ≈ P95 − P10 of the gated short-term distribution (EBU R128 approximation).
    lra = None
    if len(st_vals) >= 3:
        arr = np.array([v for v in st_vals if v > -70.0])
        if arr.size >= 3:
            rel_gate = float(np.mean(arr)) - 20.0
            gated = arr[arr >= rel_gate]
            if gated.size >= 2:
                lra = round(float(np.percentile(gated, 95) - np.percentile(gated, 10)), 2)

    return (round(integ, 2) if integ is not None else None), st_max, lra, "pyloudnorm"


def _kweight_response(freqs, fs):
    """Linear magnitude of the BS.1770 K-weighting (two biquads) at ``freqs``."""
    import numpy as np
    # Stage 1 — high-shelf "pre-filter".
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / fs)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    # Stage 2 — RLB high-pass.
    f0b, Qb = 38.13547087602444, 0.5003270373238773
    Kb = math.tan(math.pi * f0b / fs)
    a0b = 1 + Kb / Qb + Kb * Kb
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2 * (Kb * Kb - 1) / a0b, (1 - Kb / Qb + Kb * Kb) / a0b]

    w = 2 * np.pi * np.asarray(freqs) / fs
    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    H1 = (b1[0] + b1[1] * z1 + b1[2] * z2) / (a1[0] + a1[1] * z1 + a1[2] * z2)
    H2 = (b2[0] + b2[1] * z1 + b2[2] * z2) / (a2[0] + a2[1] * z1 + a2[2] * z2)
    return np.abs(H1 * H2)


def _lufs_numpy(data, sr):
    """Ungated integrated-loudness estimate via frequency-domain K-weighting.
    Used only when pyloudnorm is unavailable; clearly labelled as approximate."""
    import numpy as np
    chans = data.shape[1]
    z = 0.0
    for c in range(chans):
        x = data[:, c]
        n = len(x)
        if n == 0:
            continue
        X = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        H = _kweight_response(freqs, sr)
        Y = np.abs(X) * H
        # Parseval for a one-sided spectrum → mean square of the K-weighted signal.
        sq = Y ** 2
        if n % 2 == 0:
            energy = sq[0] + 2 * np.sum(sq[1:-1]) + sq[-1]
        else:
            energy = sq[0] + 2 * np.sum(sq[1:])
        z += float(energy / (n * n))
    if z <= 0:
        return None, None, None, "numpy-fft-approx"
    integ = round(-0.691 + 10.0 * math.log10(z), 2)
    return integ, integ, None, "numpy-fft-approx"


def _true_peak_dbtp(data):
    """Inter-sample true peak (dBTP). 4× oversampled via scipy when available,
    else the sample peak (labelled)."""
    import numpy as np
    try:
        from scipy.signal import resample_poly
        up = resample_poly(data, 4, 1, axis=0)
        return _db(float(np.max(np.abs(up)))), "4x-oversample"
    except Exception:
        return _db(float(np.max(np.abs(data)))) if data.size else None, "sample-peak"


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #

def analyze_audio(wav_path: str, max_seconds: float = 600.0) -> dict:
    """Measure an audio file and return a JSON-able dict of objective features.

    Loudness: peak/RMS dBFS, crest factor, integrated/short-term LUFS, LRA, true
    peak (dBTP). Spectrum: a 10-band balance (+ the legacy 4-band), centroid,
    rolloff, flatness. Transient: onset strength/rate and per-band crest (punch).
    Stereo: correlation, overall + per-band width. Plus DC offset, noise floor,
    leading/trailing silence and duration. ``loudness_method`` reports whether
    LUFS came from pyloudnorm or the numpy fallback."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise RuntimeError(
            "audio analysis needs numpy + soundfile (pip/uv install in the "
            "ableton12 MCP env): %s" % e
        )

    path = os.path.expanduser(wav_path)
    if not os.path.exists(path):
        raise FileNotFoundError("no such audio file: %s" % path)

    data, sr = sf.read(path, always_2d=True)  # (frames, channels), float
    if data.shape[0] > int(max_seconds * sr):
        data = data[: int(max_seconds * sr)]
    frames, channels = data.shape
    duration = frames / float(sr) if sr else 0.0

    mono = data.mean(axis=1)
    peak = float(np.max(np.abs(data))) if frames else 0.0
    rms = float(np.sqrt(np.mean(np.square(mono)))) if frames else 0.0
    crest = _db(peak) - _db(rms) if (peak > 0 and rms > 0) else None

    # --- legacy 4-band spectral balance (energy fraction), preserved verbatim ---
    bands = _LEGACY_BANDS
    spectral = {}
    if frames > 1:
        win = mono[: 1 << int(math.floor(math.log2(min(frames, 1 << 18))))] if frames >= 2 else mono
        spectrum = np.abs(np.fft.rfft(win * np.hanning(len(win))))
        freqs0 = np.fft.rfftfreq(len(win), 1.0 / sr)
        total = float(np.sum(spectrum)) or 1.0
        for label, (lo, hi) in bands.items():
            mask = (freqs0 >= lo) & (freqs0 < hi)
            spectral[label] = round(float(np.sum(spectrum[mask])) / total, 4)

    # --- richer spectrum + shape from an STFT mean magnitude ---
    spectrum_bands = {}
    spectrum_bands_db = {}
    centroid = rolloff = flatness = None
    onset_strength = onset_rate = None
    if frames > 1:
        freqs, S = _stft_mag(mono, sr)
        meanmag = S.mean(axis=1)
        for i in range(len(_SPECTRUM_EDGES) - 1):
            lo, hi = _SPECTRUM_EDGES[i], _SPECTRUM_EDGES[i + 1]
            label = "%d_%d" % (lo, hi)
            frac = _band_fraction(freqs, meanmag, lo, hi)
            spectrum_bands[label] = round(frac, 4)
            spectrum_bands_db[label] = _db(frac) if frac > 0 else None

        psum = float(np.sum(meanmag)) or 1.0
        centroid = round(float(np.sum(freqs * meanmag) / psum), 1)
        cumulative = np.cumsum(meanmag)
        roll_idx = int(np.searchsorted(cumulative, 0.85 * cumulative[-1]))
        roll_idx = min(roll_idx, len(freqs) - 1)
        rolloff = round(float(freqs[roll_idx]), 1)
        pos = meanmag[meanmag > 0]
        if pos.size:
            gmean = math.exp(float(np.mean(np.log(pos))))
            amean = float(np.mean(meanmag))
            flatness = round(gmean / amean, 4) if amean > 0 else None

        # onset / transient texture = half-wave-rectified spectral flux
        if S.shape[1] >= 2:
            diff = np.diff(S, axis=1)
            flux = np.sum(np.maximum(diff, 0.0), axis=0)
            fe = np.sum(S[:, 1:], axis=0) + 1e-12
            nf = flux / fe                       # fraction of spectrum that is "new"
            onset_strength = round(float(np.mean(nf)), 4)
            if nf.size and float(np.std(nf)) > 0:
                thresh = float(np.mean(nf) + np.std(nf))
                peaks = int(np.sum((nf[1:-1] > nf[:-2]) & (nf[1:-1] >= nf[2:]) & (nf[1:-1] > thresh)))
                onset_rate = round(peaks / duration, 2) if duration > 0 else None

    # --- per-band crest (punch) + per-band stereo width (bounded cost) ---
    cap = int(min(frames, _HEAVY_CAP_SECONDS * sr)) if frames else 0
    band_crest_db = {}
    band_stereo_width = {}
    if cap > 1:
        mono_c = mono[:cap]
        left_c = data[:cap, 0]
        right_c = data[:cap, 1] if channels >= 2 else None
        for label, (lo, hi) in bands.items():
            b = _band_timeseries(mono_c, sr, lo, hi)
            bpeak = float(np.max(np.abs(b))) if b.size else 0.0
            brms = float(np.sqrt(np.mean(np.square(b)))) if b.size else 0.0
            band_crest_db[label] = round(_db(bpeak) - _db(brms), 2) if (bpeak > 0 and brms > 0) else None
            if right_c is not None:
                bl = _band_timeseries(left_c, sr, lo, hi)
                br = _band_timeseries(right_c, sr, lo, hi)
                mid = (bl + br) / 2.0
                side = (bl - br) / 2.0
                mrms = float(np.sqrt(np.mean(mid ** 2)))
                srms = float(np.sqrt(np.mean(side ** 2)))
                band_stereo_width[label] = round(srms / (mrms + srms + 1e-12), 4)

    # --- stereo correlation / overall width ---
    correlation = None
    stereo_width = None
    if channels >= 2:
        l, r = data[:, 0], data[:, 1]
        if np.std(l) > 1e-9 and np.std(r) > 1e-9:
            correlation = round(float(np.corrcoef(l, r)[0, 1]), 4)
        mid = (l + r) / 2.0
        side = (l - r) / 2.0
        mrms = float(np.sqrt(np.mean(mid ** 2)))
        srms = float(np.sqrt(np.mean(side ** 2)))
        stereo_width = round(srms / (mrms + srms + 1e-12), 4)

    dc_offset = round(float(np.mean(mono)), 6) if frames else 0.0

    # --- noise floor: 10th-percentile of short-frame RMS ---
    noise_floor = None
    if frames:
        fl = max(1, int(0.05 * sr))
        nfrm = frames // fl
        if nfrm >= 4:
            trimmed = mono[: nfrm * fl].reshape(nfrm, fl)
            frame_rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
            noise_floor = _db(float(np.percentile(frame_rms[frame_rms > 0], 10))) if np.any(frame_rms > 0) else None

    # --- leading / trailing silence (below -60 dBFS) ---
    thresh = 10 ** (-60 / 20.0)
    loud = np.abs(mono) > thresh
    if loud.any():
        idx = np.where(loud)[0]
        lead = round(idx[0] / float(sr), 3)
        trail = round((frames - 1 - idx[-1]) / float(sr), 3)
    else:
        lead = trail = round(duration, 3)

    # --- loudness (LUFS / true peak) ---
    try:
        lufs_i, lufs_st, lra, loud_method = _lufs_pyloudnorm(data, sr)
    except Exception:
        lufs_i, lufs_st, lra, loud_method = _lufs_numpy(data, sr)
    true_peak, tp_method = _true_peak_dbtp(data)

    return {
        "path": path,
        "sample_rate": sr,
        "channels": channels,
        "duration_sec": round(duration, 3),
        # loudness / dynamics
        "peak_dbfs": _db(peak),
        "rms_dbfs": _db(rms),
        "crest_factor_db": round(crest, 2) if crest is not None else None,
        "clipping": bool(peak >= 0.999),
        "lufs_integrated": lufs_i,
        "lufs_short_term_max": lufs_st,
        "lufs_range_lra": lra,
        "true_peak_dbtp": true_peak,
        "loudness_method": loud_method,
        "true_peak_method": tp_method,
        # spectrum
        "spectral_balance": spectral,            # legacy 4-band (preserved)
        "spectrum_bands": spectrum_bands,        # 10 log bands (energy fraction)
        "spectrum_bands_db": spectrum_bands_db,
        "spectral_centroid_hz": centroid,
        "spectral_rolloff_hz": rolloff,
        "spectral_flatness": flatness,
        # transient / punch
        "onset_strength": onset_strength,
        "onset_rate_per_sec": onset_rate,
        "band_crest_db": band_crest_db,
        # stereo
        "stereo_correlation": correlation,
        "stereo_is_mono": (correlation is not None and correlation > 0.999),
        "stereo_width": stereo_width,
        "band_stereo_width": band_stereo_width,
        # misc
        "dc_offset": dc_offset,
        "noise_floor_dbfs": noise_floor,
        "lead_silence_sec": lead,
        "trail_silence_sec": trail,
    }


# --------------------------------------------------------------------------- #
# A/B comparison — deltas in human terms (the loop's progress meter)
# --------------------------------------------------------------------------- #

# Perceptual floors: only report a change when it exceeds the just-noticeable level.
_FLOOR_LU = 0.5
_FLOOR_BAND_DB = 0.7
_FLOOR_CENTROID = 75.0
_FLOOR_WIDTH = 0.03
_FLOOR_CREST = 0.7


def _delta(a, b):
    if a is None or b is None:
        return None
    return round(b - a, 2)


def compare_analyses(a: dict, b: dict) -> dict:
    """Diff two ``analyze_audio`` dicts (b relative to a) into human-readable
    deltas, suppressing sub-perceptual noise. Used by the tuning loop each
    iteration to decide whether a parameter move actually helped."""
    out = {"loudness": {}, "spectrum": {}, "tone": {}, "punch": {}, "width": {}, "dynamics": {}}
    verdicts = []

    # loudness
    d = _delta(a.get("lufs_integrated"), b.get("lufs_integrated"))
    if d is not None:
        out["loudness"]["lufs_integrated_delta"] = d
        if abs(d) >= _FLOOR_LU:
            verdicts.append("%+.1f LU %s" % (d, "louder" if d > 0 else "quieter"))
    tp_a, tp_b = a.get("true_peak_dbtp"), b.get("true_peak_dbtp")
    out["loudness"]["true_peak_dbtp_delta"] = _delta(tp_a, tp_b)
    if tp_b is not None and tp_b > -0.3:
        out["loudness"]["headroom_warning"] = "true peak %.1f dBTP — at/over ceiling" % tp_b

    # spectrum (per 10-band dB), surface the biggest movers
    movers = []
    sa, sb = a.get("spectrum_bands_db") or {}, b.get("spectrum_bands_db") or {}
    band_deltas = {}
    for k in sb:
        d = _delta(sa.get(k), sb.get(k))
        if d is not None:
            band_deltas[k] = d
    out["spectrum"]["band_db_delta"] = band_deltas
    for k, d in sorted(band_deltas.items(), key=lambda kv: -abs(kv[1])):
        if abs(d) >= _FLOOR_BAND_DB:
            movers.append("%s Hz %+.1f dB" % (k.replace("_", "–"), d))
    if movers:
        out["spectrum"]["summary"] = "; ".join(movers[:3])
        verdicts.append(movers[0])

    # tone (centroid → brightness)
    d = _delta(a.get("spectral_centroid_hz"), b.get("spectral_centroid_hz"))
    if d is not None:
        out["tone"]["spectral_centroid_hz_delta"] = d
        if abs(d) >= _FLOOR_CENTROID:
            verdicts.append("%s (%+d Hz centroid)" % ("brighter" if d > 0 else "darker", int(d)))

    # punch (low-band crest)
    ca, cb = a.get("band_crest_db") or {}, b.get("band_crest_db") or {}
    d = _delta(ca.get("low_20_120"), cb.get("low_20_120"))
    if d is not None:
        out["punch"]["low_band_crest_delta"] = d
        if abs(d) >= _FLOOR_CREST:
            verdicts.append("kick %s (low-crest %+.1f dB)" % ("punchier" if d > 0 else "more compressed", d))

    # width
    d = _delta(a.get("stereo_width"), b.get("stereo_width"))
    if d is not None:
        out["width"]["stereo_width_delta"] = d
        if abs(d) >= _FLOOR_WIDTH:
            verdicts.append("%s (width %+.2f)" % ("wider" if d > 0 else "narrower", d))
    lw = (b.get("band_stereo_width") or {}).get("low_20_120")
    if lw is not None and lw > 0.15:
        out["width"]["low_band_warning"] = "bass width %.2f — check mono compatibility" % lw

    # dynamics
    out["dynamics"]["crest_factor_db_delta"] = _delta(a.get("crest_factor_db"), b.get("crest_factor_db"))
    out["dynamics"]["lra_delta"] = _delta(a.get("lufs_range_lra"), b.get("lufs_range_lra"))

    out["headline"] = ("; ".join(verdicts) if verdicts else "no perceptible change above the noise floor")
    return out


def compare_audio(path_a: str, path_b: str, max_seconds: float = 600.0) -> dict:
    """Analyze two files and return ``{a, b, delta}`` — delta is compare_analyses."""
    a = analyze_audio(path_a, max_seconds=max_seconds)
    b = analyze_audio(path_b, max_seconds=max_seconds)
    return {"a": a, "b": b, "delta": compare_analyses(a, b)}
