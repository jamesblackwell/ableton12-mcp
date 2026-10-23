"""Ableton Live 12 MCP server.

A drop-in superset of the upstream ``ahujasid/ableton-mcp`` server: it keeps the
16 original tools (identical names, params and return strings, so existing
slash-commands and permissions work unchanged) and adds full device-parameter,
mixer, track, clip, scene and transport control.

Talks to the Ableton Remote Script over TCP ``localhost:9877`` using
length-prefixed JSON frames (see ``_framing``). Every command is executed on
Live's main thread by the Remote Script, so the request/response here is fully
synchronous: when ``send_command`` returns, the Live operation is done.
"""

import json
import logging
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from mcp.server.fastmcp import Context, FastMCP

from . import _host
from ._framing import recv_frame, send_frame

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("Ableton12MCP")

HOST = "localhost"
PORT = 9877

# Commands that create/load/delete structure, or move large note payloads, can
# take longer; give them a generous read timeout. Everything else is quick.
SLOW_COMMANDS = {
    "load_browser_item",
    "create_midi_track",
    "create_audio_track",
    "create_return_track",
    "delete_track",
    "duplicate_track",
    "create_scene",
    "delete_scene",
    "duplicate_clip",
    "add_notes_to_clip",
}


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: Optional[socket.socket] = None

    def connect(self) -> bool:
        if self.sock:
            return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton: {e}")
            self.sock = None
            return False

    def disconnect(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {e}")
            finally:
                self.sock = None

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Any:
        """Send one command frame and return the parsed ``result``."""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {"type": command_type, "params": params or {}}
        timeout = 30.0 if command_type in SLOW_COMMANDS else 15.0

        try:
            logger.info(f"Sending command: {command_type}")
            self.sock.settimeout(timeout)
            send_frame(self.sock, command)
            response = recv_frame(self.sock)
        except socket.timeout:
            logger.error("Socket timeout waiting for Ableton response")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError, ValueError) as e:
            logger.error(f"Socket error talking to Ableton: {e}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {e}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {e}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {e}")

        if response.get("status") == "error":
            message = response.get("message", "Unknown error from Ableton")
            logger.error(f"Ableton error: {message}")
            raise Exception(message)
        return response.get("result", {})


_ableton_connection: Optional[AbletonConnection] = None


def get_ableton_connection() -> AbletonConnection:
    """Return a validated persistent connection, reconnecting up to 3 times."""
    global _ableton_connection

    if _ableton_connection is not None:
        try:
            _ableton_connection.sock.settimeout(1.0)
            _ableton_connection.sock.sendall(b"")  # cheap liveness probe (no frame)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {e}")
            try:
                _ableton_connection.disconnect()
            except Exception:
                pass
            _ableton_connection = None

    for attempt in range(1, 4):
        try:
            logger.info(f"Connecting to Ableton (attempt {attempt}/3)...")
            conn = AbletonConnection(host=HOST, port=PORT)
            if conn.connect():
                conn.send_command("get_session_info")  # validate
                _ableton_connection = conn
                logger.info("Connection validated")
                return _ableton_connection
        except Exception as e:
            logger.error(f"Connection attempt {attempt} failed: {e}")
        if attempt < 3:
            time.sleep(1.0)

    raise Exception(
        "Could not connect to Ableton. Make sure Live is running, the AbletonMCP "
        "control surface is selected (In/Out = None), and Live was restarted after "
        "any script change."
    )


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    logger.info("Ableton12 MCP server starting up")
    try:
        try:
            get_ableton_connection()
            logger.info("Connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {e}")
        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("Ableton12 MCP server shut down")


# Keep the server NAME "AbletonMCP" so tools surface as mcp__AbletonMCP__* and
# existing commands/permissions keep working with zero edits.
mcp = FastMCP("AbletonMCP", lifespan=server_lifespan)


def _err(action: str, e: Exception) -> str:
    logger.error(f"Error {action}: {e}")
    return f"Error {action}: {e}"


def _track_key(track_index: int, track: Union[int, str, None]) -> Dict[str, Any]:
    """Params for a track target. A ``track`` spec (int | 'master' | 'return:N')
    overrides the legacy int ``track_index`` so master-bus / return-track work
    reuses the same tools. Old int-only callers pass track=None and are unchanged."""
    return {"track": track} if track is not None else {"track_index": track_index}


# --------------------------------------------------------------------------- #
# Existing tools (names, params and return strings preserved for drop-in compat)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session (tempo, time
    signature, track/return/scene counts, master mixer)."""
    try:
        return json.dumps(get_ableton_connection().send_command("get_session_info"), indent=2)
    except Exception as e:
        return _err("getting session info", e)


@mcp.tool()
def get_track_info(ctx: Context, track_index: int = 0, track: Union[int, str, None] = None) -> str:
    """Get detailed info about a track: name, mute/solo/arm, volume/pan, clip
    slots, and the ordered list of devices (with their indices).

    Pass track='master' or track='return:0' to inspect the master bus or a
    return track (those have no clip slots)."""
    try:
        result = get_ableton_connection().send_command("get_track_info", _track_key(track_index, track))
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting track info", e)


@mcp.tool()
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """Create a new MIDI track. index=-1 appends at the end. Re-read
    get_session_info afterwards to learn the real new index."""
    try:
        result = get_ableton_connection().send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')} (index {result.get('index')})"
    except Exception as e:
        return _err("creating MIDI track", e)


@mcp.tool()
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """Set the name of a track."""
    try:
        result = get_ableton_connection().send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        return _err("setting track name", e)


@mcp.tool()
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """Create a new empty MIDI clip. length is in BEATS (not bars)."""
    try:
        get_ableton_connection().send_command(
            "create_clip", {"track_index": track_index, "clip_index": clip_index, "length": length}
        )
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        return _err("creating clip", e)


@mcp.tool()
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
) -> str:
    """Add MIDI notes to a clip (additive — call repeatedly to build large clips).

    Each note dict: pitch (0-127), start_time (beats), duration (beats),
    velocity (1-127), mute (bool, optional)."""
    try:
        get_ableton_connection().send_command(
            "add_notes_to_clip", {"track_index": track_index, "clip_index": clip_index, "notes": notes}
        )
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("adding notes to clip", e)


@mcp.tool()
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """Set the name of a clip."""
    try:
        get_ableton_connection().send_command(
            "set_clip_name", {"track_index": track_index, "clip_index": clip_index, "name": name}
        )
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        return _err("setting clip name", e)


@mcp.tool()
def set_tempo(ctx: Context, tempo: float) -> str:
    """Set the session tempo in BPM."""
    try:
        get_ableton_connection().send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        return _err("setting tempo", e)


@mcp.tool()
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str,
                              track: Union[int, str, None] = None) -> str:
    """Load an instrument or effect device onto a track by its browser URI.

    Discover URIs with get_browser_tree / get_browser_items_at_path. After
    loading, use get_device_parameters + set_device_parameter to dial it in.
    Pass track='master' or track='return:0' to load onto the master bus or a
    return track (e.g. a Reverb on a return, or the master-bus chain)."""
    try:
        params = {"item_uri": uri}
        params.update(_track_key(track_index, track))
        result = get_ableton_connection().send_command("load_browser_item", params)
        where = track if track is not None else track_index
        if result.get("loaded", False):
            devices = result.get("devices_after", [])
            return f"Loaded '{uri}' on track {where}. Devices on track: {', '.join(devices)}"
        return f"Failed to load device with URI '{uri}'"
    except Exception as e:
        return _err("loading instrument/effect", e)


@mcp.tool()
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """Load a Drum Rack, then load the first loadable kit found at kit_path."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {"track_index": track_index, "item_uri": rack_uri})
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"

        kit_result = ableton.send_command("get_browser_items_at_path", {"path": kit_path})
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"

        loadable = [it for it in kit_result.get("items", []) if it.get("is_loadable", False)]
        if not loadable:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"

        ableton.send_command("load_browser_item", {"track_index": track_index, "item_uri": loadable[0].get("uri")})
        return f"Loaded drum rack and kit '{loadable[0].get('name')}' on track {track_index}"
    except Exception as e:
        return _err("loading drum kit", e)


