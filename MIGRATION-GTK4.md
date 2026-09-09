# SynPad 2.0 — GTK4 + libadwaita migration

**Status: complete.** All six tasks (thirteen modules plus `synpad.py`) are
ported, reviewed and tested on branch `gtk4`. `synpad.py` was the last file
still on GTK3; with it ported, SynPad 2.0 launches — confirmed as a native
Wayland client (see "Launch verification" below), with the `GDK_BACKEND=x11`
pin and the `GdkX11` present-with-time hack both deleted outright.

Branch `gtk4`, versioned `2.0.8-dev` as of the last module port (Task 6's
commit bumps it further). `dev` stays GTK3 and shippable for 1.21.x patches;
merge `dev` -> `gtk4` regularly so this branch does not rot.

GTK3 and GTK4 cannot coexist in one process (`gi.require_version` is
process-global), so there is no half-ported shippable state. This is a
hard cutover on a branch, by design.

## Why

Two bugs are in tension on GTK3:

| setup | paste from Chrome | focus on file-open |
|---|---|---|
| GTK3 + XWayland (1.x today) | hangs, intermittently | works |
| GTK3 + native Wayland | works | breaks — why `synpad.py` pins X11 |
| GTK4 + native Wayland | works | works |

Ubuntu ships no `GdkWayland-3.0` typelib, so GTK3 Python cannot apply
xdg-activation tokens. GTK4 ships `GdkWayland-4.0` and handles activation
internally. Verified 2026-09-09: Chrome runs `--ozone-platform=wayland`
while SynPad runs XWayland, so the clipboard crosses Mutter's X11 bridge.
Not yet reproduced under instrumentation — running v2 as the daily editor
is the experiment.

## Order of work (riskiest first)

1. **`completion.py` vs GtkSourceView 5** — 452 lines, true rewrite:
   async `populate_async`/`populate_finish`, `CompletionCell` display
   model, `activate()` not `activate_proposal()`, no `CompletionItem`.
   Spike standalone; it is the one item that can blow up the estimate.
   Port the headless test suite alongside.
2. **`Gtk.Notebook` -> `AdwTabView`/`AdwTabBar`** — tabs become `TabPage`
   objects instead of integer indices. Deletes the stale-`page_num` bug
   class outright: `_reindex_tabs()` and the `page-reordered` handler
   added in v1.20.4 should both disappear.
3. **25 x `dialog.run()`** -> `AdwAlertDialog`, async-native.
4. **Menus** — 47 `Gtk.MenuItem` -> `Gio.Menu` + `GAction`.
5. **Mechanical sweep** — 191 `pack_start`/`pack_end` -> `append()`,
   41 `show_all()` deleted, 10 `Gtk.Stock` -> icon names,
   3 `Gtk.EventBox` deleted.
6. **Events** -> `GestureClick` / `EventControllerKey`.
7. **Vte 2.91 -> 3.91** — small; only `Vte.Terminal` and `Vte.PtyFlags`.
8. **Delete the `GdkX11` present-with-time hack** in `synpad.py`, and the
   `GDK_BACKEND=x11` pin at line 14. The payoff. **Done** — see "Task 6" below.

Also worth adopting: `AdwToast` for the suppressed-highlighting notice
from v1.21.2.

## Task 6 — `synpad.py`, launch, cleanup (the final task)

`synpad.py`'s `Gtk.Application` became `Adw.Application`; `Gtk.Application`'s
two `gi.require_version` pins moved to `'4.0'`/`'5'` and gained
`gi.require_version('Adw', '1')`. The single `show_all()` was deleted
(GTK4 widgets are visible by default) and the single `.run()` is unchanged
in shape (`app.run(sys.argv)`), since `Gio.Application.run()` did not
change between GTK3 and GTK4.

**Deleted outright, not preserved "just in case":**
- The `os.environ.setdefault('GDK_BACKEND', 'x11')` pin and its explanatory
  comment block (former lines ~7-14).
- The entire `_present_window()` `GdkX11`/`x11_get_server_time` block
  (former lines ~77-90), including its `gi.require_version('GdkX11', '3.0')`.
  `_present_window()` is now three lines: check `self.window`, call
  `self.window.present()`, return `False`.
- `Gdk.set_program_class("synpad")` — this call does not exist on GTK4's
  `Gdk` module at all (`WM_CLASS` is an X11-only concept GDK4 dropped from
  its cross-backend API). `GLib.set_prgname("synpad")` and
  `GLib.set_application_name("SynPad")` are unaffected and still run; on
  the X11/XWayland fallback path they still set the identifiers Mutter
  reads for window-manager-class matching.

**`Adw.init()` — findings, not assumed.** Verified empirically
(`Adw.is_initialized()` before/after, both with a bare `Adw.Application`
and by tracking the real `Adw.init` symbol through a monkeypatch): calling
`Adw.Application.do_startup(self)` — i.e. chaining up through
`do_startup()`, which every GTK3->GTK4 port in this codebase already does
— **already calls `Adw.init()` internally**, before the chain-up returns.
An explicit `Adw.init()` was added anyway, directly in
`SynPadApplication.do_startup()`, as a documented defensive
belt-and-suspenders call: `Adw.init()` is idempotent (calling it twice is a
no-op the second time), so it costs nothing, and it means "libadwaita
widgets render unstyled" fails loudly and immediately if `Adw.Application`'s
auto-init behaviour is ever a libadwaita-version-specific detail rather than
a documented guarantee.

