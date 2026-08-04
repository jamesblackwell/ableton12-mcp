"""Host-side (macOS) automation — the things the Live Object Model can't do.

There is **no** Live API for launching the app, creating/saving a Set, or
exporting audio, so none of this goes through the :9877 socket. Instead we drive
the application at the OS level:

  * ``open -a`` to launch / focus Ableton (and optionally open a ``.als``),
  * AppleScript (``osascript`` → System Events) to send the menu keystrokes for
    New / Save / Save-As / Export.

All of it is **macOS-only** and the keystroke parts need a one-time
**Accessibility** grant for whatever process runs ``osascript`` (System Settings
→ Privacy & Security → Accessibility). The Save-As / Export dialog scripting is
inherently fragile (it depends on the dialog layout of the running Live version),
so each routine is best-effort and returns an ``expected_path`` / step list the
caller should verify on disk rather than trusting blindly.
"""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import time
from typing import Optional

PORT = 9877

ACCESSIBILITY_HINT = (
    "Grant Accessibility permission to the app running this MCP server "
    "(Terminal / iTerm / your editor) under System Settings → Privacy & "
    "Security → Accessibility, then retry."
)


# --------------------------------------------------------------------------- #
# Locating / launching Ableton
# --------------------------------------------------------------------------- #

def find_ableton_app() -> Optional[str]:
    """Return the best Ableton Live .app path, preferring a 12.x install."""
    candidates = sorted(glob.glob("/Applications/Ableton Live *.app"), reverse=True)
    for c in candidates:
        if "12" in os.path.basename(c):
            return c
    return candidates[0] if candidates else None


def wait_for_bridge(timeout: float = 90.0, port: int = PORT) -> bool:
    """Poll until the Remote Script's socket accepts a connection (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=2):
                return True
        except OSError:
            time.sleep(2.0)
    return False


def launch_ableton(als_path: Optional[str] = None, wait: bool = True,
                   timeout: float = 90.0) -> dict:
    """Launch (or focus) Ableton Live, optionally opening a .als, then wait for
    the control-surface bridge to come up on :9877."""
    app = find_ableton_app()
    cmd = ["open"]
    if app:
        cmd += ["-a", app]
    if als_path:
        cmd.append(os.path.expanduser(als_path))
    if not app and not als_path:
        # last resort: launch by bundle id
        cmd = ["open", "-b", "com.ableton.live"]
    subprocess.run(cmd, check=True)

    bridge_up = wait_for_bridge(timeout) if wait else None
    return {"app": app, "opened": als_path, "bridge_up": bridge_up,
            "note": ("bridge listening on :%d" % PORT) if bridge_up else
                    ("bridge not up within %.0fs — is AbletonMCP selected as a "
                     "Control Surface?" % timeout if wait else "did not wait")}


# --------------------------------------------------------------------------- #
# AppleScript helpers
# --------------------------------------------------------------------------- #

def _as_str(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if "-1719" in err or "1002" in err or "not allowed" in low or "assistive" in low:
            raise RuntimeError("macOS Accessibility permission missing. "
                               + ACCESSIBILITY_HINT + " (osascript: %s)" % err)
        raise RuntimeError("osascript failed: %s" % err)
    return (proc.stdout or "").strip()


def _activate() -> None:
    """Bring Ableton to the front so keystrokes / menu clicks land on it."""
    app = find_ableton_app()
    if app:
        subprocess.run(["open", "-a", app], check=False)
    else:
        subprocess.run(["open", "-b", "com.ableton.live"], check=False)
    time.sleep(1.0)


_PROC_CACHE: Optional[str] = None


def _live_process_name() -> str:
    """The name System Events uses for the running Ableton process.

    The macOS executable is ``Live`` (so System Events reports ``Live``), but we
    discover it at runtime to stay robust across Live editions, defaulting to
    ``Live``. Cached after the first successful lookup."""
    global _PROC_CACHE
    if _PROC_CACHE:
        return _PROC_CACHE
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of (first process '
             'whose name is "Live" or name contains "Ableton")'],
            capture_output=True, text=True, timeout=10,
        )
        name = (out.stdout or "").strip()
        if name:
            _PROC_CACHE = name
            return name
    except Exception:
        pass
    _PROC_CACHE = "Live"
    return "Live"


def _click_file_menu_item(item_label: str) -> None:
    """Click File ▸ <item_label> by its exact name.

    Clicking the menu item is far more reliable than a blind Command-key
    keystroke (which can be swallowed or land on the wrong element). Raises a
    clear error if the menu / item can't be reached."""
    proc = _live_process_name()
    _activate()
    script = "\n".join([
        'tell application "System Events" to tell process %s' % _as_str(proc),
        '  if not (exists menu bar item "File" of menu bar 1) then '
        'error "Ableton File menu not reachable (is Live running and frontmost?)"',
        '  click menu item %s of menu 1 of menu bar item "File" of menu bar 1'
        % _as_str(item_label),
        'end tell',
    ])
    _run_osascript(script)


# --------------------------------------------------------------------------- #
# Menu actions
# --------------------------------------------------------------------------- #

def new_live_set() -> dict:
    """File ▸ New Live Set. The AbletonMCP control surface is a global
    preference, so the :9877 bridge survives the new Set."""
    _click_file_menu_item("New Live Set")
    time.sleep(1.0)
    return {"ok": True, "action": "new_live_set"}


def save_set() -> dict:
    """File ▸ Save Live Set — for a Set that already has a path on disk."""
    _click_file_menu_item("Save Live Set")
    time.sleep(0.8)
    return {"ok": True, "action": "save"}


