#!/bin/bash
# SynPad Installer - handles externally-managed-environment (PEP 668)

set -e

echo "=== SynPad Installer ==="
echo ""

# Detect package manager
if command -v apt &> /dev/null; then
    PKG="apt"
elif command -v dnf &> /dev/null; then
    PKG="dnf"
elif command -v pacman &> /dev/null; then
    PKG="pacman"
else
    PKG="unknown"
fi

echo "[1/3] Installing system dependencies..."
case $PKG in
    apt)
        sudo apt update
        sudo apt install -y python3 python3-gi python3-venv \
            gir1.2-gtksource-3.0 gir1.2-vte-2.91 \
            python3-paramiko python3-secretstorage
        ;;
    dnf)
        sudo dnf install -y python3 python3-gobject python3-paramiko \
            gtksourceview3 vte291 python3-secretstorage
        ;;
    pacman)
        sudo pacman -S --needed python python-gobject python-paramiko \
            gtksourceview3 vte3 python-secretstorage
        ;;
    *)
        echo "Unknown package manager. Install manually:"
        echo "  python3, python3-gi, gir1.2-gtksource-3.0, gir1.2-vte-2.91,"
        echo "  python3-paramiko, python3-secretstorage"
        exit 1
        ;;
esac

echo ""
echo "[2/3] Verifying installation..."
python3 -c "
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GtkSource
print('  GTK 3 ........... OK')
print('  GtkSourceView .... OK')
"

python3 -c "
import paramiko
print('  paramiko ......... OK (v' + paramiko.__version__ + ')')
" 2>/dev/null || echo "  paramiko ......... MISSING (SFTP won't work, FTP still works)"

echo ""
echo "[3/3] Making synpad.py executable..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$SCRIPT_DIR/synpad.py"

echo ""
echo "=== Installation complete ==="
echo ""
echo "Run SynPad with:"
echo "  python3 $SCRIPT_DIR/synpad.py"
echo ""
echo "Or create a desktop shortcut:"
echo "  $SCRIPT_DIR/create-desktop-entry.sh"
