# SynPad 2.0 — GTK4 + libadwaita port

## Spec

Port SynPad from GTK3/GtkSourceView3/Vte2.91 to GTK4/GtkSourceView5/Vte3.91
plus libadwaita 1.5, preserving **every** existing behaviour. The GTK3 code on
`dev` is the specification: if behaviour differs, that is a bug, except where
this plan explicitly calls for an libadwaita replacement.

Motivation: GTK3 cannot satisfy both known bugs at once on this machine.
Ubuntu ships no `GdkWayland-3.0` typelib, so GTK3 Python cannot apply
xdg-activation tokens — hence the `GDK_BACKEND=x11` pin in `synpad.py`, which
puts Mutter's XWayland clipboard bridge in the paste path and is the prime
suspect for the intermittent Chrome-paste hang. GTK4 ships `GdkWayland-4.0`
and handles activation internally.

## Global Constraints

1. **Every module must end on `gi.require_version('Gtk', '4.0')`.** GTK3 and
   GTK4 cannot share a process, so a half-ported tree cannot launch. Do not
   leave a module partially converted.
2. **Preserve behaviour exactly.** Same menu items, same shortcuts, same
   dialog text, same status messages, same defaults. Do not "improve" UX,
   rename anything user-visible, or drop features.
3. **Every task ships headless tests** in `tests/test_<module>_gtk4.py`,
   following the existing style: `Gtk.init_check()` guard, `check(name, cond)`
   helper, exit 1 on any failure, `ALL PASS` on success. Tests must exercise
   the *ported* surface, not just import the module.
4. **Do not touch `config.py`'s `APP_VERSION`.** The controller owns version
   bumps; concurrent edits would conflict.
5. **Do not modify already-ported modules**: `completion.py`,
   `signature_help.py`, `git_history.py`. They are done and tested.
6. **No new dependencies.** GTK 4.14.5, libadwaita 1.5, GtkSourceView 5,
   Vte 3.91 are installed; nothing else may be added.
7. **Never dispatch subagents of your own.** Review comes from the controller.

## Established patterns — use these verbatim

Banked from the three modules already ported. Do not re-derive them.

| GTK3 | GTK4 |
|---|---|
| `gi.require_version('Gtk','3.0')` | `'4.0'` |
| `gi.require_version('GtkSource','3.0')` | `'5'` |
| `gi.require_version('Vte','2.91')` | `'3.91'` |
| `box.pack_start(w, e, f, p)` | `box.append(w)` (+ `w.set_hexpand/set_vexpand` for expand, `set_margin_*` for padding) |
| `box.pack_end(w, ...)` | `box.append(w)` + `w.set_halign(Gtk.Align.END)` |
| `container.add(w)` | `set_child(w)` (Window/ScrolledWindow/Popover/Frame) or `append(w)` (Box) |
| `w.show_all()` / `w.show()` | delete — widgets are visible by default |
| `w.hide()` | `w.set_visible(False)` |
| `Gtk.EventBox` | delete the wrapper; add controllers to the child directly |
| `connect('button-press-event', h)` | `Gtk.GestureClick()`, `connect('pressed', h)`, `w.add_controller(g)`; handler `(gesture, n_press, x, y)` |
| `event.button != 1` guard | `gesture.set_button(1)` |
| `connect('motion-notify-event', h)` | `Gtk.EventControllerMotion()`, `'motion'`, handler `(ctrl, x, y)` |
| `connect('leave-notify-event', h)` | same controller, `'leave'`, handler `(ctrl,)` |
| `connect('key-press-event', h)` | `Gtk.EventControllerKey()`, `'key-pressed'`, handler `(ctrl, keyval, keycode, state)` |
| `connect('focus-out-event', h)` | `Gtk.EventControllerFocus()`, `'leave'` |
| `widget.get_window()` + `win.set_cursor()` | `widget.set_cursor_from_name(name)` |
| `Gdk.Cursor.new_from_name(display, n)` | `Gdk.Cursor.new_from_name(n, None)` |
| `Gtk.STOCK_*` | icon names (`'document-save'`, `'document-open'`, `'gtk-cancel'` → `'process-stop'`, etc.) |
| `dialog.run()` + `dialog.destroy()` | `Adw.AlertDialog` + `.choose(parent, None, callback)`, async |
| `Gtk.MessageDialog` | `Adw.AlertDialog` (`.set_heading`, `.set_body`, `.add_response`, `.set_response_appearance`) |
| `Gtk.Menu` + `Gtk.MenuItem` | `Gio.Menu` model + `Gtk.PopoverMenu`, actions via `Gio.SimpleAction` on the window's action group |
| `menu.popup_at_pointer(ev)` | `popover.set_pointing_to(rect)` + `popover.popup()` |
| `Gtk.Notebook` | `Adw.TabView` + `Adw.TabBar` (see Task 5) |
| `buf.get_iter_at_line(n)` | **returns `(ok, iter)`** — unpack it |
| `Gtk.Window(title=...)` then `.add()` | `Gtk.Window(title=...)` then `.set_child()` |
| `win.set_position(...)` / `win.move(...)` | removed — delete; the compositor places windows |
| `Gtk.Dialog` with `add_button` | `Adw.AlertDialog` or `Gtk.Window` + explicit buttons |

