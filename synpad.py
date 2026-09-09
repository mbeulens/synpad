#!/usr/bin/env python3
"""SynPad - A lightweight PHP IDE with FTP/SFTP integration for Linux."""

import os
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gio, GLib, Adw

from config import APP_VERSION


class SynPadApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.mbeulens.synpad",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)

        # Adw.Application.do_startup() already initializes libadwaita (its
        # chain-up leaves Adw.is_initialized() True before this method
        # returns) -- verified empirically. This explicit call is therefore
        # redundant, but Adw.init() is idempotent, so keep it as a defensive
        # belt-and-suspenders: if that ever changes upstream, libadwaita
        # widgets rendering unstyled fails loudly rather than silently.
        Adw.init()

        # Start indexing the completion word lists now, not when the first
        # tab is created. GtkSourceCompletionWords indexes on an idle, and
        # the language tables are large; doing it lazily meant the index was
        # still filling while the user typed, so the popup had no rows,
        # measured zero wide, and GDK refused to map it. Roughly ten seconds
        # of "completion is broken" at every launch.
        try:
            from completion import warm_completion_cache
            warm_completion_cache()
        except Exception:
            pass          # completion is a convenience; never block startup

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

    def do_activate(self):
        from window import SynPadWindow
        if self.window is None:
            self.window = SynPadWindow(application=self)
        GLib.idle_add(self._present_window)

    def do_open(self, files, n_files, hint):
        # do_activate() already queues a _present_window idle. Do NOT queue a
        # second one after the opens: open_or_focus_file may pop a modal
        # ("reload dirty tab?") confirmation, and a trailing present() could
        # otherwise race it onto the main window instead.
        self.do_activate()
        for gio_file in files:
            path = gio_file.get_path()
            if path and os.path.isfile(path):
                GLib.idle_add(self.window.open_or_focus_file, path)

    def _present_window(self):
        if self.window is None:
            return False
        self.window.present()
        return False


def main():
    app = SynPadApplication()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