def save_set_as(als_path: str) -> dict:
    """Save the current Set to ``als_path``, driving Live's Save dialog fully
    automatically (no manual interaction required).

    Reality of Live 12's Save-As, established empirically (the old blind
    keystroke choreography failed for all three reasons):

      1. The dialog is a separate **window titled "Save"** (an NSSavePanel-style
         ``AXDialog``) — *not* a sheet of the main window.
      2. **Live auto-creates a ``<Name> Project`` folder** for a new Set, under
         whatever folder the dialog is pointing at. So for the conventional
         target ``.../<slug>/<Name> Project/<Name>.als`` we navigate to the
         *parent* (``.../<slug>``) and let Live build the ``<Name> Project``.
      3. The name field's accessibility value is **read-only** (``set value``
         throws) — it must be typed via ``keystroke`` into the focused field —
         while the Go-to-folder field **is** settable, and ``set value`` on it
         is reliable where synthetic paste/keystroke autocompletes/truncates.

    The result is verified on disk before returning. This function is strictly
    non-destructive: it never deletes or overwrites anything itself.
    """
    als_path = os.path.expanduser(als_path)
    name = os.path.splitext(os.path.basename(als_path))[0]
    desired_dir = os.path.dirname(als_path)

    # Predict where Live will actually write, given it adds "<name> Project".
    project_folder_name = "%s Project" % name
    if os.path.basename(desired_dir) == project_folder_name:
        nav_target = os.path.dirname(desired_dir)        # navigate to the slug dir
        expected_path = als_path
    else:
        nav_target = desired_dir
        expected_path = os.path.join(desired_dir, project_folder_name, "%s.als" % name)

    os.makedirs(nav_target, exist_ok=True)  # parent must exist to navigate into it

    proc = _live_process_name()
    script = "\n".join([
        'tell application "System Events" to tell process %s' % _as_str(proc),
        '  if not (exists menu bar item "File" of menu bar 1) then '
        'error "Ableton File menu not reachable (is Live running and frontmost?)"',
        '  click menu item "Save Live Set As..." of menu 1 of menu bar item "File" of menu bar 1',
        '  set t to 0',
        '  repeat until (exists window "Save") or t > 60',
        '    delay 0.2',
        '    set t to t + 1',
        '  end repeat',
        '  if not (exists window "Save") then error "Save dialog did not appear"',
        '  delay 0.9',
        '  -- Filename: the name field is focused & selected when the dialog opens.',
        '  keystroke "a" using {command down}',
        '  delay 0.3',
        '  keystroke %s' % _as_str(name),
        '  delay 0.4',
        '  -- Folder: open Go-to-folder and set its text field value directly.',
        '  keystroke "g" using {command down, shift down}',
        '  set gt to 0',
        '  repeat until (exists sheet 1 of window "Save") or gt > 30',
        '    delay 0.2',
        '    set gt to gt + 1',
        '  end repeat',
        '  if (exists sheet 1 of window "Save") then',
        '    set value of text field 1 of sheet 1 of window "Save" to %s' % _as_str(nav_target),
        '    delay 0.4',
        '    keystroke return',
        '    delay 1.2',
        '  end if',
        '  -- Save: the default (highlighted) button responds to Return.',
        '  keystroke return',
        '  delay 1.5',
        '  -- A "Replace existing file?" confirmation defaults to Replace.',
        '  if (exists window "Save") then',
        '    keystroke return',
        '    delay 1.0',
        '  end if',
        'end tell',
    ])

    _activate()
    _run_osascript(script)

    # Live writes asynchronously — verify on disk instead of trusting the UI.
    verified = False
    for _ in range(24):  # poll ~12s
        if os.path.exists(expected_path):
            verified = True
            break
        time.sleep(0.5)

    result = {
        "ok": verified,
        "action": "save_as",
        "expected_path": expected_path,
        "navigated_to": nav_target,
        "set_name": name,
        "verified": verified,
    }
    if verified:
        result["note"] = "Verified the .als on disk."
    else:
        # The name may not have taken (Live would make an "Untitled Project").
        # Report any sets found under the target — never delete them.
        found = []
        try:
            for entry in sorted(os.listdir(nav_target)):
                full = os.path.join(nav_target, entry)
                if entry.endswith(" Project") and os.path.isdir(full):
                    als = [f for f in os.listdir(full) if f.endswith(".als")]
                    if als:
                        found.append(os.path.join(full, als[0]))
        except OSError:
            pass
        result["found_under_parent"] = found
        result["note"] = (
            "Could not verify %s within 12s. If the Save dialog is still open, "
            "finish it manually. Sets currently under the target folder: %s"
            % (expected_path, found or "none")
        )
    return result


def open_export_dialog(target_path: Optional[str] = None, format_hint: str = "") -> dict:
    """Open File ▸ Export Audio/Video and copy ``target_path`` to the clipboard
    so it can be pasted into the export save sheet. Deliberately does **not**
    press Render — the user confirms the export (Hybrid automation)."""
    if target_path:
        target_path = os.path.expanduser(target_path)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        subprocess.run(["pbcopy"], input=target_path, text=True, check=False)
    _click_file_menu_item("Export Audio/Video...")
    steps = [
        "In the Export dialog: set Rendered Track = Master, the Length to the full song.",
        "Set Sample Rate 44100, Bit Depth 24, File Type WAV (PCM).",
        "Click Export; in the save sheet press Cmd-Shift-G and paste (Cmd-V) the "
        "path that is already on your clipboard, then Save.",
    ]
    if format_hint:
        steps.append("Requested format(s): %s" % format_hint)
    return {"ok": True, "action": "open_export_dialog",
            "clipboard": target_path, "steps": steps}
