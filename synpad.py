#!/usr/bin/env python3
"""SynPad - A lightweight PHP IDE with FTP/SFTP integration for Linux."""

import sys
import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib

from config import APP_VERSION


class SynPadApplication(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="com.mbeulens.synpad",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)

        # Suppress all GTK/GLib warning and critical messages from stderr
        import ctypes
        try:
            libc = ctypes.CDLL("libglib-2.0.so.0")
            libc.g_log_set_always_fatal(0)
            LOG_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))
            _noop_handler = LOG_FUNC(lambda *a: None)
            self._log_handler = _noop_handler
            libc.g_log_set_handler(b"Gtk", 0xFF, _noop_handler, None)
            libc.g_log_set_handler(b"GtkSourceView", 0xFF, _noop_handler, None)
        except Exception:
            pass

        import warnings
        warnings.filterwarnings('ignore')

        GLib.set_prgname("synpad")
        GLib.set_application_name("SynPad")
        Gdk.set_program_class("synpad")

    def do_activate(self):
        from window import SynPadWindow
        if self.window is None:
            self.window = SynPadWindow(application=self)
            self.window.show_all()
        GLib.idle_add(self._present_window)

    def do_open(self, files, n_files, hint):
        self.do_activate()
        for gio_file in files:
            path = gio_file.get_path()
            if path and os.path.isfile(path):
                GLib.idle_add(self.window.open_or_focus_file, path)
        GLib.idle_add(self._present_window)

    def _present_window(self):
        # D-Bus-triggered activations have no GDK event in flight, so
        # Gtk.get_current_event_time() returns 0 and the WM applies focus-
        # stealing prevention. Use the X server's current timestamp on X11 so
        # the WM accepts the focus request; fall back to present() elsewhere.
        if self.window is None:
            return False
        timestamp = 0
        gdk_window = self.window.get_window()
        if gdk_window is not None:
            try:
                gi.require_version('GdkX11', '3.0')
                from gi.repository import GdkX11
                if isinstance(gdk_window, GdkX11.X11Window):
                    timestamp = GdkX11.x11_get_server_time(gdk_window)
            except Exception:
                pass
        if timestamp:
            self.window.present_with_time(timestamp)
        else:
            self.window.present()
        return False


def main():
    app = SynPadApplication()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
