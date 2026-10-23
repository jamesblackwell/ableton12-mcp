# AbletonMCP/__init__.py  --  Ableton Live 12 Remote Script (Control Surface)
#
# Full-control bridge for the ableton12 MCP server. Runs inside Ableton's
# embedded Python (the Live Object Model is available as the `Live` module and
# via self.song()). A socket server on localhost:9877 accepts length-prefixed
# JSON command frames and dispatches them to handlers.
#
# Two hard rules drive the design:
#   1. The Live API is single-threaded. EVERY command — reads included — is
#      marshaled onto Live's main thread via schedule_message + a reply queue
#      (_run_on_main). This fixes the race that broke upstream device-parameter
#      support (a read on the socket thread concurrent with a main-thread write).
#   2. Socket messages are framed with a 4-byte big-endian length prefix so
#      large responses (browser trees) and back-to-back commands are unambiguous.
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import struct
import threading
import time
import traceback

try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

__version__ = "0.4.0"

DEFAULT_PORT = 9877
HOST = "localhost"
MAX_FRAME = 32 * 1024 * 1024  # 32 MB

# Commands allowed a longer main-thread budget (create/load/delete/big writes).
SLOW_COMMANDS = frozenset([
    "load_browser_item", "create_midi_track", "create_audio_track",
    "create_return_track", "delete_track", "duplicate_track", "create_scene",
    "delete_scene", "duplicate_clip", "add_notes_to_clip",
])


# --------------------------------------------------------------------------- #
# Length-prefixed framing (mirrors src/ableton12_mcp/_framing.py)
# --------------------------------------------------------------------------- #

def _recv_exactly(sock, n):
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(min(65536, n - got))
        if not chunk:
            raise IOError("peer closed connection mid-frame")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def recv_frame(sock):
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME:
        raise ValueError("invalid frame length: %d" % length)
    return json.loads(_recv_exactly(sock, length).decode("utf-8"))


def send_frame(sock, obj):
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def create_instance(c_instance):
    return AbletonMCP(c_instance)


