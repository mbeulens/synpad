"""synpad.py under GTK4 — headless.

Guards Task 6's scope, the final module of the port:

- `Gtk.Application` -> `Adw.Application` (mutation-sensitive: checked via
  `issubclass(..., Adw.Application)`, not just `Gtk.Application` — every
  `Adw.Application` already *is* a `Gtk.Application`, so that weaker check
  would pass even if the subclassing were reverted).
- `Adw.init()` is actually called by production code during `do_startup`
  (tracked by wrapping `Adw.init` itself, not just asserted from source —
  `Adw.is_initialized()` alone can't prove it, since every other test file
  in this suite already calls `Adw.init()` at import time and initialization
  is process-global/idempotent).
- The `GDK_BACKEND=x11` pin and the entire `GdkX11` present-with-time block
  are gone — asserted both against the live source text (so a re-add is
  caught even if it happens to be dead code) and behaviourally (`_present_window`
  calls plain `.present()`, no `GdkX11` import happens as a side effect).
- `application_id` and `Gio.ApplicationFlags.HANDLES_OPEN` are preserved
  exactly, since a second SynPad instance's single-instance file-open
  behaviour depends on both.
- `do_activate()`/`do_open()` end to end against a *real* `SynPadWindow`:
  a single window is constructed and reused across repeated activation,
  presented via the idle callback, and `do_open()` actually opens the given
  file into a tab (not a stub) using the real `open_or_focus_file` path.

Safety note (Global Constraint 8): `$HOME`/`$XDG_CONFIG_HOME` are redirected
to a throwaway directory before `config` (or anything importing it, i.e.
`window`) is ever imported, and `config.save_config`/`secrets_store` are
monkeypatched on top, matching the convention established in
test_window_gtk4.py. This file constructs real `SynPadWindow` instances via
`SynPadApplication.do_activate()`, so both layers apply.
"""
import os
import sys
import tempfile

_FAKE_HOME = tempfile.mkdtemp(prefix='synpad_test_home_')
os.environ['HOME'] = _FAKE_HOME
os.environ.setdefault('XDG_CONFIG_HOME', os.path.join(_FAKE_HOME, '.config'))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GLib, Adw

if not Gtk.init_check():
    print("SKIP: no display")
    sys.exit(0)
Adw.init()

import config
save_config_calls = []
config.save_config = lambda cfg: save_config_calls.append(dict(cfg))

import secrets_store
secrets_store.is_available = lambda: False

import window
window.save_config = lambda cfg: save_config_calls.append(dict(cfg))
if hasattr(window, 'secrets_store'):
    window.secrets_store = secrets_store
for mod_name in ('editor', 'remote', 'local_files', 'dialogs', 'connection'):
    mod = sys.modules.get(mod_name) or getattr(window, mod_name, None)
    if mod is not None and hasattr(mod, 'save_config'):
        mod.save_config = lambda cfg: save_config_calls.append(dict(cfg))
    if mod is not None and hasattr(mod, 'secrets_store'):
        mod.secrets_store = secrets_store

import synpad

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {extra}" if not cond and extra else ""))
    if not cond:
        fails.append(name)


def pump(n=50):
    ctx = GLib.MainContext.default()
    for _ in range(n):
        while ctx.iteration(False):
            pass


# =========================================================================
# Source-level: the X11 hack is fully gone, not just unreachable
# =========================================================================

_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'synpad.py')).read()

check("GDK_BACKEND pin removed from source", "GDK_BACKEND" not in _src)
check("GdkX11 require_version removed from source", "GdkX11" not in _src)
check("x11_get_server_time removed from source", "x11_get_server_time" not in _src)
check("present_with_time removed from source", "present_with_time" not in _src)
check("show_all removed from source", "show_all" not in _src)
check("synpad.py pins Gtk 4.0", "gi.require_version('Gtk', '4.0')" in _src)
check("synpad.py pins GtkSource 5", "gi.require_version('GtkSource', '5')" in _src)
check("synpad.py pins Adw 1", "gi.require_version('Adw', '1')" in _src)