### Known gotchas

- `GtkSource.View` installs its own `EventControllerFocus` — a test asserting
  "a focus controller exists" passes vacuously. Count before vs after.
- GTK4 popovers hold exactly one parent: `unparent()` before `set_parent()`.
- `Adw.AlertDialog.choose()` is async. Callers that relied on `run()`'s return
  value must be restructured into callbacks. Preserve the decision logic
  exactly; only the control flow changes.
- `Gtk.init_check()` returns a plain bool in GTK4.

### Gotchas harvested from Task 1 (leaf UI modules)

- **`pack_end()` reverses call order; `Gtk.Dialog.add_button()` preserves it.**
  When replacing either with `.append()`, button order depends on which it
  was. Guessing wrong silently swaps two buttons. Verify empirically.
- **`Gtk.Button.set_image()` is gone** — use `set_icon_name()`.
  `Gtk.Image.new_from_icon_name()` no longer takes a size; use
  `image.set_pixel_size(n)` (`Gtk.IconSize` is now INHERIT/NORMAL/LARGE only).
- **`Gtk.ReliefStyle` / `set_relief()` are gone** — use
  `widget.add_css_class('flat')`.
- **`set_no_show_all()` is gone** — use `set_visible()` directly.
- **`Vte.Terminal.spawn_async()` did NOT change shape** between 2.91 and 3.91
  on this install. Verified by introspection and a live call. Keep the
  existing 11-positional-arg call.
- **A custom-content dialog is a `Gtk.Window` + explicit buttons, not
  `Adw.AlertDialog`.** `Adw.AlertDialog` fits heading/body/responses only;
  anything with real body widgets (checkbox grids, previews) uses a Window.
- **`Gtk.Popover.popup()` segfaults if its widget has no realized toplevel.**
  In tests, host the widget in a real `Gtk.Window` before showing a popover.
  This crashes the interpreter, it does not raise.
- **`Gtk.Widget` has no `get_action_group()`.** In tests, invoke actions via
  `widget.activate_action('prefix.name', None)`.
- **A bare `Gtk.Window()`'s child tree contains an internal CSD close button.**
  Filter widget-tree walks by `get_label()` truthiness.
- **A plain `Gtk.Window` installs its own `Gtk.EventControllerKey`** (for
  mnemonics). Tests must assert "at least one" and drive every match rather
  than indexing `[0]` — same shape as the `GtkSource.View` focus-controller
  trap above.
- **`Gtk.Dialog` has a built-in `close` action-signal bound to Escape;
  `Gtk.Window` has none.** Any `Gtk.Dialog` -> `Gtk.Window` conversion must
  re-add Escape handling explicitly (an `EventControllerKey` routed to the
  same cancel path as the Cancel button), or Escape silently stops working.
  For `Adw.AlertDialog`, use `set_close_response(...)`.
- **To test a dialog built inside a mixin method**, monkeypatch
  `gi.repository.Gtk.Window` to a capturing subclass for the call; the mixin
  host must itself be a real `Gtk.Window` subclass (dialogs pass
  `transient_for=self`).

## Tasks

### Task 1 — Leaf UI modules: `local_files.py`, `terminal_tab.py`, `claude_tab.py`

Port all three. They share the same shape: box packing, `show_all`, one
context menu, a couple of event handlers, and (terminal_tab) Vte.

- `local_files.py` (409): 8 packing sites, 2 `show_all`, 12 `Gtk.Menu*`,
  1 `.run()`. The file-browser context menu becomes `Gio.Menu` +
  `Gtk.PopoverMenu`. Keep every menu item and its label identical.