@mcp.tool()
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """Start playing a clip (launches the clip slot)."""
    try:
        get_ableton_connection().send_command("fire_clip", {"track_index": track_index, "clip_index": clip_index})
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("firing clip", e)


@mcp.tool()
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """Stop a playing clip slot."""
    try:
        get_ableton_connection().send_command("stop_clip", {"track_index": track_index, "clip_index": clip_index})
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("stopping clip", e)


@mcp.tool()
def start_playback(ctx: Context) -> str:
    """Start the session transport."""
    try:
        get_ableton_connection().send_command("start_playback")
        return "Started playback"
    except Exception as e:
        return _err("starting playback", e)


@mcp.tool()
def stop_playback(ctx: Context) -> str:
    """Stop the session transport."""
    try:
        get_ableton_connection().send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        return _err("stopping playback", e)


@mcp.tool()
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """Get the top-level browser categories ('all', 'instruments', 'sounds',
    'drums', 'audio_effects', 'midi_effects'). Use get_browser_items_at_path to
    drill in and find loadable URIs."""
    try:
        result = get_ableton_connection().send_command("get_browser_tree", {"category_type": category_type})
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting browser tree", e)


@mcp.tool()
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """List browser items at a path like 'instruments/Operator' or
    'drums'. Each item reports name, is_folder, is_loadable and uri."""
    try:
        result = get_ableton_connection().send_command("get_browser_items_at_path", {"path": path})
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting browser items at path", e)


# --------------------------------------------------------------------------- #
# Devices & parameters (the headline new capability — "change the tone")
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_device_parameters(ctx: Context, track_index: int, device_index: int,
                          track: Union[int, str, None] = None) -> str:
    """List every automatable parameter of a device on a track.

    Returns JSON with the device name and a list of parameters, each:
    index, name, value, min, max, is_quantized, value_items (enum labels for
    quantized params, else null), display_value (e.g. '440 Hz', 'Lowpass'),
    default_value, is_enabled. Use this to discover what you can change, then
    call set_device_parameter. track='master'/'return:N' targets those tracks."""
    try:
        params = {"device_index": device_index}
        params.update(_track_key(track_index, track))
        result = get_ableton_connection().send_command("get_device_parameters", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting device parameters", e)


@mcp.tool()
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter: Union[int, str],
    value: Union[float, str, None] = None,
    value_normalized: Optional[float] = None,
    track: Union[int, str, None] = None,
    display_target: Optional[float] = None,
) -> str:
    """Set one device parameter — this is how you change a sound's tone.

    - parameter: the parameter index (int) OR its exact name (str, case-insensitive).
    - value: native units. For a continuous param a float in [min, max]
      (e.g. a frequency/dB/ms). For a quantized param, either an integer index
      into value_items or the exact value_items label string (e.g. "Lowpass").
    - value_normalized: alternative to value, a 0.0-1.0 fraction mapped to
      min + x*(max-min); for quantized params it picks the value_items index.

    - display_target: the number as Live displays it, in the display's own
      units (dB, Hz, ms, %, ratio), e.g. -20 for "-20 dB" or 150 for "150 ms".
      Use this for continuous params whose raw value is not in display units.

    Provide exactly one of value / value_normalized / display_target. The reply reports the
    resulting display_value and whether the write applied (a parameter driven
    by a macro is not writable)."""
    try:
        params: Dict[str, Any] = {"device_index": device_index, "parameter": parameter}
        params.update(_track_key(track_index, track))
        if display_target is not None:
            params["display_target"] = display_target
        elif value_normalized is not None:
            params["value_normalized"] = value_normalized
        elif value is not None:
            params["value"] = value
        else:
            return "Error: provide 'value', 'value_normalized' or 'display_target'"
        r = get_ableton_connection().send_command("set_device_parameter", params)
        applied = "" if r.get("applied", True) else f" (NOT applied: {r.get('message')})"
        where = track if track is not None else track_index
        return (f"Set '{r.get('name')}' = {r.get('value')} ({r.get('display_value')}) "
                f"on track {where} device {device_index}{applied}")
    except Exception as e:
        return _err("setting device parameter", e)


@mcp.tool()
def set_device_enabled(ctx: Context, track_index: int, device_index: int, on: bool,
                       track: Union[int, str, None] = None) -> str:
    """Turn a device on or off (its bypass / 'Device On' switch).
    track='master'/'return:N' targets those tracks."""
    try:
        params = {"device_index": device_index, "on": on}
        params.update(_track_key(track_index, track))
        r = get_ableton_connection().send_command("set_device_enabled", params)
        where = track if track is not None else track_index
        return f"Device '{r.get('device')}' on track {where} is now {'on' if r.get('on') else 'off'}"
    except Exception as e:
        return _err("setting device enabled", e)


