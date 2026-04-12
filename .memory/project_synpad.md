---
name: SynPad project
description: GTK3 Python code editor with FTP/SFTP support - active development project on GitHub
type: project
originSessionId: 755c183c-86ca-4b4e-b566-7d088fcb2c9d
---
SynPad is a lightweight code editor (mini IDE) built in Python with GTK3 + GtkSourceView.

**Location:** `~/Development/Local/Synpad/repo/` (git repo, working dir on `dev` branch)
- GitHub: https://github.com/mbeulens/synpad
- Archive of old version folders: `~/Development/Local/Synpad/archive/`
- Desktop launcher: `~/.local/share/applications/synpad.desktop` (points to repo)
- Config: `~/.config/synpad/config.json`
- Session: `~/.config/synpad/session.json`
- Custom icon: `synpad.svg` in repo (green gradient notepad + yellow pencil)

**Current version:** v1.16.0 on both dev and main
**Branch:** Always work on `dev`, merge to `main` for releases

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

**Git config:** user=mbeulens, email=m.beulens@syntec-it.nl

**Why:** User's personal tool for remote and local code editing.

**How to apply:** Work in ~/Development/Local/Synpad/repo/synpad.py on dev branch. Commit and push after each change. Merge to main for releases.