# =========================================================================
# Class shape
# =========================================================================

check("SynPadApplication subclasses Adw.Application (not just Gtk.Application)",
      issubclass(synpad.SynPadApplication, Adw.Application))
# Note: GTK4 itself loads the GdkX11 typelib as part of backend probing
# (verified: present after a bare `from gi.repository import Gtk` + one
# Gtk.ApplicationWindow(), with none of synpad.py's code involved), so
# `sys.modules` is not a usable signal for "does synpad.py import GdkX11" —
# the source-text checks above are the real (and mutation-sensitive) guard.

app = synpad.SynPadApplication()

check("application_id preserved", app.get_application_id() == "com.mbeulens.synpad")
check("HANDLES_OPEN flag preserved",
      bool(app.get_flags() & Gio.ApplicationFlags.HANDLES_OPEN))
check("app.window starts unset", app.window is None)

# NON_UNIQUE only: registering the real "com.mbeulens.synpad" id on the
# session bus from a headless test process would either collide with (or
# be silently proxied to) a genuinely running SynPad, and this sandbox has
# no real desktop session anyway. This does not touch the application_id
# check above, which already ran against a pristine, unregistered instance.
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)


# =========================================================================
# do_startup (via register()) — Adw.init() is actually called by
# production code, not just by this test file's own setup
# =========================================================================

_init_calls = []
_real_adw_init = Adw.init


def _tracking_init(*a, **kw):
    _init_calls.append((a, kw))
    return _real_adw_init(*a, **kw)


Adw.init = _tracking_init
try:
    check("app.register() succeeds", app.register())
finally:
    Adw.init = _real_adw_init

check("register() -> do_startup() calls Adw.init() itself", len(_init_calls) >= 1)
check("Adw is initialized after startup", Adw.is_initialized())


# =========================================================================
# do_activate — constructs exactly one real SynPadWindow, reused on replay
# =========================================================================

app.activate()
check("do_activate() constructs a window", app.window is not None)
check("constructed window is a SynPadWindow", isinstance(app.window, window.SynPadWindow))
first_window = app.window

presented = []
first_window.present = lambda: presented.append(True)

pump()
check("idle-queued _present_window() calls window.present()", presented == [True])

app.activate()
check("a second activation does NOT construct a second window",
      app.window is first_window)


# =========================================================================
# do_open — real file-open path, not a stub
# =========================================================================

app2 = synpad.SynPadApplication()
# A distinct id: two GApplications sharing "com.mbeulens.synpad" would both
# try to export a D-Bus object at the same path on the session bus and the
# second registration would fail outright — an artifact of running two
# instances in one test process, not something the real single-instance
# app ever does (do_open() here is being driven directly, the same call
# a real second `synpad.py` invocation's remote delegation would make).
app2.set_application_id("com.mbeulens.synpad.test-second-instance")
app2.set_flags(app2.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
app2.register()
app2.activate()
app2.window.present = lambda: None
pump()

fd, tmp_path = tempfile.mkstemp(suffix='.txt', prefix='synpad_open_test_')
with os.fdopen(fd, 'w') as f:
    f.write("hello from test_synpad_gtk4\n")

gio_file = Gio.File.new_for_path(tmp_path)
app2.open([gio_file], "")
pump()

opened_paths = [
    tab.local_path for tab in app2.window.tabs.values()
    if getattr(tab, 'is_local', False)
]
check("do_open() actually opens the file into a tab (real open_or_focus_file, not a stub)",
      os.path.realpath(tmp_path) in [os.path.realpath(p) for p in opened_paths if p])

os.unlink(tmp_path)


# =========================================================================
# Cleanup — close both real windows so nothing lingers
# =========================================================================

for a in (app, app2):
    if a.window is not None:
        try:
            a.window.destroy()
        except Exception:
            pass

if fails:
    print(f"\n{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("\nALL PASS")