class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live 12."""

    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP (ableton12 v%s) initializing..." % __version__)

        # Captured on Live's main thread so _run_on_main can detect re-entrancy.
        self._main_thread_id = threading.get_ident()
        self._song = self.song()

        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False

        self._dispatch = self._build_dispatch()
        self.start_server()

        self.log_message("AbletonMCP initialized")
        self.show_message("AbletonMCP (ableton12): listening on port %d" % DEFAULT_PORT)

    def disconnect(self):
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")

    # ----------------------------------------------------------------- server

    def start_server(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            self.log_message("Server started on port %d" % DEFAULT_PORT)
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))

    def _server_thread(self):
        try:
            self.server.settimeout(1.0)
            while self.running:
                try:
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    t = threading.Thread(target=self._handle_client, args=(client,))
                    t.daemon = True
                    t.start()
                    self.client_threads.append(t)
                    self.client_threads = [x for x in self.client_threads if x.is_alive()]
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
        except Exception as e:
            self.log_message("Server thread error: " + str(e))

    def _handle_client(self, client):
        """One persistent connection: read frame -> process -> send frame.

        A bad command returns a framed error and never tears down the socket;
        only EOF / framing errors close the connection.
        """
        self.log_message("Client handler started")
        client.settimeout(None)
        try:
            while self.running:
                try:
                    command = recv_frame(client)
                except (ValueError, IOError, OSError) as e:
                    self.log_message("Connection closing: " + str(e))
                    break
                try:
                    response = self._process_command(command)
                except Exception as e:
                    response = {"status": "error", "message": str(e)}
                try:
                    send_frame(client, response)
                except Exception as e:
                    self.log_message("Failed to send response: " + str(e))
                    break
        finally:
            try:
                client.close()
            except Exception:
                pass
            self.log_message("Client handler stopped")

    # ------------------------------------------------------ main-thread bridge

    def _run_on_main(self, fn, timeout=15.0):
        """Run fn() on Live's main thread and block until it returns.

        Returns fn()'s value, or re-raises its exception on the caller thread.
        """
        if threading.get_ident() == self._main_thread_id:
            return fn()

        reply = queue.Queue()

        def task():
            try:
                reply.put(("ok", fn()))
            except BaseException as e:  # never let anything escape the tick
                reply.put(("err", e, traceback.format_exc()))

        self.schedule_message(0, task)  # 0 == next ~100ms tick, on the main thread

        try:
            outcome = reply.get(timeout=timeout)
        except queue.Empty:
            raise Exception(
                "Timeout: Live did not execute the command within %.1fs "
                "(audio engine stalled or a modal dialog is open?)" % timeout
            )
        if outcome[0] == "ok":
            return outcome[1]
        self.log_message("Main-thread task failed:\n" + outcome[2])
        raise outcome[1]

    def _process_command(self, command):
        command_type = command.get("type", "")
        params = command.get("params", {})
        handler = self._dispatch.get(command_type)
        if handler is None:
            return {"status": "error", "message": "Unknown command: " + str(command_type)}
        timeout = 30.0 if command_type in SLOW_COMMANDS else 15.0
        try:
            result = self._run_on_main(lambda: handler(params), timeout)
            return {"status": "success", "result": result}
        except Exception as e:
            self.log_message("Command '%s' error: %s" % (command_type, str(e)))
            self.log_message(traceback.format_exc())
            return {"status": "error", "message": str(e)}

    def _build_dispatch(self):
        return {
            # existing
            "get_session_info": self._get_session_info,
            "get_track_info": self._get_track_info,
            "create_midi_track": self._create_midi_track,
            "set_track_name": self._set_track_name,
            "create_clip": self._create_clip,
            "add_notes_to_clip": self._add_notes_to_clip,
            "set_clip_name": self._set_clip_name,
            "set_tempo": self._set_tempo,
            "fire_clip": self._fire_clip,
            "stop_clip": self._stop_clip,
            "start_playback": self._start_playback,
            "stop_playback": self._stop_playback,
            "load_browser_item": self._load_browser_item,
            "get_browser_tree": lambda p: self.get_browser_tree(p.get("category_type", "all")),
            "get_browser_items_at_path": lambda p: self.get_browser_items_at_path(p.get("path", "")),
            # devices & parameters
            "get_device_parameters": self._get_device_parameters,
            "set_device_parameter": self._set_device_parameter,
            "set_device_enabled": self._set_device_enabled,
            "delete_device": self._delete_device,
            "get_macros": self._get_macros,
            "set_macro": self._set_macro,
            # mixer
            "set_track_volume": self._set_track_volume,
            "set_track_pan": self._set_track_pan,
            "set_track_send": self._set_track_send,
            "set_track_mute": self._set_track_mute,
            "set_track_solo": self._set_track_solo,
            "set_track_arm": self._set_track_arm,
            "set_track_color": self._set_track_color,
            "get_track_meter": self._get_track_meter,
            "get_master_meter": lambda p: self._get_track_meter({"track": "master"}),
            # routing & sidechain
            "get_track_routing": self._get_track_routing,
            "set_track_routing": self._set_track_routing,
            "get_device_routing": self._get_device_routing,
            "set_device_sidechain": self._set_device_sidechain,
            # tracks
            "create_audio_track": self._create_audio_track,
            "create_return_track": self._create_return_track,
            "delete_track": self._delete_track,
            "duplicate_track": self._duplicate_track,
            # clips
            "get_clip_notes": self._get_clip_notes,
            "remove_notes_from_clip": self._remove_notes_from_clip,
            "set_clip_loop": self._set_clip_loop,
            "duplicate_clip": self._duplicate_clip,
            "delete_clip": self._delete_clip,
            "set_clip_color": self._set_clip_color,
            # scenes
            "create_scene": self._create_scene,
            "fire_scene": self._fire_scene,
            "set_scene_name": self._set_scene_name,
            "delete_scene": self._delete_scene,
            # transport
            "set_time_signature": self._set_time_signature,
            "set_metronome": self._set_metronome,
            "set_song_position": self._set_song_position,
            # arrangement / recording (lets a Session loop be captured for export)
            "get_transport": self._get_transport,
            "set_record_mode": self._set_record_mode,
            "set_session_record": self._set_session_record,
            "set_clip_trigger_quantization": self._set_clip_trigger_quantization,
            "set_back_to_arranger": self._set_back_to_arranger,
            # audio clips, clip copy, scenes, scale
            "get_clip_info": self._get_clip_info,
            "set_audio_clip": self._set_audio_clip,
            "copy_clip": self._copy_clip,
            "get_scenes": self._get_scenes,
            "set_scene_color": self._set_scene_color,
            "get_song_scale": self._get_song_scale,
            "set_song_scale": self._set_song_scale,
            # diagnostics
            "health": self._health,
            "reload_script": self._reload_script,
        }

    # ----------------------------------------------------------- validators

    def _get_track(self, track_index):
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index %d out of range (0..%d)"
                             % (track_index, len(self._song.tracks) - 1))
        return self._song.tracks[track_index]

    def _resolve_track(self, spec):
        """Resolve a track from an int index OR a string spec.

        int N        -> regular track N
        "master"     -> the master track (for the master-bus chain)
        "return:N"   -> return track N (for reverb/delay sends); "return" == 0
        numeric str  -> same as int
        This is what lets /master address the master bus & return tracks, which
        live outside song.tracks in the Live Object Model.
        """
        if isinstance(spec, bool):  # bool is an int subclass
            spec = int(spec)
        if isinstance(spec, int):
            return self._get_track(spec)
        s = str(spec).strip().lower()
        if s in ("master", "main"):
            return self._song.master_track
        if s.startswith("return"):
            rest = s[len("return"):].lstrip(":").strip()
            ri = int(rest) if rest else 0
            if ri < 0 or ri >= len(self._song.return_tracks):
                raise IndexError("Return index %d out of range (0..%d)"
                                 % (ri, len(self._song.return_tracks) - 1))
            return self._song.return_tracks[ri]
        if s.lstrip("-").isdigit():
            return self._get_track(int(s))
        raise ValueError("Unknown track spec: %r (use an int, 'master', or 'return:N')" % (spec,))

    def _track_spec(self, params):
        """Pick the track from params: new 'track' (int|str) overrides legacy
        'track_index' (int), so old int-only callers keep working unchanged."""
        if params.get("track") is not None:
            return self._resolve_track(params["track"])
        return self._get_track(params.get("track_index", 0))

    def _get_device(self, track_index, device_index):
        track = self._get_track(track_index)
        return self._device_on(track, device_index)

    def _device_on(self, track, device_index):
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index %d out of range (track has %d devices)"
                             % (device_index, len(track.devices)))
        return track.devices[device_index]

    def _clip_at(self, track_index, clip_index):
        track = self._get_track(track_index)
        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip index %d out of range on track %d" % (clip_index, track_index))
        slot = track.clip_slots[clip_index]
        if not slot.has_clip:
            raise ValueError("No clip in slot %d of track %d" % (clip_index, track_index))
        return slot.clip

    # ------------------------------------------------ parameter helpers

    def _param_display(self, p):
        try:
            return str(p.str_for_value(p.value))
        except Exception:
            try:
                return str(p)
            except Exception:
                return str(p.value)

    def _serialize_parameter(self, index, p):
        # default_value is a Live property that RAISES (not just AttributeError)
        # for parameter types with no default, so getattr's fallback isn't enough.
        try:
            default_value = p.default_value
        except Exception:
            default_value = None
        info = {
            "index": index,
            "name": p.name,
            "value": p.value,
            "min": p.min,
            "max": p.max,
            "is_quantized": bool(p.is_quantized),
            "is_enabled": bool(getattr(p, "is_enabled", True)),
            "default_value": default_value,
            "value_items": None,
            "display_value": self._param_display(p),
        }
        if p.is_quantized and getattr(p, "value_items", None):
            info["value_items"] = [str(it) for it in p.value_items]
        return info

    def _resolve_param(self, device, selector):
        params = device.parameters
        if isinstance(selector, bool):  # bool is an int subclass; treat as index
            selector = int(selector)
        if isinstance(selector, int):
            if selector < 0 or selector >= len(params):
                raise IndexError("Parameter index %d out of range (0..%d)"
                                 % (selector, len(params) - 1))
            return selector, params[selector]
        sel = str(selector).strip().lower()
        for i, p in enumerate(params):
            if p.name.lower() == sel:
                return i, p
        names = ", ".join(p.name for p in params)
        raise ValueError("No parameter named '%s'. Available: %s" % (selector, names))

    def _write_param(self, p, params):
        """Resolve and write a value to a DeviceParameter.

        Returns (enabled, target). Honours value_normalized (0..1) and raw
        value (native units, or value_items string/index for quantized params),
        always clamping into [min, max]. A parameter with is_enabled == False
        is macro-controlled / locked: we do NOT write it and report it back.
        """
        enabled = bool(getattr(p, "is_enabled", True))

        if params.get("value_normalized") is not None:
            x = float(params["value_normalized"])
            x = max(0.0, min(1.0, x))
            if p.is_quantized and getattr(p, "value_items", None):
                target = float(int(round(x * (len(p.value_items) - 1))))
            else:
                target = p.min + x * (p.max - p.min)
        elif params.get("value") is not None:
            raw = params["value"]
            if p.is_quantized and getattr(p, "value_items", None) and isinstance(raw, str):
                items = [str(it) for it in p.value_items]
                lowered = [s.lower() for s in items]
                key = raw.strip().lower()
                if raw in items:
                    target = float(items.index(raw))
                elif key in lowered:
                    target = float(lowered.index(key))
                else:
                    raise ValueError("Value '%s' not in %s" % (raw, items))
            else:
                target = float(raw)
        else:
            raise ValueError("No value or value_normalized provided")

        target = max(p.min, min(p.max, target))
        if enabled:
            p.value = target
        return enabled, target

    def _get_device_type(self, device):
        try:
            if getattr(device, "can_have_drum_pads", False):
                return "drum_machine"
            if getattr(device, "can_have_chains", False):
                return "rack"
            cdn = getattr(device, "class_display_name", "") or ""
            cn = getattr(device, "class_name", "") or ""
            if "instrument" in cdn.lower():
                return "instrument"
            if "audio_effect" in cn.lower():
                return "audio_effect"
            if "midi_effect" in cn.lower():
                return "midi_effect"
            return "unknown"
        except Exception:
            return "unknown"

    # ===================================================================== #
    # Handlers (each runs inside _run_on_main, on Live's main thread)
    # ===================================================================== #

    # ---- session / transport ----

    def _get_session_info(self, params):
        return {
            "tempo": self._song.tempo,
            "signature_numerator": self._song.signature_numerator,
            "signature_denominator": self._song.signature_denominator,
            "track_count": len(self._song.tracks),
            "return_track_count": len(self._song.return_tracks),
            "scene_count": len(self._song.scenes),
            "is_playing": self._song.is_playing,
            "metronome": self._song.metronome,
            "master_track": {
                "name": "Master",
                "volume": self._song.master_track.mixer_device.volume.value,
                "panning": self._song.master_track.mixer_device.panning.value,
            },
        }

    def _set_tempo(self, params):
        self._song.tempo = float(params.get("tempo", 120.0))
        return {"tempo": self._song.tempo}

    def _start_playback(self, params):
        self._song.start_playing()
        return {"playing": self._song.is_playing}

    def _stop_playback(self, params):
        self._song.stop_playing()
        return {"playing": self._song.is_playing}

    def _set_time_signature(self, params):
        self._song.signature_numerator = int(params["numerator"])
        self._song.signature_denominator = int(params["denominator"])
        return {"numerator": self._song.signature_numerator,
                "denominator": self._song.signature_denominator}

    def _set_metronome(self, params):
        self._song.metronome = bool(params.get("on", True))
        return {"metronome": self._song.metronome}

    def _set_song_position(self, params):
        self._song.current_song_time = float(params.get("beats", 0.0))
        return {"current_song_time": self._song.current_song_time}

    # ---- arrangement / recording ----
    # These expose the transport's record state so a Session-view scene can be
    # *recorded into the Arrangement* — the prerequisite for exporting, since
    # Live's Export Audio/Video only renders the Arrangement timeline. The timed
    # orchestration (arm -> fire -> wait one pass -> stop) lives in the MCP
    # server, NOT here: a long wait on Live's main thread would freeze audio/UI.

    def _get_transport(self, params):
        """Transport / record state. Used to save+restore quantization around a
        capture and to poll current_song_time (in beats) while one pass records."""
        s = self._song
        return {
            "is_playing": bool(s.is_playing),
            "record_mode": int(getattr(s, "record_mode", 0)),
            "session_record": bool(getattr(s, "session_record", False)),
            "current_song_time": s.current_song_time,
            "back_to_arranger": int(getattr(s, "back_to_arranger", 0)),
            "clip_trigger_quantization": int(getattr(s, "clip_trigger_quantization", 0)),
            "tempo": s.tempo,
            "signature_numerator": s.signature_numerator,
            "signature_denominator": s.signature_denominator,
        }

    def _set_record_mode(self, params):
        """Arrangement (global) Record — the transport's ● button. With it ON,
        launching Session clips/scenes prints them into the Arrangement."""
        self._song.record_mode = 1 if params.get("on", True) else 0
        return {"record_mode": int(self._song.record_mode)}

    def _set_session_record(self, params):
        """Session Record button — records into Session clip slots. We force this
        OFF during a capture so the material lands in the Arrangement instead."""
        self._song.session_record = bool(params.get("on", True))
        return {"session_record": bool(self._song.session_record)}

    def _set_clip_trigger_quantization(self, params):
        """Global clip-launch quantization (0 = None). Set to 0 for a capture so
        the scene launches immediately at 1.1.1 instead of on the next bar."""
        self._song.clip_trigger_quantization = int(params.get("value", 0))
        return {"clip_trigger_quantization": int(self._song.clip_trigger_quantization)}

    def _set_back_to_arranger(self, params):
        """'Back to Arrangement': 1 means Session clips are overriding Arrangement
        playback. Set 0 after a capture so playback uses the recorded Arrangement."""
        self._song.back_to_arranger = 1 if params.get("on", True) else 0
        return {"back_to_arranger": int(self._song.back_to_arranger)}

    def _health(self, params):
        return {"status": "ok", "version": __version__,
                "tempo": self._song.tempo, "track_count": len(self._song.tracks)}

    # ---- tracks ----

    def _get_track_info(self, params):
        # master/return tracks (via 'track': 'master' / 'return:N') lack
        # clip_slots, audio/midi input flags and arm — guard every such attr.
        track = self._track_spec(params)

        clip_slots = []
        for slot_index, slot in enumerate(getattr(track, "clip_slots", []) or []):
            clip_info = None
            if slot.has_clip:
                clip = slot.clip
                clip_info = {"name": clip.name, "length": clip.length,
                             "is_playing": clip.is_playing, "is_recording": clip.is_recording}
            clip_slots.append({"index": slot_index, "has_clip": slot.has_clip, "clip": clip_info})

        devices = []
        for di, device in enumerate(track.devices):
            devices.append({"index": di, "name": device.name, "class_name": device.class_name,
                            "type": self._get_device_type(device),
                            "is_active": bool(getattr(device, "is_active", True))})

        mixer = track.mixer_device
        return {
            "index": params.get("track", params.get("track_index", 0)),
            "name": track.name,
            "is_audio_track": bool(getattr(track, "has_audio_input", False)),
            "is_midi_track": bool(getattr(track, "has_midi_input", False)),
            "mute": getattr(track, "mute", None),
            "solo": getattr(track, "solo", None),
            "arm": track.arm if getattr(track, "can_be_armed", False) else None,
            "volume": mixer.volume.value,
            "panning": mixer.panning.value,
            "send_count": len(mixer.sends),
            "clip_slots": clip_slots,
            "devices": devices,
        }

    def _create_midi_track(self, params):
        index = params.get("index", -1)
        self._song.create_midi_track(index)
        new_index = len(self._song.tracks) - 1 if index == -1 else index
        t = self._song.tracks[new_index]
        return {"index": new_index, "name": t.name}

    def _create_audio_track(self, params):
        index = params.get("index", -1)
        self._song.create_audio_track(index)
        new_index = len(self._song.tracks) - 1 if index == -1 else index
        t = self._song.tracks[new_index]
        return {"index": new_index, "name": t.name}

    def _create_return_track(self, params):
        self._song.create_return_track()
        idx = len(self._song.return_tracks) - 1
        return {"index": idx, "name": self._song.return_tracks[idx].name}

    def _delete_track(self, params):
        ti = params["track_index"]
        self._get_track(ti)
        self._song.delete_track(ti)
        return {"deleted": ti}

    def _duplicate_track(self, params):
        ti = params["track_index"]
        self._get_track(ti)
        self._song.duplicate_track(ti)
        return {"index": ti + 1}

    def _set_track_name(self, params):
        track = self._get_track(params.get("track_index", 0))
        track.name = params.get("name", "")
        return {"name": track.name}

    def _set_track_volume(self, params):
        track = self._track_spec(params)
        p = track.mixer_device.volume
        self._write_param(p, params)
        return {"track_index": params.get("track", params.get("track_index")), "value": p.value,
                "display_value": self._param_display(p)}

    def _set_track_pan(self, params):
        track = self._track_spec(params)
        p = track.mixer_device.panning
        self._write_param(p, params)
        return {"track_index": params.get("track", params.get("track_index")), "value": p.value,
                "display_value": self._param_display(p)}

    def _set_track_send(self, params):
        track = self._track_spec(params)
        si = params.get("send_index", 0)
        sends = track.mixer_device.sends
        if si < 0 or si >= len(sends):
            raise IndexError("Send index %d out of range (track has %d sends)" % (si, len(sends)))
        p = sends[si]
        self._write_param(p, params)
        return {"send_index": si, "value": p.value, "display_value": self._param_display(p)}

    def _set_track_mute(self, params):
        track = self._get_track(params["track_index"])
        track.mute = bool(params.get("on", True))
        return {"mute": track.mute}

    def _set_track_solo(self, params):
        track = self._get_track(params["track_index"])
        track.solo = bool(params.get("on", True))
        return {"solo": track.solo}

    def _set_track_arm(self, params):
        track = self._get_track(params["track_index"])
        if not track.can_be_armed:
            raise ValueError("Track %d cannot be armed (group/return/master)" % params["track_index"])
        track.arm = bool(params.get("on", True))
        return {"arm": track.arm}

    def _set_track_color(self, params):
        track = self._get_track(params["track_index"])
        track.color = int(params.get("color", 0))
        return {"color": track.color}

    def _get_track_meter(self, params):
        """Output meter levels (0..1) for a track / master / return. These only
        move while audio is playing, so fire a clip/scene first, then sample."""
        track = self._track_spec(params)
        return {
            "track": params.get("track", params.get("track_index", 0)),
            "name": track.name,
            "output_meter_left": getattr(track, "output_meter_left", None),
            "output_meter_right": getattr(track, "output_meter_right", None),
            "output_meter_level": getattr(track, "output_meter_level", None),
            "is_playing": self._song.is_playing,
        }

    # ---- routing & sidechain ----
    # Live exposes routing as RoutingType / RoutingChannel objects: read the
    # available_* tuple, then assign one of its members. Choosing a type
    # rebuilds the channel list, so the type is always written before the channel.
    # Tracks use input_/output_routing_*. Compressor exposes its sidechain source
    # as input_routing_* on the device; Max devices use DeviceIO routing_*.

    MONITORING = ("in", "auto", "off")

    def _routing_names(self, items):
        return [str(getattr(it, "display_name", it)) for it in (items or [])]

    def _routing_name(self, item):
        return None if item is None else str(getattr(item, "display_name", item))

    def _pick_routing(self, available, wanted, what):
        """Match a RoutingType/Channel by display name: exact, then ignoring a
        leading track number ("1-KICK" == "KICK"), then a unique substring."""
        items = list(available or [])
        names = self._routing_names(items)
        key = str(wanted).strip().lower()

        def bare(n):
            head, sep, tail = n.partition("-")
            return tail if sep and head.strip().isdigit() else n

        for test in (lambda n: n.lower() == key,
                     lambda n: bare(n).strip().lower() == key):
            hits = [it for it, n in zip(items, names) if test(n)]
            if len(hits) == 1:
                return hits[0]
        hits = [it for it, n in zip(items, names) if key in n.lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ValueError("%s '%s' is ambiguous: %s"
                             % (what, wanted, [self._routing_name(h) for h in hits]))
        raise ValueError("No %s matching '%s'. Available: %s" % (what, wanted, names))

    def _routing_side(self, obj, type_attr, chan_attr):
        if not hasattr(obj, type_attr):
            return None
        return {
            "type": self._routing_name(getattr(obj, type_attr, None)),
            "channel": self._routing_name(getattr(obj, chan_attr, None)),
            "available_types": self._routing_names(getattr(obj, "available_" + type_attr + "s", [])),
            "available_channels": self._routing_names(getattr(obj, "available_" + chan_attr + "s", [])),
        }

    def _write_routing(self, obj, type_attr, chan_attr, type_name, chan_name, what):
        if type_name is not None:
            setattr(obj, type_attr, self._pick_routing(
                getattr(obj, "available_" + type_attr + "s"), type_name, what + " type"))
        if chan_name is not None:
            setattr(obj, chan_attr, self._pick_routing(
                getattr(obj, "available_" + chan_attr + "s"), chan_name, what + " channel"))

    def _get_track_routing(self, params):
        track = self._track_spec(params)
        out = {
            "track": params.get("track", params.get("track_index", 0)),
            "name": track.name,
            "input": self._routing_side(track, "input_routing_type", "input_routing_channel"),
            "output": self._routing_side(track, "output_routing_type", "output_routing_channel"),
            "monitoring": None,
        }
        state = getattr(track, "current_monitoring_state", None)
        if state is not None and 0 <= int(state) < len(self.MONITORING):
            out["monitoring"] = self.MONITORING[int(state)]
        return out

    def _set_track_routing(self, params):
        track = self._track_spec(params)
        if params.get("input_type") is not None or params.get("input_channel") is not None:
            if not hasattr(track, "input_routing_type"):
                raise ValueError("Track '%s' has no input routing" % track.name)
            self._write_routing(track, "input_routing_type", "input_routing_channel",
                                params.get("input_type"), params.get("input_channel"), "input")
        if params.get("output_type") is not None or params.get("output_channel") is not None:
            self._write_routing(track, "output_routing_type", "output_routing_channel",
                                params.get("output_type"), params.get("output_channel"), "output")
        if params.get("monitoring") is not None:
            mode = str(params["monitoring"]).strip().lower()
            if mode not in self.MONITORING:
                raise ValueError("monitoring must be one of %s" % (self.MONITORING,))
            if not hasattr(track, "current_monitoring_state"):
                raise ValueError("Track '%s' has no monitoring switch" % track.name)
            track.current_monitoring_state = self.MONITORING.index(mode)
        return self._get_track_routing(params)

    def _device_inputs(self, device):
        """Return (label, obj, type_attr, chan_attr) for each routable device input."""
        found = []
        if hasattr(device, "input_routing_type"):
            found.append(("sidechain", device, "input_routing_type", "input_routing_channel"))
        for i, io in enumerate(getattr(device, "audio_inputs", None) or []):
            if hasattr(io, "routing_type"):
                found.append(("audio_input:%d" % i, io, "routing_type", "routing_channel"))
        return found

    def _sidechain_switch(self, device):
        for p in device.parameters:
            if p.name.lower() in ("s/c on", "sidechain on", "sidechain"):
                return p
        return None

    def _get_device_routing(self, params):
        device = self._device_on(self._track_spec(params), params["device_index"])
        inputs = []
        for label, obj, type_attr, chan_attr in self._device_inputs(device):
            side = self._routing_side(obj, type_attr, chan_attr)
            side["input"] = label
            inputs.append(side)
        switch = self._sidechain_switch(device)
        return {
            "device_index": params["device_index"],
            "device_name": device.name,
            "class_name": device.class_name,
            "inputs": inputs,
            "sidechain_on": None if switch is None else bool(switch.value),
            "note": None if inputs else
                    "Live's API exposes no sidechain routing for this device",
        }

    def _set_device_sidechain(self, params):
        device = self._device_on(self._track_spec(params), params["device_index"])
        inputs = self._device_inputs(device)
        if not inputs:
            raise ValueError("Device '%s' (%s) has no routable sidechain input in Live's API"
                             % (device.name, device.class_name))
        label = params.get("input")
        target = inputs[0]
        if label is not None:
            matches = [i for i in inputs if i[0] == label]
            if not matches:
                raise ValueError("No input '%s'. Available: %s" % (label, [i[0] for i in inputs]))
            target = matches[0]
        _, obj, type_attr, chan_attr = target
        self._write_routing(obj, type_attr, chan_attr,
                            params.get("source"), params.get("tap"), "sidechain")
        if params.get("enabled") is not None:
            switch = self._sidechain_switch(device)
            if switch is None:
                raise ValueError("Device '%s' has no sidechain on/off parameter" % device.name)
            switch.value = switch.max if params["enabled"] else switch.min
        return self._get_device_routing(params)

    # ---- audio clips, clip copy, scenes, scale ----

    WARP_MODES = ("Beats", "Tones", "Texture", "Re-Pitch", "Complex", "REX", "Complex Pro")
    NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

    def _get_clip_info(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        info = {
            "name": clip.name, "length": clip.length, "is_audio_clip": bool(clip.is_audio_clip),
            "looping": clip.looping, "loop_start": clip.loop_start, "loop_end": clip.loop_end,
            "start_marker": clip.start_marker, "end_marker": clip.end_marker,
            "muted": bool(getattr(clip, "muted", False)), "color": clip.color,
        }
        if clip.is_audio_clip:
            mode = int(clip.warp_mode)
            info.update({
                "file_path": getattr(clip, "file_path", None),
                "warping": bool(clip.warping),
                "warp_mode": self.WARP_MODES[mode] if 0 <= mode < len(self.WARP_MODES) else mode,
                "pitch_coarse": clip.pitch_coarse, "pitch_fine": clip.pitch_fine,
                "gain": clip.gain, "gain_display": getattr(clip, "gain_display_string", None),
            })
        return info

    def _set_audio_clip(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        if not clip.is_audio_clip:
            raise ValueError("Clip '%s' is not an audio clip" % clip.name)
        if params.get("warping") is not None:
            clip.warping = bool(params["warping"])
        if params.get("warp_mode") is not None:
            mode = params["warp_mode"]
            if isinstance(mode, str):
                names = [m.lower() for m in self.WARP_MODES]
                if mode.strip().lower() not in names:
                    raise ValueError("warp_mode must be one of %s" % (self.WARP_MODES,))
                mode = names.index(mode.strip().lower())
            if int(mode) not in [int(m) for m in clip.available_warp_modes]:
                raise ValueError("Warp mode %s not available for this clip" % self.WARP_MODES[int(mode)])
            clip.warp_mode = int(mode)
        if params.get("pitch_coarse") is not None:
            clip.pitch_coarse = int(params["pitch_coarse"])
        if params.get("pitch_fine") is not None:
            clip.pitch_fine = float(params["pitch_fine"])
        if params.get("gain") is not None:
            clip.gain = max(0.0, min(1.0, float(params["gain"])))
        return self._get_clip_info(params)

    def _copy_clip(self, params):
        """Copy a Session clip into any empty slot (same or another track of the
        same type); move=True deletes the source afterwards."""
        src_track = self._get_track(params["track_index"])
        src = src_track.clip_slots[params["clip_index"]]
        if not src.has_clip:
            raise ValueError("No clip in source slot")
        dst_track = self._get_track(params.get("to_track_index", params["track_index"]))
        ti = params["to_clip_index"]
        if ti < 0 or ti >= len(dst_track.clip_slots):
            raise IndexError("Target slot %d out of range" % ti)
        dst = dst_track.clip_slots[ti]
        if dst.has_clip:
            raise ValueError("Target slot %d on '%s' is not empty" % (ti, dst_track.name))
        src.duplicate_clip_to(dst)
        if params.get("move"):
            src.delete_clip()
        return {"copied": dst.clip.name, "to_track": dst_track.name, "to_clip_index": ti,
                "moved": bool(params.get("move"))}

    def _get_scenes(self, params):
        out = []
        for i, sc in enumerate(self._song.scenes):
            out.append({"index": i, "name": sc.name, "color": sc.color,
                        "is_empty": bool(getattr(sc, "is_empty", False))})
        return {"scenes": out}

    def _set_scene_color(self, params):
        sc = self._song.scenes[params["scene_index"]]
        sc.color = int(params.get("color", 0))
        return {"color": sc.color}

    def _get_song_scale(self, params):
        s = self._song
        root = int(getattr(s, "root_note", 0))
        return {"root_note": root, "root_name": self.NOTE_NAMES[root % 12],
                "scale_name": str(getattr(s, "scale_name", "")),
                "scale_mode": bool(getattr(s, "scale_mode", False))}

    def _set_song_scale(self, params):
        s = self._song
        if params.get("root") is not None:
            root = params["root"]
            if isinstance(root, str):
                root = self.NOTE_NAMES.index(root.strip().upper())
            s.root_note = int(root) % 12
        if params.get("scale_name") is not None:
            s.scale_name = str(params["scale_name"])
        if params.get("scale_mode") is not None:
            s.scale_mode = bool(params["scale_mode"])
        return self._get_song_scale(params)

    def _reload_script(self, params):
        """Re-import this file and swap the running instance onto the new class,
        so handler changes load without restarting Live. The socket server keeps
        running; restart Live if __init__/server code itself changed."""
        import importlib
        import sys
        module = sys.modules[type(self).__module__]
        old = __version__
        module = importlib.reload(module)
        self.__class__ = module.AbletonMCP
        self._dispatch = self._build_dispatch()
        return {"reloaded": True, "from_version": old, "version": module.__version__}

    # ---- devices & parameters ----

    def _get_device_parameters(self, params):
        device = self._device_on(self._track_spec(params), params["device_index"])
        out = [self._serialize_parameter(i, p) for i, p in enumerate(device.parameters)]
        return {"track_index": params.get("track", params.get("track_index")),
                "device_index": params["device_index"],
                "device_name": device.name, "parameters": out}

    def _set_device_parameter(self, params):
        device = self._device_on(self._track_spec(params), params["device_index"])
        idx, p = self._resolve_param(device, params.get("parameter"))
        enabled, target = self._write_param(p, params)
        info = self._serialize_parameter(idx, p)
        info["applied"] = bool(enabled and abs(p.value - target) < 1e-4)
        info["enabled"] = enabled
        if not enabled:
            info["message"] = "parameter not writable (controlled by a macro / disabled)"
        return info

    def _set_device_enabled(self, params):
        device = self._device_on(self._track_spec(params), params["device_index"])
        on = bool(params.get("on", True))
        target = None
        for p in device.parameters:
            if p.name == "Device On":
                target = p
                break
        if target is None and len(device.parameters) > 0:
            target = device.parameters[0]
        if target is None:
            raise ValueError("Device '%s' has no on/off parameter" % device.name)
        target.value = 1.0 if on else 0.0
        return {"device": device.name, "on": bool(target.value)}

    def _delete_device(self, params):
        track = self._track_spec(params)
        di = params["device_index"]
        if di < 0 or di >= len(track.devices):
            raise IndexError("Device index %d out of range" % di)
        track.delete_device(di)
        return {"deleted": di}

    def _get_macros(self, params):
        device = self._get_device(params["track_index"], params["device_index"])
        if not getattr(device, "can_have_chains", False):
            raise ValueError("Device '%s' is not a Rack (no macros)" % device.name)
        out = []
        for i, p in enumerate(device.parameters):
            if p.name == "Device On":
                continue
            out.append(self._serialize_parameter(i, p))
        return {"track_index": params["track_index"], "device_index": params["device_index"],
                "device_name": device.name, "macros": out}

    def _set_macro(self, params):
        device = self._get_device(params["track_index"], params["device_index"])
        if not getattr(device, "can_have_chains", False):
            raise ValueError("Device '%s' is not a Rack" % device.name)
        idx, p = self._resolve_param(device, params.get("macro"))
        enabled, target = self._write_param(p, params)
        info = self._serialize_parameter(idx, p)
        info["applied"] = bool(enabled and abs(p.value - target) < 1e-4)
        return info

    # ---- clips ----

    def _create_clip(self, params):
        track = self._get_track(params.get("track_index", 0))
        ci = params.get("clip_index", 0)
        if ci < 0 or ci >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        slot = track.clip_slots[ci]
        if slot.has_clip:
            raise Exception("Clip slot already has a clip")
        slot.create_clip(float(params.get("length", 4.0)))
        return {"name": slot.clip.name, "length": slot.clip.length}

    def _add_notes_to_clip(self, params):
        clip = self._clip_at(params.get("track_index", 0), params.get("clip_index", 0))
        live_notes = []
        for note in params.get("notes", []):
            live_notes.append((
                int(note.get("pitch", 60)),
                float(note.get("start_time", 0.0)),
                float(note.get("duration", 0.25)),
                int(note.get("velocity", 100)),
                bool(note.get("mute", False)),
            ))
        clip.set_notes(tuple(live_notes))
        return {"note_count": len(live_notes)}

    def _get_clip_notes(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        notes = []
        if hasattr(clip, "get_notes_extended"):
            try:
                for n in clip.get_notes_extended(0, 128, 0.0, clip.length):
                    notes.append({"note_id": getattr(n, "note_id", None), "pitch": n.pitch,
                                  "start_time": n.start_time, "duration": n.duration,
                                  "velocity": n.velocity, "mute": n.mute})
                return {"note_count": len(notes), "notes": notes}
            except Exception as e:
                self.log_message("get_notes_extended failed (%s); using legacy" % e)
        for (pitch, start, dur, vel, mute) in clip.get_notes(0.0, 0, clip.length, 128):
            notes.append({"pitch": pitch, "start_time": start, "duration": dur,
                          "velocity": vel, "mute": mute})
        return {"note_count": len(notes), "notes": notes}

    def _remove_notes_from_clip(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        from_time = float(params.get("from_time", 0.0))
        time_span = params.get("time_span", None)
        time_span = clip.length if time_span is None else float(time_span)
        from_pitch = int(params.get("from_pitch", 0))
        pitch_span = int(params.get("pitch_span", 128))
        if hasattr(clip, "remove_notes_extended"):
            clip.remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
        else:
            clip.remove_notes(from_time, from_pitch, time_span, pitch_span)
        return {"removed": True}

    def _set_clip_loop(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        if "looping" in params:
            clip.looping = bool(params["looping"])
        # set end before start so an extending move never crosses itself
        if "loop_end" in params:
            clip.loop_end = float(params["loop_end"])
        if "loop_start" in params:
            clip.loop_start = float(params["loop_start"])
        if "end_marker" in params:
            clip.end_marker = float(params["end_marker"])
        if "start_marker" in params:
            clip.start_marker = float(params["start_marker"])
        return {"looping": clip.looping, "loop_start": clip.loop_start, "loop_end": clip.loop_end}

    def _set_clip_name(self, params):
        clip = self._clip_at(params.get("track_index", 0), params.get("clip_index", 0))
        clip.name = params.get("name", "")
        return {"name": clip.name}

    def _set_clip_color(self, params):
        clip = self._clip_at(params["track_index"], params["clip_index"])
        clip.color = int(params.get("color", 0))
        return {"color": clip.color}

    def _duplicate_clip(self, params):
        track = self._get_track(params["track_index"])
        ci = params["clip_index"]
        if ci < 0 or ci >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        if not track.clip_slots[ci].has_clip:
            raise ValueError("No clip to duplicate")
        track.duplicate_clip_slot(ci)
        return {"target_index": ci + 1}

    def _delete_clip(self, params):
        track = self._get_track(params["track_index"])
        ci = params["clip_index"]
        if ci < 0 or ci >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        slot = track.clip_slots[ci]
        if not slot.has_clip:
            raise ValueError("No clip in slot")
        slot.delete_clip()
        return {"deleted": True}

    def _fire_clip(self, params):
        track = self._get_track(params.get("track_index", 0))
        ci = params.get("clip_index", 0)
        if ci < 0 or ci >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        slot = track.clip_slots[ci]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        slot.fire()
        return {"fired": True}

    def _stop_clip(self, params):
        track = self._get_track(params.get("track_index", 0))
        ci = params.get("clip_index", 0)
        if ci < 0 or ci >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        track.clip_slots[ci].stop()
        return {"stopped": True}

    # ---- scenes ----

    def _create_scene(self, params):
        index = params.get("index", -1)
        self._song.create_scene(index)
        new_index = len(self._song.scenes) - 1 if index == -1 else index
        return {"index": new_index}

    def _fire_scene(self, params):
        si = params["scene_index"]
        if si < 0 or si >= len(self._song.scenes):
            raise IndexError("Scene index out of range")
        self._song.scenes[si].fire()
        return {"fired": si}

    def _set_scene_name(self, params):
        si = params["scene_index"]
        if si < 0 or si >= len(self._song.scenes):
            raise IndexError("Scene index out of range")
        self._song.scenes[si].name = params.get("name", "")
        return {"name": self._song.scenes[si].name}

    def _delete_scene(self, params):
        si = params["scene_index"]
        if si < 0 or si >= len(self._song.scenes):
            raise IndexError("Scene index out of range")
        self._song.delete_scene(si)
        return {"deleted": si}

    # ---- browser (load + walk; reused from the proven upstream script) ----

    def _load_browser_item(self, params):
        item_uri = params.get("item_uri", "")
        track = self._track_spec(params)  # int | 'master' | 'return:N'
        app = self.application()
        item = self._find_browser_item_by_uri(app.browser, item_uri)
        if not item:
            raise ValueError("Browser item with URI '%s' not found" % item_uri)
        self._song.view.selected_track = track
        app.browser.load_item(item)
        return {"loaded": True, "item_name": item.name, "track_name": track.name,
                "uri": item_uri, "devices_after": [d.name for d in track.devices]}

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        try:
            if hasattr(browser_or_item, "uri") and browser_or_item.uri == uri:
                return browser_or_item
            if current_depth >= max_depth:
                return None
            if hasattr(browser_or_item, "instruments"):
                for category in [browser_or_item.instruments, browser_or_item.sounds,
                                 browser_or_item.drums, browser_or_item.audio_effects,
                                 browser_or_item.midi_effects]:
                    found = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if found:
                        return found
                return None
            if hasattr(browser_or_item, "children") and browser_or_item.children:
                for child in browser_or_item.children:
                    found = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if found:
                        return found
            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: " + str(e))
            return None

    def get_browser_tree(self, category_type="all"):
        app = self.application()
        if not app or not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError("Browser is not available in the Live application")
        browser_attrs = [a for a in dir(app.browser) if not a.startswith("_")]
        result = {"type": category_type, "categories": [], "available_categories": browser_attrs}

        def process_item(item):
            if not item:
                return None
            return {
                "name": item.name if hasattr(item, "name") else "Unknown",
                "is_folder": hasattr(item, "children") and bool(item.children),
                "is_device": hasattr(item, "is_device") and item.is_device,
                "is_loadable": hasattr(item, "is_loadable") and item.is_loadable,
                "uri": item.uri if hasattr(item, "uri") else None,
                "children": [],
            }

        wanted = [
            ("instruments", "Instruments"), ("sounds", "Sounds"), ("drums", "Drums"),
            ("audio_effects", "Audio Effects"), ("midi_effects", "MIDI Effects"),
        ]
        for attr, label in wanted:
            if (category_type == "all" or category_type == attr) and hasattr(app.browser, attr):
                try:
                    cat = process_item(getattr(app.browser, attr))
                    if cat:
                        cat["name"] = label
                        result["categories"].append(cat)
                except Exception as e:
                    self.log_message("Error processing %s: %s" % (attr, str(e)))
        return result

    def get_browser_items_at_path(self, path):
        app = self.application()
        if not app or not hasattr(app, "browser") or app.browser is None:
            raise RuntimeError("Browser is not available in the Live application")
        browser_attrs = [a for a in dir(app.browser) if not a.startswith("_")]
        path_parts = path.split("/")
        if not path_parts:
            raise ValueError("Invalid path")

        root = path_parts[0].lower()
        standard = {"instruments", "sounds", "drums", "audio_effects", "midi_effects"}
        current = None
        if root in standard and hasattr(app.browser, root):
            current = getattr(app.browser, root)
        else:
            for attr in browser_attrs:
                if attr.lower() == root:
                    current = getattr(app.browser, attr)
                    break
            if current is None:
                return {"path": path, "error": "Unknown or unavailable category: %s" % root,
                        "available_categories": browser_attrs, "items": []}

        for i in range(1, len(path_parts)):
            part = path_parts[i]
            if not part:
                continue
            if not hasattr(current, "children"):
                return {"path": path, "error": "Item at '%s' has no children"
                        % "/".join(path_parts[:i]), "items": []}
            found = False
            for child in current.children:
                if hasattr(child, "name") and child.name.lower() == part.lower():
                    current = child
                    found = True
                    break
            if not found:
                return {"path": path, "error": "Path part '%s' not found" % part, "items": []}

        items = []
        if hasattr(current, "children"):
            for child in current.children:
                items.append({
                    "name": child.name if hasattr(child, "name") else "Unknown",
                    "is_folder": hasattr(child, "children") and bool(child.children),
                    "is_device": hasattr(child, "is_device") and child.is_device,
                    "is_loadable": hasattr(child, "is_loadable") and child.is_loadable,
                    "uri": child.uri if hasattr(child, "uri") else None,
                })
        return {
            "path": path,
            "name": current.name if hasattr(current, "name") else "Unknown",
            "uri": current.uri if hasattr(current, "uri") else None,
            "is_folder": hasattr(current, "children") and bool(current.children),
            "is_loadable": hasattr(current, "is_loadable") and current.is_loadable,
            "items": items,
        }