- `terminal_tab.py` (269): 3 packing, 4 `show_all`, 1 `.run()`,
  2 event handlers, 2 `Gtk.EventBox`. Bump Vte to `3.91`. Check
  `Vte.Terminal.spawn_async()`'s GTK4 signature and adapt.
- `claude_tab.py` (362): 7 packing, 2 `show_all`, 1 `.run()`.

Tests: `tests/test_local_files_gtk4.py`, `test_terminal_tab_gtk4.py`,
`test_claude_tab_gtk4.py`.

### Task 2 — Dialogs: `dialogs.py`, `connection.py`

- `dialogs.py` (533): 32 packing, 3 `show_all`, **4 `.run()`**, 2 `Gtk.STOCK`.
  This is where the `Adw.AlertDialog` pattern is set for the whole codebase.
  Every `.run()` becomes an async `.choose()` with a callback. Callers in
  other modules will be updated in their own tasks — export a clear async
  API and document each function's new callback signature in the report.
- `connection.py` (801): 6 packing, 1 `show_all`, 2 `.run()`, 4 `Gtk.STOCK`.
  Mostly non-GTK logic; only the UI surface changes.

Tests: `tests/test_dialogs_gtk4.py`, `tests/test_connection_gtk4.py`.

### Task 3 — `compare.py`, `remote.py`

- `compare.py` (635): 37 packing, 3 `show_all`, 1 `.run()`, 2 events,
  1 `EventBox`. Two `Gtk.Window(...)` constructions need `set_child()`.
- `remote.py` (851): 13 packing, 4 `show_all`, 4 `.run()`, 17 `Gtk.Menu*`.
  Use the `Adw.AlertDialog` API from Task 2 — do not invent a second pattern.

Tests: `tests/test_compare_gtk4.py`, `tests/test_remote_gtk4.py`.

### Task 4 — `editor.py`

1,812 lines: 32 packing, 7 `show_all`, 6 `.run()`, 10 `Gtk.Menu*`, 3 events,
4 `Gtk.STOCK`, 5 `EventBox`.

- Fix the **4 outstanding `get_iter_at_line()` tuple-unpack sites**
  (~lines 1558, 1572, 1602, 1629).
- `GtkSource` is already at 5 via `completion.py` — match it.
- Keep `_register_bundled_languages()` and the bundled TypeScript spec
  working (v1.21.1 feature).
- Keep the `MAX_HIGHLIGHT_LINE_LEN` long-line guard and its right-click
  "Enable Syntax Highlighting (slow)" item (v1.21.2 feature).
- Do **not** convert `Gtk.Notebook` here — that is Task 5.

Tests: extend `tests/test_editor_gtk4.py`; port `tests/test_long_line_highlight.py`.

### Task 5 — `window.py` + `Adw.TabView`

1,168 lines: 53 packing, 14 `show_all`, 4 `.run()`, 18 `Gtk.Menu*`, 3 events.

Replace `Gtk.Notebook` with `Adw.TabView` + `Adw.TabBar` across `window.py`
and `editor.py`'s tab helpers. `Adw.TabView` addresses tabs by `Adw.TabPage`
object, not integer index — so the stale-`page_num` workarounds added in
v1.20.4 (`_reindex_tabs()` and the `page-reordered` handler) should be
**deleted**, not ported. Verify tab reorder, close, and switch still work.

Also fix the 1 outstanding `get_iter_at_line()` site (~line 1082).
Convert the main menu to `Gio.Menu` + `Gio.SimpleAction`, preserving every
item, label and accelerator.

Tests: `tests/test_window_gtk4.py`; port `tests/test_tab_reorder.py` to
`Adw.TabView`.

### Task 6 — `synpad.py`, launch, cleanup

- Port `synpad.py` (103): `Gtk.Application` → `Adw.Application`, `Adw.init()`.
- **Delete the `GDK_BACKEND=x11` pin (line ~14) and the entire `GdkX11`
  present-with-time block (~lines 77-90).** GTK4 handles xdg-activation
  internally; this hack is the thing the port exists to remove.
- Confirm the app launches natively on Wayland and is absent from `xlsclients`.
- Run the whole test suite; every test file must pass.
- Update `MIGRATION-GTK4.md` to mark the port complete.