@mcp.tool()
def delete_device(ctx: Context, track_index: int, device_index: int,
                  track: Union[int, str, None] = None) -> str:
    """Delete a device from a track. track='master'/'return:N' targets those tracks."""
    try:
        params = {"device_index": device_index}
        params.update(_track_key(track_index, track))
        get_ableton_connection().send_command("delete_device", params)
        where = track if track is not None else track_index
        return f"Deleted device {device_index} from track {where}"
    except Exception as e:
        return _err("deleting device", e)


@mcp.tool()
def get_macros(ctx: Context, track_index: int, device_index: int) -> str:
    """List the macro controls of a Rack device (Instrument/Audio/MIDI/Drum
    Rack). Returns JSON like get_device_parameters but limited to the rack's
    macros. Set them with set_macro (or set_device_parameter)."""
    try:
        result = get_ableton_connection().send_command(
            "get_macros", {"track_index": track_index, "device_index": device_index}
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting macros", e)


@mcp.tool()
def set_macro(
    ctx: Context,
    track_index: int,
    device_index: int,
    macro: Union[int, str],
    value: Union[float, str, None] = None,
    value_normalized: Optional[float] = None,
) -> str:
    """Set a Rack macro by index (0-based) or name. Same value/value_normalized
    semantics as set_device_parameter."""
    try:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "macro": macro,
        }
        if value_normalized is not None:
            params["value_normalized"] = value_normalized
        elif value is not None:
            params["value"] = value
        else:
            return "Error: provide either 'value' or 'value_normalized'"
        r = get_ableton_connection().send_command("set_macro", params)
        return f"Set macro '{r.get('name')}' = {r.get('value')} ({r.get('display_value')})"
    except Exception as e:
        return _err("setting macro", e)


# --------------------------------------------------------------------------- #
# Mixer
# --------------------------------------------------------------------------- #

@mcp.tool()
def set_track_volume(
    ctx: Context, track_index: int, value: Optional[float] = None,
    value_normalized: Optional[float] = None, track: Union[int, str, None] = None,
) -> str:
    """Set a track's volume fader (0.0-1.0). NOTE: this is the fader position,
    not dB — ~0.85 ≈ 0 dB (unity), 1.0 ≈ +6 dB. value and value_normalized are
    equivalent here (the fader range is already 0..1). Reply reports the dB.
    track='master' sets the master fader."""
    try:
        params: Dict[str, Any] = {}
        params.update(_track_key(track_index, track))
        if value_normalized is not None:
            params["value_normalized"] = value_normalized
        elif value is not None:
            params["value"] = value
        else:
            return "Error: provide either 'value' or 'value_normalized'"
        r = get_ableton_connection().send_command("set_track_volume", params)
        where = track if track is not None else track_index
        return f"Track {where} volume = {r.get('value'):.3f} ({r.get('display_value')})"
    except Exception as e:
        return _err("setting track volume", e)


@mcp.tool()
def set_track_pan(ctx: Context, track_index: int, value: float,
                  track: Union[int, str, None] = None) -> str:
    """Set a track's pan, -1.0 (hard left) .. 0.0 (center) .. +1.0 (hard right).
    track='master'/'return:N' targets those tracks."""
    try:
        params: Dict[str, Any] = {"value": value}
        params.update(_track_key(track_index, track))
        r = get_ableton_connection().send_command("set_track_pan", params)
        where = track if track is not None else track_index
        return f"Track {where} pan = {r.get('value'):.3f} ({r.get('display_value')})"
    except Exception as e:
        return _err("setting track pan", e)


@mcp.tool()
def set_track_send(
    ctx: Context,
    track_index: int,
    send_index: int,
    value: Optional[float] = None,
    value_normalized: Optional[float] = None,
) -> str:
    """Set a track's send level (0.0-1.0) to return track send_index (0-based)."""
    try:
        params: Dict[str, Any] = {"track_index": track_index, "send_index": send_index}
        if value_normalized is not None:
            params["value_normalized"] = value_normalized
        elif value is not None:
            params["value"] = value
        else:
            return "Error: provide either 'value' or 'value_normalized'"
        r = get_ableton_connection().send_command("set_track_send", params)
        return f"Track {track_index} send {send_index} = {r.get('value'):.3f} ({r.get('display_value')})"
    except Exception as e:
        return _err("setting track send", e)


@mcp.tool()
def set_track_mute(ctx: Context, track_index: int, on: bool) -> str:
    """Mute (on=True) or unmute (on=False) a track."""
    try:
        get_ableton_connection().send_command("set_track_mute", {"track_index": track_index, "on": on})
        return f"Track {track_index} mute = {on}"
    except Exception as e:
        return _err("setting track mute", e)


@mcp.tool()
def set_track_solo(ctx: Context, track_index: int, on: bool) -> str:
    """Solo (on=True) or unsolo (on=False) a track."""
    try:
        get_ableton_connection().send_command("set_track_solo", {"track_index": track_index, "on": on})
        return f"Track {track_index} solo = {on}"
    except Exception as e:
        return _err("setting track solo", e)


@mcp.tool()
def set_track_arm(ctx: Context, track_index: int, on: bool) -> str:
    """Arm (on=True) or disarm (on=False) a track for recording."""
    try:
        get_ableton_connection().send_command("set_track_arm", {"track_index": track_index, "on": on})
        return f"Track {track_index} arm = {on}"
    except Exception as e:
        return _err("setting track arm", e)


@mcp.tool()
def set_track_color(ctx: Context, track_index: int, color: int) -> str:
    """Set a track's color as a packed RGB integer (0xRRGGBB, e.g. 16711680 = red)."""
    try:
        get_ableton_connection().send_command("set_track_color", {"track_index": track_index, "color": color})
        return f"Set track {track_index} color to #{color:06X}"
    except Exception as e:
        return _err("setting track color", e)


# --------------------------------------------------------------------------- #
# Routing & sidechain
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_track_routing(ctx: Context, track_index: int = 0, track: Union[int, str, None] = None) -> str:
    """Read a track's audio/MIDI input and output routing plus its monitoring
    state ('in' / 'auto' / 'off'). Each side lists the current type/channel and
    every available option name, ready for set_track_routing.
    track='master'/'return:N' targets those tracks (they have no input)."""
    try:
        result = get_ableton_connection().send_command("get_track_routing", _track_key(track_index, track))
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting track routing", e)


