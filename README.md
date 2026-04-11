# SynPad

A lightweight PHP/JS IDE with FTP/SFTP integration for Linux, built with Python and GTK3.

## Features

- **FTP & SFTP** connection with server profiles and quick connect
- **Syntax highlighting** for PHP, JS, TS, Python, HTML, CSS, JSON, SQL, and more
- **Code completion** for PHP and JS/TS with function signatures and type hints
- **Function outline** pane with click-to-navigate
- **Find & Replace** with regex support (Ctrl+F / Ctrl+R)
- **Go to Line** (Ctrl+G)
- **JSON and XML** pretty print
- **Remote file management** — create, rename, delete files/directories, chmod
- **Local file editing** — open and save local files (Ctrl+O / Ctrl+N)
- **Multiple color schemes** with custom color editor
- **Dark/light theme** toggle
- **Draggable panes** — rearrange function list, editor, and file tree
- **Tab management** — right-click context menu with close, close all, close all but this

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

## License

MIT
