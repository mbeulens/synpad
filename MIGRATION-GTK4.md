# SynPad 2.0 — GTK4 + libadwaita migration

Branch `gtk4`, versioned `2.0.0-dev`. `dev` stays GTK3 and shippable for
1.21.x patches; merge `dev` -> `gtk4` regularly so this branch does not rot.

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
   `GDK_BACKEND=x11` pin at line 14. The payoff.

Also worth adopting: `AdwToast` for the suppressed-highlighting notice
from v1.21.2.

## Deps

Installed: GTK 4.14.5, libadwaita 1.5, `GdkWayland-4.0`.
Needed: `gir1.2-gtksource-5`, `gir1.2-vte-3.91`.
