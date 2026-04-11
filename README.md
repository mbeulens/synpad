# SynPad

A lightweight PHP/JS IDE with FTP/SFTP integration for Linux, built with Python and GTK3.

## Features

### Editor
- **Syntax highlighting** for PHP, JS, TS, Python, HTML, CSS, JSON, SQL, YAML, Markdown, and more
- **Code completion** for PHP and JS/TS with function signatures and type hints
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
- **Smart save** — auto-switches server connection when saving a file from a different server
- **File tree** auto-navigates to the correct directory after server switch
- **File operations** — create, rename, delete files and directories, chmod permissions
- **GUID-based** server references — rename profiles without breaking anything

### Local Files
- **Open local files** (Ctrl+O) with file type filters
- **Create new local files** (Ctrl+N)
- **Smart Ctrl+S** — saves locally for local files, uploads for remote files

### UI & Customization
- **Dark/light theme** toggle for the entire application
- **Multiple color schemes** — Classic, Cobalt, Oblivion, Solarized, Tango, Yaru, and more
- **Custom color editor** — per-element foreground, background, bold, italic with save/load
- **Draggable resizable panes** — rearrange function list, editor, and file tree
- **Console pane** — collapsible log of all FTP/SFTP operations with color-coded output
- **Version display** in title bar

## Dependencies

### Debian/Ubuntu
```bash
sudo apt install python3 python3-gi gir1.2-gtksource-3.0 python3-paramiko
```

### Fedora
```bash
sudo dnf install python3-paramiko python3-gobject gtksourceview3
```

### Arch
```bash
sudo pacman -S python-paramiko python-gobject gtksourceview3
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

### Desktop Launcher (Linux)

Create `~/.local/share/applications/synpad.desktop`:
```ini
[Desktop Entry]
Name=SynPad
Comment=Lightweight PHP IDE with FTP/SFTP
Exec=python3 /path/to/synpad.py
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
