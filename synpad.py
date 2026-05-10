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
            flags=(Gio.ApplicationFlags.HANDLES_COMMAND_LINE
                   | Gio.ApplicationFlags.SEND_ENVIRONMENT),
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

    def do_command_line(self, command_line):
        self.do_activate()

        # GTK3 only forwards the X11 desktop-startup-id to the primary
        # instance via D-Bus platform-data; the Wayland XDG_ACTIVATION_TOKEN
        # is dropped on the floor (gtk_application_before_emit gates it on
        # GDK_IS_X11_DISPLAY). Pull whichever token the launcher set out of
        # the secondary instance's env and apply it manually so present()
        # can spend it via xdg-activation.
        token = self._extract_activation_token(command_line)
        if token and self.window is not None:
            try:
                self.window.set_startup_id(token)
            except Exception:
                pass

        args = command_line.get_arguments() or []
        cwd = command_line.get_cwd() or os.getcwd()
        for arg in args[1:]:
            full_path = arg if os.path.isabs(arg) else os.path.join(cwd, arg)
            if os.path.isfile(full_path):
                GLib.idle_add(self.window.open_or_focus_file, full_path)

        GLib.idle_add(self._present_window)
        return 0

    @staticmethod
    def _extract_activation_token(command_line):
        env_list = command_line.get_environ() or []
        xdg_token = None
        startup_id = None
        for var in env_list:
            s = var if isinstance(var, str) else var.decode('utf-8', 'replace')
            if s.startswith("XDG_ACTIVATION_TOKEN="):
                xdg_token = s.split("=", 1)[1]
            elif s.startswith("DESKTOP_STARTUP_ID="):
                startup_id = s.split("=", 1)[1]
        return xdg_token or startup_id

    def _present_window(self):
        if self.window is None:
            return False
        # Hint via taskbar/dock so the user notices even if the compositor
        # refuses to raise the window — typical on Wayland for CLI launches
        # whose terminal didn't acquire an XDG_ACTIVATION_TOKEN.
        if not self.window.is_active():
            try:
                self.window.set_urgency_hint(True)
            except Exception:
                pass
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
