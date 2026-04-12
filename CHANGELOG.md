# Changelog

All notable changes to SynPad are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [1.15.0] - 2026-04-12

### Added
- Open files via "Open with" from the file manager — SynPad now accepts a file path as a command-line argument
- Desktop launcher updated with `%f` to receive file paths from the system

## [1.14.0] - 2026-04-12

We skip 13 to be sure to be sure :)

## [1.13.0] - 2026-04-12

### Added
- Local file browser — toggle between Remote/Local with icons in Files pane
- Local path bar with up button — browse anywhere on the filesystem
- Local file tree context menu: New File, New Directory, Rename, Permissions, Delete
- Smart file opening — text files open in editor, binary files open with system app (xdg-open)
- File Types manager — configure which extensions open in editor (Menu > File Types)
- ~40 default editor extensions covering code, config, and text formats
- Remote files with non-editor extensions download and open externally

### Fixed
- Rename dialog pre-fills current name and shows "Rename" button
- Server Manager opens with clean form

## [1.12.0] - 2026-04-12

### Added
- Server groups — group server profiles and quick connect shows submenus
- Profile name as a form field — no more separate rename dialog
- SHA256 hash verification before every upload — prevents overwriting server changes
- Conflict dialog with 4 options: Overwrite, Use Remote, Compare, Cancel
- Conflict compare view — side-by-side diff of local changes vs server version with action buttons
- Remote file stats (mtime, hash) saved in session for restored tabs
- All conflict check actions logged to console (COMPARE GET, hash, OK/CHANGED)

### Fixed
- Server Manager opens with clean form instead of last used server
- Form fields clear when selecting "(New connection)"
- Session-restored tabs now check for server changes before first upload

## [1.11.0] - 2026-04-12

### Added
- Side-by-side file compare (Menu > Compare Tabs)
  - Color-coded: yellow=changed, red=deleted, green=added
  - Line numbers on both sides
  - Synced scrolling between left and right panes
  - Change minimap with colored markers (clickable to jump)
  - Prev/Next change navigation buttons with block counter
  - Tab selector shows server/path info to distinguish same-named files
- Custom SVG icon — green gradient notepad with yellow pencil
- `///` + Tab snippet expands to separator line
- Debug mode toggle in menu (logs to console)
- `.gitignore` for `__pycache__`

### Fixed
- Tab close X button using stale page number after reindexing
- Untitled tab label not updating after Save As (closure bug)
- Untitled counter now reuses numbers when tabs are closed
- Open/Save dialogs remember last used folder
- `///` + Tab works anywhere (completion popup dismissed first)
- Menu renamed: "Settings" → "Server Manager"
- Diff block counting consistent between nav bar and status bar

## [1.10.0] - 2026-04-11

Maintenance release with debug mode, icon, and bug fixes.

## [1.9.0] - 2026-04-11

### Added
- Collapsible console pane with FTP/SFTP command logging (connect, disconnect, list, get, put, mkdir, delete, rename, chmod)
- Console: color-coded messages (green=success, red=error, gray=timestamps), reverse order (newest first), 500 line limit, clear button
- Server profiles with GUIDs — rename freely without breaking references
- Rename button in connection dialog for server profiles
- Smart save — auto-switches server connection when saving a file that belongs to a different server
- File tree auto-navigates to the uploaded file's directory after server switch
- Session save/restore — all open tabs persist across restarts with content preserved
- Docblock generation for PHP and JS/TS — type `/**` above a function and press Tab
- Code completion for PHP and JS/TS with function signatures and type hints
- Find window (Ctrl+F) with match case and regex checkboxes
- Find & Replace window (Ctrl+R) with Replace and Replace All
- Go to Line dialog (Ctrl+G)
- JSON pretty print (menu item)
- XML pretty print (menu item)
- Open local file (Ctrl+O) with file type filters
- Create new local file (Ctrl+N)
- Smart Ctrl+S — saves locally for local files, uploads for remote files
- Remote file management: create file, create directory, rename, delete (with confirmation), chmod
- Right-click context menu on file tree with all file operations
- Permissions dialog with checkbox grid and octal input for chmod
- Right-click highlights the selected item in file tree
- Version number in title bar (APP_VERSION)
- Desktop launcher (.desktop file) with StartupWMClass

### Fixed
- Quick connect dropdown now updates after rename/save/delete in connection dialog
- SFTP thread collision on auto-switch upload (serialized operations)
- Empty file upload on server switch (content saved before switching)
- NoneType errors after closing tabs during upload
- Function list no longer shows `if`, `while`, `for` etc. as functions in JS
- Symbol list click navigates to exact line (uses character offset)
- No horizontal scroll when clicking function in symbol list
- GTK widget realized warnings suppressed
- GtkSourceView `#black` color validation
- Markup escaping for `&`, `<`, `>` in completion signatures

## [1.8.0] - 2026-04-11

Initial GitHub release. All features from V0.1 through V1.0.0 consolidated.

### Added
- FTP connection with host, port, username, password
- SFTP connection via paramiko with password and SSH key auth (RSA, Ed25519, ECDSA)
- Remote file tree browser with lazy-loading directories on expand
- Code editor with GtkSourceView syntax highlighting (PHP, JS, TS, Python, HTML, CSS, JSON, SQL, YAML, Markdown, and more)
- Tab support for multiple open files with modified indicators
- File size check before upload with configurable max size
- Dark theme (oblivion) with monospace font
- Credentials saved to ~/.config/synpad/config.json
- Keyboard shortcuts: Ctrl+S (save/upload), Ctrl+W (close tab), Ctrl+Q (quit)
- In-place file upload (sftp.open write) to avoid permission denied errors
- FTP uses CWD + STOR for server compatibility
- Auto-detect home directory (sftp.normalize('.') / ftp.pwd())
- Manual home directory override in connection dialog
- Server profiles with save/load/delete and quick connect dropdown
- Function/symbol outline pane with click-to-navigate (PHP, JS, TS, Python)
- Draggable resizable panes with arrow-button reordering
- Dark/light theme toggle (full GTK + editor)
- Multiple GtkSourceView color schemes with picker dialog
- Custom color editor with per-element foreground, background, bold, italic
- Save/load multiple custom color schemes
- Tab right-click context menu: Close, Close All, Close All But This
- Hamburger menu with all actions
- Folder expand fix — no more glitch when clicking directories

## Pre-release versions (archive)

### V0.9 (V1.0.0 candidate)
- Draggable pane reordering with persistent layout

### V0.8
- Custom color scheme editor with save/load
- Fixed deprecation warnings

### V0.7
- Dark/light theme toggle for full application
- Color scheme picker with live preview

### V0.6
- Hamburger menu with Settings, Save & Upload, Quit
- Tab right-click context menu (Close, Close All, Close All But This)
- Connection controls moved to Files pane

### V0.5
- Function/symbol outline pane (left side)
- Removed layout swap button, fixed pane layout

### V0.4
- Arrow buttons to reorder panes
- Fixed segfault on pane rearrangement

### V0.3
- SFTP support with SSH key authentication
- Permission denied fix (in-place file write)
- Folder expand glitch fix
- Auto-detect home directory
- Swappable layout (file tree left/right)

### V0.2
- Install script for dependencies

### V0.1
- Initial prototype: FTP connection, file tree, code editor with syntax highlighting