### Launch verification (2026-09-09)

Under an isolated `$HOME`/`$XDG_CONFIG_HOME` (never the user's real config —
verified unchanged, md5 `82fb2b87edd9bfbcfbdae194c092b16a`, before and after
every launch in this section), `synpad.py` was started directly
(`python3 synpad.py`) in the real GNOME/Wayland session already running on
this machine (`XDG_SESSION_TYPE=wayland`, `XDG_CURRENT_DESKTOP=ubuntu:GNOME`)
with no `GDK_BACKEND` override of any kind — the whole point being that
nothing forces a backend choice any more.

- The process started clean, reached a steady sleeping state (`ps` state
  `Sl`), and stayed there — no crash, no traceback, no restart loop.
- Relaunched with `GDK_DEBUG=misc` for an authoritative, GDK-internal
  confirmation of backend selection. The log shows, verbatim: `Trying
  wayland backend`, a full Wayland global/output/dmabuf negotiation with
  the running Mutter compositor (including `interface xdg_activation_v1`
  among the advertised globals — the protocol GTK4 uses instead of the
  deleted `GdkX11`/X11-timestamp hack), and finally `Using wayland display
  wayland-0` / `Using OpenGL backend EGL`. No X11 fallback was attempted at
  any point.
- `xlsclients -a`, run immediately before and immediately after launch
  (both with and without `GDK_DEBUG`), never listed `synpad` — X11 clients
  (XWayland-backed GTK apps included) always appear in `xlsclients` because
  it queries the X server's own client list; a genuine native-Wayland client
  never opens an X connection and so can never appear there. Confirmed by
  process inspection too: `/proc/<pid>/fd` for the running `synpad` process
  showed no connection to either `/tmp/.X11-unix/X1` or `X2`, only Wayland
  protocol artifacts (`/memfd:mutter-shared (deleted)`,
  `/memfd:wayland-cursor (deleted)`, a socket to `$XDG_RUNTIME_DIR/wayland-0`).
- Both launches were closed cleanly with `SIGTERM` (`kill <pid>`); each time
  the process exited immediately with no orphaned child processes and no
  further output.
- No accessibility/window-listing tool was available in this sandbox to
  independently screenshot the mapped window (`wmctrl`/`xdotool` not
  installed; `gdbus … org.gnome.Shell.Eval` and
  `org.gnome.Shell.Introspect.GetWindows` both refused with
  `AccessDenied`/unsafe-mode-off; `grim` failed with `compositor doesn't
  support wlr-screencopy-unstable-v1`, expected on Mutter). The evidence
  above (GDK's own backend log, `xlsclients` absence, and the fd/socket
  inspection) is the strongest verification available in this environment
  and is unambiguous on the one question that mattered: native Wayland, not
  XWayland.

## Deps

Installed: GTK 4.14.5, libadwaita 1.5, `GdkWayland-4.0`.
Needed: `gir1.2-gtksource-5`, `gir1.2-vte-3.91`.

## Gotchas found in flight

Recorded as they bite, so later modules do not rediscover them.

- **`Gtk.TextBuffer.get_iter_at_line()` returns `(ok, iter)` in GTK4**, not a
  bare iter — silently breaks as `AttributeError: '_ResultTuple' object has
  no attribute 'copy'`. Fixed in `git_history.py`, `editor.py` (4 sites:
  lines ~1731, 1767, 1797, 1824) and `window.py` (1 site, `_console_log`'s
  500-line trim, ~line 1248). Same applies to `get_iter_at_line_offset()`
  and `get_iter_at_line_index()`. **All known sites are fixed as of Task 6.**
- **Cursors are a widget property.** `view.get_window(...)` + `win.set_cursor()`
  becomes `widget.set_cursor_from_name(name)`; `Gdk.Cursor.new_from_name()`
  no longer takes a display argument.
- **`GtkSource.View` installs its own `EventControllerFocus`.** A test that
  merely asserts one is present passes vacuously — count before and after.
- **Popovers hold exactly one parent.** Re-anchoring needs `unparent()`
  before `set_parent()`, or GTK warns and the popover misplaces.
- **`Gdk.set_program_class()` does not exist on GTK4's `Gdk` module.**
  `AttributeError` at call time (not at import time), so a half-hearted
  port that only fixes `gi.require_version` pins still crashes on launch.
  Removed from `synpad.py`; `GLib.set_prgname()`/`set_application_name()`
  are the cross-backend equivalents and were kept.
- **`Adw.Application.do_startup()`'s chain-up already calls `Adw.init()`.**
  Verified via `Adw.is_initialized()` before/after and by monkeypatching
  the `Adw.init` symbol itself. An explicit `Adw.init()` call is redundant
  once `Adw.Application` is in use, but harmless (idempotent) — kept as a
  defensive belt-and-suspenders in `synpad.py`'s `do_startup()`.