@mcp.tool()
def set_track_routing(
    ctx: Context,
    track_index: int = 0,
    input_type: Optional[str] = None,
    input_channel: Optional[str] = None,
    output_type: Optional[str] = None,
    output_channel: Optional[str] = None,
    monitoring: Optional[str] = None,
    track: Union[int, str, None] = None,
) -> str:
    """Set a track's routing ("Audio From" / "Audio To" and their channel
    choosers) and monitoring. Names match get_track_routing's available lists
    (case-insensitive; a leading track number may be omitted, a unique
    substring also works). The type is applied before the channel.

    monitoring: 'in', 'auto' or 'off'. 'in' plays live input through the
    speakers and can cause feedback, so only set it when explicitly asked.
    Returns the resulting routing."""
    try:
        params: Dict[str, Any] = _track_key(track_index, track)
        for key, val in (("input_type", input_type), ("input_channel", input_channel),
                         ("output_type", output_type), ("output_channel", output_channel),
                         ("monitoring", monitoring)):
            if val is not None:
                params[key] = val
        result = get_ableton_connection().send_command("set_track_routing", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("setting track routing", e)


@mcp.tool()
def get_device_routing(ctx: Context, track_index: int, device_index: int,
                       track: Union[int, str, None] = None) -> str:
    """Read a device's sidechain input routing (e.g. Compressor's sidechain
    "Audio From" source and tap point) and whether its sidechain is on.
    'inputs' is empty when Live's API exposes no sidechain routing for the device."""
    try:
        params = {"device_index": device_index}
        params.update(_track_key(track_index, track))
        result = get_ableton_connection().send_command("get_device_routing", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting device routing", e)


@mcp.tool()
def set_device_sidechain(
    ctx: Context,
    track_index: int,
    device_index: int,
    source: Optional[str] = None,
    tap: Optional[str] = None,
    enabled: Optional[bool] = None,
    input: Optional[str] = None,
    track: Union[int, str, None] = None,
) -> str:
    """Route a device's sidechain, e.g. duck a bass Compressor from the kick.

    - source: the sidechain "Audio From" type, usually a track name ("KICK").
    - tap: the channel/tap point, e.g. "Post FX", "Pre FX", "Post Mixer", or
      a Drum Rack chain. Read get_device_routing after choosing the source:
      the channel list depends on it.
    - enabled: switch the sidechain on/off (Compressor's 'S/C On').
    - input: which device input to route ('sidechain' or 'audio_input:N');
      defaults to the first one.
    Returns the resulting device routing."""
    try:
        params: Dict[str, Any] = {"device_index": device_index}
        params.update(_track_key(track_index, track))
        for key, val in (("source", source), ("tap", tap), ("enabled", enabled), ("input", input)):
            if val is not None:
                params[key] = val
        result = get_ableton_connection().send_command("set_device_sidechain", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("setting device sidechain", e)


# --------------------------------------------------------------------------- #
# Audio clips, clip copy, scenes, scale, script reload
# --------------------------------------------------------------------------- #

def _send_json(command: str, params: Dict[str, Any], action: str) -> str:
    try:
        return json.dumps(get_ableton_connection().send_command(command, params), indent=2)
    except Exception as e:
        return _err(action, e)


@mcp.tool()
def get_clip_info(ctx: Context, track_index: int, clip_index: int) -> str:
    """Read a Session clip's loop/markers and, for audio clips, its file path,
    warping, warp mode, transpose (pitch_coarse semitones, pitch_fine cents)
    and clip gain."""
    return _send_json("get_clip_info", {"track_index": track_index, "clip_index": clip_index},
                      "getting clip info")


@mcp.tool()
def set_audio_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    warping: Optional[bool] = None,
    warp_mode: Union[str, int, None] = None,
    pitch_coarse: Optional[int] = None,
    pitch_fine: Optional[float] = None,
    gain: Optional[float] = None,
) -> str:
    """Change an audio clip's Warp settings, transpose and gain.
    warp_mode: 'Beats', 'Tones', 'Texture', 'Re-Pitch', 'Complex', 'REX' or
    'Complex Pro' (best for vocals). pitch_coarse: -48..48 semitones;
    pitch_fine: -50..50 cents. gain: 0..1 (read gain_display for dB)."""
    params: Dict[str, Any] = {"track_index": track_index, "clip_index": clip_index}
    for key, val in (("warping", warping), ("warp_mode", warp_mode), ("pitch_coarse", pitch_coarse),
                     ("pitch_fine", pitch_fine), ("gain", gain)):
        if val is not None:
            params[key] = val
    return _send_json("set_audio_clip", params, "setting audio clip")


@mcp.tool()
def copy_clip(ctx: Context, track_index: int, clip_index: int, to_clip_index: int,
              to_track_index: Optional[int] = None, move: bool = False) -> str:
    """Copy a Session clip into an empty slot (another scene row and/or another
    track of the same kind). move=True removes the original. The target slot
    must be empty."""
    params: Dict[str, Any] = {"track_index": track_index, "clip_index": clip_index,
                              "to_clip_index": to_clip_index, "move": move}
    if to_track_index is not None:
        params["to_track_index"] = to_track_index
    return _send_json("copy_clip", params, "copying clip")


@mcp.tool()
def get_scenes(ctx: Context) -> str:
    """List Session scenes with index, name, color and whether they are empty."""
    return _send_json("get_scenes", {}, "getting scenes")


@mcp.tool()
def set_scene_color(ctx: Context, scene_index: int, color: int) -> str:
    """Set a scene's color as a packed RGB integer (0xRRGGBB)."""
    return _send_json("set_scene_color", {"scene_index": scene_index, "color": color},
                      "setting scene color")


@mcp.tool()
def get_song_scale(ctx: Context) -> str:
    """Read the Set's Scale (root note, scale name, Scale Mode on/off)."""
    return _send_json("get_song_scale", {}, "getting song scale")


@mcp.tool()
def set_song_scale(ctx: Context, root: Union[str, int, None] = None,
                   scale_name: Optional[str] = None, scale_mode: Optional[bool] = None) -> str:
    """Set the Set's Scale: root as a note name ('A') or 0-11, scale_name as
    shown in Live (e.g. 'Minor'), scale_mode to toggle Scale Mode."""
    params: Dict[str, Any] = {}
    for key, val in (("root", root), ("scale_name", scale_name), ("scale_mode", scale_mode)):
        if val is not None:
            params[key] = val
    return _send_json("set_song_scale", params, "setting song scale")


@mcp.tool()
def reload_script(ctx: Context) -> str:
    """Reload the Remote Script's handlers inside Live after running install.sh,
    without restarting Live. Restart Live if the socket/server code changed."""
    return _send_json("reload_script", {}, "reloading script")


# --------------------------------------------------------------------------- #
# Tracks (structure)
# --------------------------------------------------------------------------- #

@mcp.tool()
def create_audio_track(ctx: Context, index: int = -1) -> str:
    """Create a new audio track. index=-1 appends at the end."""
    try:
        r = get_ableton_connection().send_command("create_audio_track", {"index": index})
        return f"Created audio track: {r.get('name')} (index {r.get('index')})"
    except Exception as e:
        return _err("creating audio track", e)


@mcp.tool()
def create_return_track(ctx: Context) -> str:
    """Create a new return track (appended after existing returns)."""
    try:
        r = get_ableton_connection().send_command("create_return_track")
        return f"Created return track: {r.get('name')} (return index {r.get('index')})"
    except Exception as e:
        return _err("creating return track", e)


@mcp.tool()
def delete_track(ctx: Context, track_index: int) -> str:
    """Delete a track. All higher track indices shift down — re-read
    get_session_info afterwards."""
    try:
        get_ableton_connection().send_command("delete_track", {"track_index": track_index})
        return f"Deleted track {track_index}"
    except Exception as e:
        return _err("deleting track", e)


@mcp.tool()
def duplicate_track(ctx: Context, track_index: int) -> str:
    """Duplicate a track (with its devices and clips). Re-read get_session_info
    afterwards to learn indices."""
    try:
        r = get_ableton_connection().send_command("duplicate_track", {"track_index": track_index})
        return f"Duplicated track {track_index} -> new index {r.get('index')}"
    except Exception as e:
        return _err("duplicating track", e)


# --------------------------------------------------------------------------- #
# Clips
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_clip_notes(ctx: Context, track_index: int, clip_index: int) -> str:
    """Read all MIDI notes from a clip. Returns JSON with a list of notes
    (pitch, start_time, duration, velocity, mute, and note_id when available)."""
    try:
        result = get_ableton_connection().send_command(
            "get_clip_notes", {"track_index": track_index, "clip_index": clip_index}
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting clip notes", e)


@mcp.tool()
def remove_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    from_time: float = 0.0,
    time_span: Optional[float] = None,
    from_pitch: int = 0,
    pitch_span: int = 128,
) -> str:
    """Remove MIDI notes from a clip within a time/pitch region. Defaults clear
    the whole clip (from_time=0, full length, all pitches). time_span=None means
    'to the end of the clip'."""
    try:
        params: Dict[str, Any] = {
            "track_index": track_index,
            "clip_index": clip_index,
            "from_time": from_time,
            "from_pitch": from_pitch,
            "pitch_span": pitch_span,
        }
        if time_span is not None:
            params["time_span"] = time_span
        get_ableton_connection().send_command("remove_notes_from_clip", params)
        return f"Removed notes from clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("removing notes from clip", e)


@mcp.tool()
def set_clip_loop(
    ctx: Context,
    track_index: int,
    clip_index: int,
    looping: Optional[bool] = None,
    loop_start: Optional[float] = None,
    loop_end: Optional[float] = None,
    start_marker: Optional[float] = None,
    end_marker: Optional[float] = None,
) -> str:
    """Set a clip's loop attributes (only the ones you pass are changed):
    looping on/off, loop_start/loop_end (beats), start_marker/end_marker."""
    try:
        params: Dict[str, Any] = {"track_index": track_index, "clip_index": clip_index}
        for k, v in (
            ("looping", looping), ("loop_start", loop_start), ("loop_end", loop_end),
            ("start_marker", start_marker), ("end_marker", end_marker),
        ):
            if v is not None:
                params[k] = v
        get_ableton_connection().send_command("set_clip_loop", params)
        return f"Updated loop on clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("setting clip loop", e)


@mcp.tool()
def duplicate_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """Duplicate a clip into the next clip slot on the same track."""
    try:
        r = get_ableton_connection().send_command(
            "duplicate_clip", {"track_index": track_index, "clip_index": clip_index}
        )
        return f"Duplicated clip to slot {r.get('target_index')} on track {track_index}"
    except Exception as e:
        return _err("duplicating clip", e)


@mcp.tool()
def delete_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """Delete the clip in a clip slot."""
    try:
        get_ableton_connection().send_command("delete_clip", {"track_index": track_index, "clip_index": clip_index})
        return f"Deleted clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        return _err("deleting clip", e)


@mcp.tool()
def set_clip_color(ctx: Context, track_index: int, clip_index: int, color: int) -> str:
    """Set a clip's color as a packed RGB integer (0xRRGGBB)."""
    try:
        get_ableton_connection().send_command(
            "set_clip_color", {"track_index": track_index, "clip_index": clip_index, "color": color}
        )
        return f"Set clip color at track {track_index}, slot {clip_index} to #{color:06X}"
    except Exception as e:
        return _err("setting clip color", e)


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #

@mcp.tool()
def create_scene(ctx: Context, index: int = -1) -> str:
    """Create a new scene. index=-1 appends at the end."""
    try:
        r = get_ableton_connection().send_command("create_scene", {"index": index})
        return f"Created scene at index {r.get('index')}"
    except Exception as e:
        return _err("creating scene", e)


@mcp.tool()
def fire_scene(ctx: Context, scene_index: int) -> str:
    """Launch a scene (fires every track's clip in that scene row)."""
    try:
        get_ableton_connection().send_command("fire_scene", {"scene_index": scene_index})
        return f"Fired scene {scene_index}"
    except Exception as e:
        return _err("firing scene", e)


@mcp.tool()
def set_scene_name(ctx: Context, scene_index: int, name: str) -> str:
    """Set a scene's name."""
    try:
        get_ableton_connection().send_command("set_scene_name", {"scene_index": scene_index, "name": name})
        return f"Renamed scene {scene_index} to '{name}'"
    except Exception as e:
        return _err("setting scene name", e)


@mcp.tool()
def delete_scene(ctx: Context, scene_index: int) -> str:
    """Delete a scene. Higher scene indices shift down."""
    try:
        get_ableton_connection().send_command("delete_scene", {"scene_index": scene_index})
        return f"Deleted scene {scene_index}"
    except Exception as e:
        return _err("deleting scene", e)


# --------------------------------------------------------------------------- #
# Transport / song
# --------------------------------------------------------------------------- #

@mcp.tool()
def set_time_signature(ctx: Context, numerator: int, denominator: int) -> str:
    """Set the song time signature, e.g. numerator=3, denominator=4 for 3/4."""
    try:
        get_ableton_connection().send_command(
            "set_time_signature", {"numerator": numerator, "denominator": denominator}
        )
        return f"Set time signature to {numerator}/{denominator}"
    except Exception as e:
        return _err("setting time signature", e)


@mcp.tool()
def set_metronome(ctx: Context, on: bool) -> str:
    """Turn the metronome on or off."""
    try:
        get_ableton_connection().send_command("set_metronome", {"on": on})
        return f"Metronome {'on' if on else 'off'}"
    except Exception as e:
        return _err("setting metronome", e)


@mcp.tool()
def set_song_position(ctx: Context, beats: float) -> str:
    """Move the transport playhead to a position in beats from song start."""
    try:
        get_ableton_connection().send_command("set_song_position", {"beats": beats})
        return f"Set song position to beat {beats}"
    except Exception as e:
        return _err("setting song position", e)


# --------------------------------------------------------------------------- #
# Arrangement recording — capture a Session loop so it can be exported
# Live's Export Audio/Video only renders the Arrangement timeline, so a song
# built in Session view must first be recorded into the Arrangement. These tools
# expose the transport's record state and a one-call capture that does it all.
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_transport(ctx: Context) -> str:
    """Report transport/record state: is_playing, record_mode (Arrangement Record
    ● armed?), session_record, current_song_time (beats), back_to_arranger,
    clip_trigger_quantization, tempo and time signature. Use it to verify a
    capture is running and to watch the playhead advance."""
    try:
        return json.dumps(get_ableton_connection().send_command("get_transport"), indent=2)
    except Exception as e:
        return _err("getting transport state", e)


@mcp.tool()
def set_arrangement_record(ctx: Context, on: bool = True) -> str:
    """Arm/disarm Arrangement (global) Record — the transport's ● button. With it
    ON, launching Session clips/scenes records them into the Arrangement
    timeline. This is the capability that lets a Session-view loop be captured
    for export (Live's exporter only renders the Arrangement). For the full
    automated capture use capture_session_to_arrangement instead."""
    try:
        r = get_ableton_connection().send_command("set_record_mode", {"on": on})
        return f"Arrangement Record {'ARMED' if r.get('record_mode') else 'disarmed'}"
    except Exception as e:
        return _err("setting arrangement record", e)


@mcp.tool()
def set_clip_trigger_quantization(ctx: Context, value: int = 0) -> str:
    """Set the global clip-launch quantization by index (0 = None, which launches
    clips immediately). capture_session_to_arrangement sets this to None for a
    tight start and restores it afterwards; exposed here for manual control."""
    try:
        r = get_ableton_connection().send_command("set_clip_trigger_quantization", {"value": value})
        return f"Clip trigger quantization index = {r.get('clip_trigger_quantization')}"
    except Exception as e:
        return _err("setting clip trigger quantization", e)


@mcp.tool()
def capture_session_to_arrangement(
    ctx: Context,
    scene_index: int,
    length_beats: float,
    tail_beats: float = 1.0,
    tighten_launch: bool = True,
    wait: bool = True,
    max_wait_seconds: float = 0.0,
) -> str:
    """Record a Session scene into the Arrangement so the song can be exported.

    Live's Export Audio/Video only renders the Arrangement, so a Session-view
    loop must first be printed there. This automates the whole capture — no
    manual Arrangement-record arming:

      1. stop, rewind to 1.1.1, force Session Record off, and (if tighten_launch)
         set clip-launch quantization to None for a tight start at bar 1,
      2. arm Arrangement Record (the ● button),
      3. fire scene `scene_index` — its clips begin printing into the Arrangement,
      4. wait one full pass (`length_beats` + `tail_beats`), polling the playhead,
      5. stop, disarm record, restore quantization, rewind, return to Arrangement.

    length_beats = bars * time_signature_numerator (e.g. 128 bars of 4/4 = 512).
    `tail_beats` lets the final notes ring before stopping. Re-recording the same
    span overwrites the previously captured Arrangement region for the scene's
    tracks, so re-exports stay clean.

    wait=True (default) blocks until the pass finishes and returns the result.
    wait=False arms + fires and returns immediately (then call stop_playback and
    set_arrangement_record(False) yourself once the playhead reaches length_beats).
    max_wait_seconds caps the blocking wait (0 = auto: one pass + 15 s headroom)."""
    try:
        conn = get_ableton_connection()

        info = conn.send_command("get_session_info")
        tempo = float(info.get("tempo", 120.0)) or 120.0
        seconds_per_beat = 60.0 / tempo
        expected_seconds = (length_beats + max(0.0, tail_beats)) * seconds_per_beat

        # Save clip-launch quantization so we can restore it after the capture.
        old_q = int(conn.send_command("get_transport").get("clip_trigger_quantization", 0))

        # Arm: clean transport, rewind, target the Arrangement (not Session slots).
        conn.send_command("stop_playback")
        conn.send_command("set_record_mode", {"on": False})
        conn.send_command("set_session_record", {"on": False})
        conn.send_command("set_song_position", {"beats": 0.0})
        if tighten_launch:
            conn.send_command("set_clip_trigger_quantization", {"value": 0})
        conn.send_command("set_record_mode", {"on": True})
        conn.send_command("fire_scene", {"scene_index": scene_index})

        if not wait:
            return json.dumps({
                "armed": True,
                "scene_index": scene_index,
                "expected_end_beat": length_beats,
                "est_seconds": round(expected_seconds, 1),
                "tempo": tempo,
                "note": ("Recording into the Arrangement now. When current_song_time "
                         ">= %g (see get_transport), call stop_playback then "
                         "set_arrangement_record(False)%s." % (
                             length_beats,
                             " and set_clip_trigger_quantization(%d)" % old_q
                             if tighten_launch else "")),
            }, indent=2)

        # Wait one full pass: coarse sleep to just before the target, then poll
        # the real playhead so we stop precisely when the song has recorded.
        cap = max_wait_seconds if max_wait_seconds and max_wait_seconds > 0 else expected_seconds + 15.0
        started = time.time()
        time.sleep(max(0.0, expected_seconds - 1.0))
        end_song_time = None
        while time.time() - started < cap:
            cur = float(conn.send_command("get_transport").get("current_song_time", 0.0))
            if cur >= length_beats:
                end_song_time = cur
                break
            time.sleep(0.25)

        # Disarm and restore.
        conn.send_command("stop_playback")
        conn.send_command("set_record_mode", {"on": False})
        if tighten_launch:
            conn.send_command("set_clip_trigger_quantization", {"value": old_q})
        conn.send_command("set_song_position", {"beats": 0.0})
        conn.send_command("set_back_to_arranger", {"on": False})

        reached = end_song_time is not None
        return json.dumps({
            "ok": reached,
            "scene_index": scene_index,
            "length_beats": length_beats,
            "end_song_time": end_song_time,
            "seconds": round(time.time() - started, 1),
            "tempo": tempo,
            "note": ("Captured one pass into the Arrangement. Re-recording the same "
                     "span overwrites it. Now export with Master selected and length "
                     "= %g beats." % length_beats) if reached else
                    ("Stopped after the wait cap (%.0fs) before the playhead reached "
                     "%g beats — check tempo/length and retry, or capture with "
                     "wait=False and stop manually." % (cap, length_beats)),
        }, indent=2)
    except Exception as e:
        return _err("capturing session to arrangement", e)


@mcp.tool()
def health(ctx: Context) -> str:
    """Diagnostic: confirm the connection and report the Remote Script version
    that is actually loaded in Ableton (useful after reinstalling the script)."""
    try:
        return json.dumps(get_ableton_connection().send_command("health"), indent=2)
    except Exception as e:
        return _err("checking health", e)


# --------------------------------------------------------------------------- #
# Metering — read output levels so mixing/mastering is data-driven
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_track_meter(ctx: Context, track: Union[int, str] = 0) -> str:
    """Read a track's output meter (0..1) for left/right and overall level.
    track can be an int index, 'master', or 'return:N'. Meters only move while
    audio plays — fire a clip/scene first, then sample (call a few times)."""
    try:
        result = get_ableton_connection().send_command("get_track_meter", {"track": track})
        return json.dumps(result, indent=2)
    except Exception as e:
        return _err("getting track meter", e)


@mcp.tool()
def get_master_meter(ctx: Context) -> str:
    """Read the master bus output meter (0..1). Sample while a scene is playing
    to gauge overall level / spot clipping (level near 1.0 = too hot)."""
    try:
        return json.dumps(get_ableton_connection().send_command("get_master_meter"), indent=2)
    except Exception as e:
        return _err("getting master meter", e)


# --------------------------------------------------------------------------- #
# Host-OS automation — launch, save, export (NOT via the Live socket)
# These drive Ableton at the macOS level (open / AppleScript) because the Live
# Object Model has no API for app launch, file save, or audio export.
# --------------------------------------------------------------------------- #

@mcp.tool()
def launch_ableton(ctx: Context, als_path: Optional[str] = None) -> str:
    """Launch (or focus) Ableton Live 12, optionally opening a .als, then wait
    for the AbletonMCP control-surface bridge to come up on :9877. Use this to
    start Ableton before composing, or to reopen a saved project."""
    try:
        return json.dumps(_host.launch_ableton(als_path), indent=2)
    except Exception as e:
        return _err("launching Ableton", e)


@mcp.tool()
def new_live_set(ctx: Context) -> str:
    """File ▸ New Live Set (Cmd-N) for a clean slate. The control surface is a
    global preference, so the :9877 bridge survives. Requires Accessibility."""
    try:
        return json.dumps(_host.new_live_set(), indent=2)
    except Exception as e:
        return _err("creating new Live Set", e)


@mcp.tool()
def save_set(ctx: Context) -> str:
    """File ▸ Save (Cmd-S) — for a Set already saved to disk. Requires Accessibility."""
    try:
        return json.dumps(_host.save_set(), indent=2)
    except Exception as e:
        return _err("saving set", e)


@mcp.tool()
def save_set_as(ctx: Context, als_path: str) -> str:
    """File ▸ Save As to a specific path, e.g.
    '.../projects/<slug>/<Name> Project/<Name>.als'. Drives the macOS save sheet
    via AppleScript (best-effort) and returns expected_path — verify the file
    exists afterwards. Requires Accessibility."""
    try:
        return json.dumps(_host.save_set_as(als_path), indent=2)
    except Exception as e:
        return _err("saving set as", e)


@mcp.tool()
def open_export_dialog(ctx: Context, target_path: Optional[str] = None,
                       format_hint: str = "") -> str:
    """Open Export Audio/Video (Cmd-Shift-R) and copy target_path to the
    clipboard for pasting into the save sheet. Returns the remaining steps; does
    NOT press Render (you confirm the export). Requires Accessibility."""
    try:
        return json.dumps(_host.open_export_dialog(target_path, format_hint), indent=2)
    except Exception as e:
        return _err("opening export dialog", e)


@mcp.tool()
def analyze_audio(ctx: Context, wav_path: str) -> str:
    """Analyze a rendered/captured audio file — the objective 'listening' step.

    Returns peak/RMS dBFS, crest factor, clipping; LUFS (integrated/short-term/
    LRA) and true peak (dBTP); a 10-band spectrum (+ legacy 4-band) with centroid,
    rolloff and flatness; transient/punch (onset strength/rate, per-band crest);
    stereo correlation + overall/per-band width; DC offset, noise floor and
    leading/trailing silence. `loudness_method` says whether LUFS came from
    pyloudnorm or the numpy fallback. Capture a track/section first (record_audio)
    or render via the export path, then analyze. Needs numpy+soundfile."""
    try:
        from . import _analysis
        return json.dumps(_analysis.analyze_audio(wav_path), indent=2)
    except Exception as e:
        return _err("analyzing audio", e)


@mcp.tool()
def compare_audio(ctx: Context, path_a: str, path_b: str) -> str:
    """Diff two audio files (b relative to a) into human-readable deltas —
    loudness, per-band level, brightness (centroid), punch (low-band crest),
    width and dynamics, plus a one-line headline. Sub-perceptual changes are
    suppressed. This is the closed loop's progress meter: capture before/after a
    parameter move and compare to confirm the change helped."""
    try:
        from . import _analysis
        return json.dumps(_analysis.compare_audio(path_a, path_b), indent=2)
    except Exception as e:
        return _err("comparing audio", e)


# --------------------------------------------------------------------------- #
# Loopback capture — give Claude per-track "ears" (realtime, unattended)
# Captures Live's output from a loopback device (BlackHole) while a soloed track
# (or the master) plays, so any single part can be measured with analyze_audio.
# Orchestration (solo/transport) lives here; the ffmpeg capture is in _record.
# --------------------------------------------------------------------------- #

def _source_is_track(source: Union[int, str]) -> tuple[bool, Optional[int]]:
    """(is_regular_track, index). 'master'/'return:N' → (False, None)."""
    if isinstance(source, bool):
        return False, None
    if isinstance(source, int):
        return True, source
    s = str(source).strip()
    if s.lstrip("-").isdigit():
        return True, int(s)
    return False, None


def _record_audio_impl(seconds, bars, source, position_beats, solo, scene,
                       preroll_seconds, tail_seconds, tighten_launch, device, out_path) -> Dict[str, Any]:
    """Capture `source` to a WAV. Solos a regular track (then restores), launches
    a Session scene (or Arrangement playback), records via the loopback device,
    and trims the window. Returns capture metadata. Raises on failure."""
    from . import _record

    conn = get_ableton_connection()
    info = conn.send_command("get_session_info")
    tempo = float(info.get("tempo", 120.0)) or 120.0
    num = int(info.get("signature_numerator", 4)) or 4
    track_count = int(info.get("track_count", 0))

    if bars is not None and seconds is None:
        seconds = float(bars) * num * (60.0 / tempo)
    if seconds is None:
        raise ValueError("provide either 'seconds' or 'bars'")
    seconds = float(seconds)
    if seconds <= 0:
        raise ValueError("duration must be > 0")
    capped_note = "long window (%.1fs) — realtime capture cost; prefer 2–4 bars" % seconds if seconds > 60 else None

    is_track, tval = _source_is_track(source)
    do_solo = bool(solo and is_track)

    # Resolve the loopback device BEFORE touching transport (fail clean).
    dev_idx, dev_name = _record.find_loopback_device(device)
    if not out_path:
        out_path = _record.default_capture_path("track%d" % tval if is_track else str(source))

    prior_solo: Dict[int, bool] = {}
    old_q = None
    try:
        if do_solo:
            for i in range(track_count):
                ti = conn.send_command("get_track_info", {"track_index": i})
                prior_solo[i] = bool(ti.get("solo"))
            for i in range(track_count):
                want = (i == tval)
                if prior_solo.get(i) != want:
                    conn.send_command("set_track_solo", {"track_index": i, "on": want})

        conn.send_command("stop_playback")
        conn.send_command("set_song_position", {"beats": position_beats})
        if scene is not None:
            if tighten_launch:
                old_q = int(conn.send_command("get_transport").get("clip_trigger_quantization", 0))
                conn.send_command("set_clip_trigger_quantization", {"value": 0})
            conn.send_command("fire_scene", {"scene_index": scene})
        else:
            conn.send_command("start_playback")

        _record.ffmpeg_record(dev_idx, preroll_seconds + seconds + tail_seconds, out_path)
    finally:
        try:
            conn.send_command("stop_playback")
        except Exception:
            pass
        if old_q is not None:
            try:
                conn.send_command("set_clip_trigger_quantization", {"value": old_q})
            except Exception:
                pass
        for i, was in prior_solo.items():
            if was != (i == tval):
                try:
                    conn.send_command("set_track_solo", {"track_index": i, "on": was})
                except Exception:
                    pass

    trim = _record.trim_to_window(out_path, seconds, max_lead_trim_seconds=preroll_seconds + 0.5)
    return {
        "path": out_path, "source": source, "seconds": round(seconds, 3), "bars": bars,
        "tempo": tempo, "device": dev_name, "soloed_track": tval if do_solo else None,
        "scene": scene, "trim": trim, "note": capped_note,
    }


@mcp.tool()
def record_audio(
    ctx: Context,
    seconds: Optional[float] = None,
    bars: Optional[float] = None,
    source: Union[int, str] = "master",
    scene: Optional[int] = None,
    position_beats: float = 0.0,
    solo: bool = True,
    preroll_seconds: float = 0.6,
    tail_seconds: float = 0.3,
    tighten_launch: bool = True,
    device: Optional[str] = None,
    out_path: Optional[str] = None,
) -> str:
    """Capture Live's audio output to a WAV via a loopback device (BlackHole) —
    the per-track 'ears'. Give a duration as `bars` (resolved from the session
    tempo/meter) or `seconds`; capture is REALTIME so prefer 2–4 bars.

    source: a track index (int) to capture ONE track — it is soloed for the
    capture and the prior solo state is restored — or 'master' / 'return:N'
    (no solo). scene: the Session scene to fire so clips actually play (this
    project composes in Session view — pass the arrangement scene index);
    omit scene to play the Arrangement from position_beats instead.

    Needs BlackHole + a Multi-Output Device set as Live's output, ffmpeg, and a
    one-time Microphone grant. If no loopback device is found the reply explains
    how to install it and names the manual-export fallback. Returns the WAV path
    (in the temp dir) — feed it to analyze_audio / compare_audio."""
    try:
        result = _record_audio_impl(seconds, bars, source, position_beats, solo, scene,
                                    preroll_seconds, tail_seconds, tighten_launch, device, out_path)
        return json.dumps(result, indent=2)
    except Exception as e:
        from . import _record
        if isinstance(e, _record.LoopbackNotFound):
            return "Error capturing audio: %s" % e
        return _err("capturing audio", e)


@mcp.tool()
def record_and_analyze(
    ctx: Context,
    seconds: Optional[float] = None,
    bars: Optional[float] = None,
    source: Union[int, str] = "master",
    scene: Optional[int] = None,
    position_beats: float = 0.0,
    solo: bool = True,
    device: Optional[str] = None,
) -> str:
    """Capture `source` (see record_audio) then analyze it in one call — the
    single primitive the tuning loop uses: change a parameter, then
    record_and_analyze(source=track, bars=2, scene=N) to read the result.
    Returns {capture, analysis}."""
    try:
        from . import _analysis
        cap = _record_audio_impl(seconds, bars, source, position_beats, solo, scene,
                                 0.6, 0.3, True, device, None)
        return json.dumps({"capture": cap, "analysis": _analysis.analyze_audio(cap["path"])}, indent=2)
    except Exception as e:
        from . import _record
        if isinstance(e, _record.LoopbackNotFound):
            return "Error capturing audio: %s" % e
        return _err("recording and analyzing", e)


def main() -> None:
    """Run the MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
