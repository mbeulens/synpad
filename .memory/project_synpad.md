---
name: SynPad project
description: Python code editor with FTP/SFTP - GTK3 on dev/main, GTK4+libadwaita port on the gtk4 branch under daily trial
type: project
originSessionId: 755c183c-86ca-4b4e-b566-7d088fcb2c9d
---
SynPad is a lightweight code editor (mini IDE) built in Python with GTK3 + GtkSourceView.

**Location:** `~/Development/Local/Synpad/repo/` (git repo, `dev` branch — GTK3, stable)
**GTK4 port:** `~/Development/Local/Synpad/repo-gtk4/` (git worktree, `gtk4` branch)
- GitHub: https://github.com/mbeulens/synpad
- Archive of old version folders: `~/Development/Local/Synpad/archive/`
- Desktop launchers: `synpad.desktop` (green icon, v1/GTK3/XWayland, `StartupWMClass=synpad`)
  and `synpad-2.0.desktop` (blue icon, v2/GTK4/native Wayland,
  `StartupWMClass=com.mbeulens.synpad`). Both go via `~/.local/bin/synpad`
  and `~/.local/bin/synpad-2.0` so a branch checkout cannot repoint them.
  Diagnostics: `~/.local/bin/synpad-2.0-debug` (crash + completion logging to
  /tmp/synpad2-crash.log) and `synpad-2.0-x11` (XWayland comparison run).
- Config: `~/.config/synpad/config.json`
- Session: `~/.config/synpad/session.json`
- Custom icon: `synpad.svg` in repo (green gradient notepad + yellow pencil)

**Current version:** v1.16.1 on dev (v1.18.0 tag exists on main from memory-only releases — running code is v1.16.1)
**Branch:** Always work on `dev`, merge to `main` for releases

**SynPad 2.0 — GTK4 + libadwaita port (branch `gtk4`, 42 commits, NOT merged).**
Started 2026-09-09. All 13 modules on GTK4/GtkSourceView 5/Vte 3.91/libadwaita,
17 test files passing. Plan and findings: `docs/superpowers/plans/gtk4-migration.md`
and `MIGRATION-GTK4.md` in the worktree.
- **Why:** GTK3 could not satisfy both known bugs at once. Ubuntu ships no
  GdkWayland-3.0 typelib, so GTK3 Python cannot apply xdg-activation tokens —
  hence the `GDK_BACKEND=x11` pin, which put Mutter's XWayland clipboard bridge
  in the paste path and was the prime suspect for the intermittent Chrome-paste
  hang. GTK4 handles activation internally; the pin and the GdkX11 hack are gone.
- **Status as of 2026-09-09 23:30:** user is running v2 daily for a few days
  before deciding on merge. Unproven: whether the Chrome-paste hang is actually
  fixed. Smoke-test list is in MIGRATION-GTK4.md.
- **Known accepted regression:** the completion list shows bare names, not
  `name (signature)`. GtkSourceView 5's completion provider cannot be
  implemented from PyGObject — a minimal do-nothing provider segfaults (six
  variants tested). Signature help still shows signatures.
- **v1.20.4's tab workarounds are deleted:** Adw.TabView keys tabs by TabPage
  object, so `_reindex_tabs()` and the page-reordered handler are gone; the
  stale-page_num bug class is structurally impossible.

**Architecture (v1.16.0):** Modular — split from monolith into 13 files:
- synpad.py (entry point), config.py, tab.py, symbols.py, completion.py
- connection.py (FTP/SFTP + dialog), session.py, local_files.py
- compare.py, remote.py, editor.py, dialogs.py, window.py (main window + mixins)
- SynPadWindow uses mixin classes: EditorMixin, RemoteMixin, LocalFilesMixin, CompareMixin, DialogsMixin, SessionMixin
- APP_VERSION and DEBUG_MODE live in config.py
- Rollback tag: v1.15.0-monolith (last single-file version)

**Features (v1.16.0, formerly v1.14.2):**
- FTP & SFTP with server profiles (GUID-based), groups, quick connect submenus, SSH key auth
- Smart save — auto-switches server when saving file from different server
- SHA256 hash conflict detection before every upload with compare/overwrite/use-remote options
- Syntax highlighting for 15+ languages
- Code completion for PHP and JS/TS with function signatures
- Docblock generation (/** + Tab), separator snippet (/// + Tab)
- Function outline pane with click-to-navigate
- Find & Replace (Ctrl+F/R) with regex, Go to Line (Ctrl+G)
- JSON and XML pretty print
- Compare Tabs — side-by-side diff with colors, line numbers, minimap, change navigation
- Local file browser with Remote/Local toggle, path bar, up button
- Local file management: create, rename, delete, chmod (same as remote)
- Smart file opening: text→editor, binary→xdg-open, configurable via File Types manager
- Local file editing (Ctrl+O open, Ctrl+N new untitled, Ctrl+S smart save)
- Session persistence — tabs restore on restart with hash/mtime
- Console pane with FTP/SFTP command + conflict check logging
- Debug mode toggle in menu
- Custom color schemes with separate Dark/Light mode tabs, save/load
- Dark/light theme toggle with matching custom colors
- Editor background color customizable per theme mode
- Draggable resizable panes with arrow-button reordering
- Custom SVG icon (synpad.svg + icon.alternative.svg)
- Version number in title bar
- Single-instance mode (v1.16.1) — second launches forward files to the running window via Gtk.Application + HANDLES_OPEN; dirty tabs prompt on re-open with Reload/Cancel

**Git config:** user=mbeulens, email=m.beulens@syntec-it.nl

**Why:** User's personal tool for remote and local code editing.

**How to apply:** Work in ~/Development/Local/Synpad/repo/synpad.py on dev branch. Commit and push after each change. Merge to main for releases.