- **`GLib-GIO-CRITICAL: g_application_list_actions: assertion
  'application->priv->is_registered' failed`, then a segfault** —
  constructing a `Gtk.ApplicationWindow`/`SynPadWindow` with a real
  `application=` before that application has been registered
  (`Gio.Application.register()`, which `.run()` does for you) crashes the
  interpreter rather than raising. Headless tests that exercise
  `do_activate()`/`do_open()` against a real `SynPadApplication` must
  `register()` the app first (with `Gio.ApplicationFlags.NON_UNIQUE` added
  for the test process, and a throwaway `application_id` for any second
  instance in the same process, to avoid a same-session D-Bus object-path
  collision that two co-existing test `Adw.Application`s would otherwise
  hit).
- **`xlsclients` proves native-Wayland-vs-XWayland conclusively, cheaply.**
  It queries the X server's own client list, so any X11-backed client
  (XWayland included) always appears in it; a genuine native Wayland client
  never opens an X connection and so can never appear. No GUI automation
  tooling needed.

## Test suite

All 15 files in `tests/` pass under an isolated `$HOME`/`$XDG_CONFIG_HOME`
(see "Global Constraints" above — every file redirects `$HOME` before
importing `config`, and monkeypatches `save_config`/`secrets_store` besides):

```
test_claude_tab_gtk4.py        ALL PASS
test_compare_gtk4.py           ALL PASS
test_completion_gsv5.py        ALL PASS
test_connection_gtk4.py        ALL PASS
test_connection_liveness.py    6/6 passed
test_dialogs_gtk4.py           ALL PASS
test_editor_gtk4.py            ALL PASS
test_git_history_gtk4.py       ALL PASS
test_local_files_gtk4.py       ALL PASS
test_long_line_highlight.py    9/9 passed
test_remote_gtk4.py            ALL PASS
test_signature_help_gtk4.py    ALL PASS
test_synpad_gtk4.py            ALL PASS   (new — Task 6)
test_tab_reorder.py            ALL PASS
test_terminal_tab_gtk4.py      ALL PASS
test_window_gtk4.py            ALL PASS
```

`test_synpad_gtk4.py` covers, against real production code (not stubs):
`application_id` and `HANDLES_OPEN` preserved; `SynPadApplication` actually
subclasses `Adw.Application` (mutation-sensitive — a plain `Gtk.Application`
would pass a weaker `isinstance` check since every `Adw.Application` already
is one); `Adw.init()` is called by `do_startup()` itself, tracked by
wrapping the real `Adw.init` symbol (not inferred from global
`Adw.is_initialized()` state, which every other test file in the suite
already flips true at import time); the X11 hack's removal, asserted
against the live source text so a reintroduction is caught even as dead
code; and `do_activate()`/`do_open()` driven end-to-end against a real,
registered `SynPadApplication` and a real `SynPadWindow` — a temp file is
actually opened into a tab via the production `open_or_focus_file` path,
window construction is proven idempotent across repeated activation, and
the idle-queued present is proven to actually call `.present()`. All four
of the file's central assertions (X11-pin removal, `Adw.init()` call,
`Adw.Application` subclassing, real `do_open()` file-opening) were verified
by mutation: each was individually reverted in a scratch copy and confirmed
to turn its corresponding assertion red, then restored.

## Manual smoke test (do this once, running v2 as your daily editor)

The headless suite cannot drive real input devices, real Vte key delivery,
or pointer drag/middle-click, so the following need one pass by hand before
trusting v2 day to day:

- **`Ctrl+Shift+A`** (Ask Claude) with the cursor in an editor tab — this
  was dead before the Task 5 CAPTURE-phase fix (GtkSource's own
  `ShortcutController` was winning at BUBBLE phase).
- **`F12` / `Ctrl+W` / `Ctrl+Q` / `Ctrl+S`** with focus inside a Vte
  terminal tab. The terminal half of the CAPTURE-phase fix has *no*
  automated coverage: Vte's own key controller returns `False` for every
  key regardless of phase, so a headless test can't distinguish "the fix
  works" from "Vte swallows everything anyway" — this can only be checked
  by hand.
- **Tab-to-expand** (type a docblock `/**` then press Tab) while the
  completion popup is open — another CAPTURE-phase key-ordering case.
- **`Adw.TabBar` middle-click-close and drag-to-reorder** — stock
  libadwaita behaviour, but no pointer device exists in this sandbox to
  exercise it.
- **Tools-pane header buttons** (Clear / New terminal / Stop Claude /
  Detach) sit flush right, as intended.
- **Quit with unsaved changes**, both answers (save-and-quit, discard).
- **The intermittent Chrome-paste hang** this whole port exists to fix.
  This is the one item that isn't a quick manual check — it needs real use
  over days as the daily editor, per the "Why" section above. Not yet
  reproduced under instrumentation even on GTK3; absence of the hang after
  a few days of native-Wayland use is the actual signal, not a single
  paste.
