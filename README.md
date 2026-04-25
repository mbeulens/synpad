# SynPad

A lightweight PHP/JS IDE with FTP/SFTP integration for Linux, built with Python and GTK3.

## Features

### Editor
- **Syntax highlighting** for PHP, JS, TS, Python, HTML, CSS, JSON, SQL, YAML, Markdown, and more
- **Code completion** for PHP (1024+ functions, sourced from JetBrains phpstorm-stubs) and JS/TS with function signatures and type hints
- **Signature help popover** — shows the function signature under the cursor while typing inside a call, with the current parameter highlighted
- **Docblock generation** — type `/**` above a function and press Tab to auto-generate PHPDoc or JSDoc
- **Function outline** pane with click-to-navigate
- **Find & Replace** with match case and regex support (Ctrl+F / Ctrl+R)
- **Go to Line** (Ctrl+G)
- **JSON and XML** pretty print
- **Tab management** — right-click context menu with close, close all, close all but this
- **Session persistence** — all open tabs restore on restart, including unsaved changes
- **Compare Tabs** — side-by-side diff view with color coding, synced scrolling, change navigation, and minimap

### Remote File Management
- **FTP & SFTP** connection with SSH key auth (RSA, Ed25519, ECDSA)
- **Server profiles** with save, rename, delete, and quick connect
- **Server groups** — organize profiles into groups with submenu quick connect
- **Smart save** — auto-switches server connection when saving a file from a different server
- **Conflict detection** — SHA256 hash check before every upload, warns if server file changed
- **File tree** auto-navigates to the correct directory after server switch
- **File operations** — create, rename, delete files and directories, chmod permissions
- **GUID-based** server references — rename profiles without breaking anything

### Local Files
- **Local file browser** — toggle between Remote/Local in the Files pane
- **Path bar** with up button — browse anywhere on the filesystem
- **Local file management** — create, rename, delete files/directories, chmod
- **Open local files** (Ctrl+O) with file type filters
- **Create new local files** (Ctrl+N)
- **Smart Ctrl+S** — saves locally for local files, uploads for remote files
- **Smart file opening** — text files in editor, binary files with system default app
- **File Types manager** — configure which extensions open in the editor

### UI & Customization
- **Dark/light theme** toggle for the entire application
- **Multiple color schemes** — Classic, Cobalt, Oblivion, Solarized, Tango, Yaru, and more
- **Custom color editor** — per-element foreground, background, bold, italic with save/load
- **Draggable resizable panes** — rearrange function list, editor, and file tree
- **Console pane** — collapsible log of all FTP/SFTP operations with color-coded output (toggle with F12)
- **Single-instance mode** — opening a file from the file manager or terminal forwards it to the running window as a new tab; dirty tabs prompt before reload
- **Version display** in title bar

## Dependencies

### Debian/Ubuntu
```bash
sudo apt install python3 python3-gi gir1.2-gtksource-3.0 gir1.2-vte-2.91 python3-paramiko python3-secretstorage
```

### Fedora
```bash
sudo dnf install python3-paramiko python3-gobject gtksourceview3 vte291 python3-secretstorage
```

### Arch
```bash
sudo pacman -S python-paramiko python-gobject gtksourceview3 vte3 python-secretstorage
```

Or run the installer:
```bash
chmod +x install.sh
./install.sh
```

## Usage

```bash
python3 synpad.py
```

Open a file directly:
```bash
python3 synpad.py /path/to/file.php
```

### Desktop Launcher (Linux)

Create `~/.local/share/applications/synpad.desktop`:
```ini
[Desktop Entry]
Name=SynPad
Comment=Lightweight PHP IDE with FTP/SFTP
Exec=python3 /path/to/synpad.py %f
Icon=accessories-text-editor
Terminal=false
Type=Application
Categories=Development;TextEditor;
StartupNotify=false
StartupWMClass=synpad
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+N | New local file |
| Ctrl+O | Open local file |
| Ctrl+S | Save (local or upload) |
| Ctrl+F | Find |
| Ctrl+R | Find & Replace |
| Ctrl+G | Go to line |
| Ctrl+W | Close tab |
| Ctrl+Q | Quit |
| Tab (on `/**`) | Generate docblock |
| Tab (on `///`) | Insert separator line |
| Escape | Close search / console |

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for full release history.

## License

MIT
