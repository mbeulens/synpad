#!/usr/bin/env python3
"""SynPad - A lightweight PHP IDE with FTP/SFTP integration for Linux."""

import sys
import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, Gdk, GLib

from config import APP_VERSION


def main():
    # Suppress all GTK/GLib warning and critical messages from stderr
    import ctypes
    try:
        libc = ctypes.CDLL("libglib-2.0.so.0")
        libc.g_log_set_always_fatal(0)
        # Install a no-op log handler for Gtk and GtkSourceView domains
        LOG_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))
        _noop_handler = LOG_FUNC(lambda *a: None)
        # Keep reference alive
        main._log_handler = _noop_handler
        libc.g_log_set_handler(b"Gtk", 0xFF, _noop_handler, None)
        libc.g_log_set_handler(b"GtkSourceView", 0xFF, _noop_handler, None)
    except Exception:
        pass

    import warnings
    warnings.filterwarnings('ignore')

    GLib.set_prgname("synpad")
    GLib.set_application_name("SynPad")
    Gdk.set_program_class("synpad")

    from window import SynPadWindow
    win = SynPadWindow()
    win.show_all()

    # Open file passed via command line (e.g. "Open with" from file manager)
    if len(sys.argv) > 1:
        filepath = os.path.abspath(sys.argv[1])
        if os.path.isfile(filepath):
            GLib.idle_add(win._open_local_file, filepath)

    Gtk.main()


if __name__ == '__main__':
    main()
