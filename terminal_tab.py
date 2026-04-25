"""Terminal tabs for the Tools pane — local PTY-backed shells via VTE.

Adds a `+` action widget to the Tools notebook for spawning new terminals,
each running $SHELL in the local tree's current directory. Closing a tab
prompts when a foreground process is still running."""

import os
import signal
import sys

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

try:
    gi.require_version('Vte', '2.91')
    from gi.repository import Vte
    HAS_VTE = True
except (ImportError, ValueError):
    HAS_VTE = False


_VTE_WARNED = False


class TerminalMixin:
    """Provides terminal-tab spawning in the Tools notebook. Lazy-init."""

    def _terminal_init(self):
        if getattr(self, '_terminal_initialized', False):
            return
        self._terminal_initialized = True
        # widget -> {'term': Vte.Terminal, 'pid': int|None}
        self._terminals = {}
        self._terminal_counter = 0

        if not HAS_VTE:
            global _VTE_WARNED
            if not _VTE_WARNED:
                _VTE_WARNED = True
                sys.stderr.write(
                    "synpad: VTE 2.91 not available; terminal tabs disabled. "
                    "Install gir1.2-vte-2.91 (apt) or vte3 (dnf/pacman).\n")

    def _terminal_default_cwd(self):
        """Best directory for a fresh terminal:
        1. parent dir of the active editor tab (if it's a local file)
        2. local file tree's current path
        3. $HOME as a last resort"""
        try:
            page_num = self.notebook.get_current_page()
            tab = self.tabs.get(page_num)
            if tab and getattr(tab, 'is_local', False) and tab.local_path:
                parent = os.path.dirname(tab.local_path)
                if parent and os.path.isdir(parent):
                    return parent
        except Exception:
            pass
        if hasattr(self, '_local_path_entry'):
            cwd = self._local_path_entry.get_text().strip()
            if cwd and os.path.isdir(cwd):
                return cwd
        return os.path.expanduser('~')

    def _terminal_make_add_button(self):
        """Return a `+` button to spawn new terminals, or None if VTE missing.
        Caller packs this wherever it wants (e.g. the Tools header)."""
        if not HAS_VTE:
            return None
        btn = Gtk.Button()
        btn.set_image(Gtk.Image.new_from_icon_name(
            'list-add-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text("New terminal")
        btn.connect('clicked', lambda _: self._terminal_add_new())
        return btn

    def _terminal_add_new(self):
        if not HAS_VTE:
            return

        cwd = self._terminal_default_cwd()
        shell = os.environ.get('SHELL', '/bin/bash')

        term = Vte.Terminal()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(term)

        self._terminal_counter += 1
        name = f"Terminal {self._terminal_counter}"
        label_box, label_evbox, label = self._terminal_make_tab_label(name, scroll)

        page = self._console_notebook.append_page(scroll, label_box)
        scroll.show_all()
        self._console_notebook.set_current_page(page)

        self._terminals[scroll] = {
            'term': term, 'pid': None,
            'label_evbox': label_evbox, 'label': label,
            'renaming': False,
        }
        term.connect('child-exited',
                     lambda _t, _s, sc=scroll: self._terminal_on_exit(sc))

        # Spawn the shell. Vte's spawn_async does not block.
        term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            cwd,
            [shell],
            [],          # env: inherit
            GLib.SpawnFlags.DEFAULT,
            None, None,  # child setup
            -1,          # timeout
            None,        # cancellable
            self._terminal_on_spawned,
            scroll,
        )

        # Reveal the Tools pane if hidden so the user actually sees their terminal
        if not getattr(self, '_console_visible', True):
            self._on_toggle_console()
        term.grab_focus()

    def _terminal_make_tab_label(self, name, scroll):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(
            'utilities-terminal-symbolic', Gtk.IconSize.MENU)
        box.pack_start(icon, False, False, 0)
        # Label wrapped in EventBox so it can receive double-click events
        label_evbox = Gtk.EventBox()
        label_evbox.set_visible_window(False)
        label = Gtk.Label(label=name)
        label_evbox.add(label)
        label_evbox.connect(
            'button-press-event', self._terminal_on_label_press, scroll)
        box.pack_start(label_evbox, False, False, 0)
        close_btn = Gtk.Button()
        close_btn.set_image(Gtk.Image.new_from_icon_name(
            'window-close-symbolic', Gtk.IconSize.MENU))
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.set_focus_on_click(False)
        close_btn.connect('clicked', lambda _: self._terminal_close(scroll))
        box.pack_end(close_btn, False, False, 0)
        box.show_all()
        return box, label_evbox, label

    # -- In-place tab rename ------------------------------------------------

    def _terminal_on_label_press(self, _evbox, event, scroll):
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self._terminal_begin_rename(scroll)
            return True
        return False

    def _terminal_begin_rename(self, scroll):
        info = self._terminals.get(scroll)
        if not info or info.get('renaming'):
            return
        info['renaming'] = True
        evbox = info['label_evbox']
        label = info['label']
        current = label.get_text()
        entry = Gtk.Entry()
        entry.set_text(current)
        entry.set_width_chars(max(8, len(current) + 2))
        entry.set_has_frame(False)
        entry.connect('activate',
                      lambda _e: self._terminal_commit_rename(scroll, entry))
        entry.connect('focus-out-event',
                      lambda _w, _e: self._terminal_commit_rename(scroll, entry))
        entry.connect('key-press-event',
                      self._terminal_rename_keypress, scroll, entry)
        evbox.remove(label)
        evbox.add(entry)
        evbox.show_all()
        entry.grab_focus()
        entry.select_region(0, -1)

    def _terminal_rename_keypress(self, _w, event, scroll, entry):
        if event.keyval == Gdk.KEY_Escape:
            self._terminal_end_rename(scroll, commit=False)
            return True
        return False

    def _terminal_commit_rename(self, scroll, entry):
        info = self._terminals.get(scroll)
        if not info or not info.get('renaming'):
            return False
        new_text = entry.get_text().strip()
        if new_text:
            info['label'].set_text(new_text)
        self._terminal_end_rename(scroll, commit=True)
        return False

    def _terminal_end_rename(self, scroll, commit=True):
        info = self._terminals.get(scroll)
        if not info or not info.get('renaming'):
            return
        info['renaming'] = False
        evbox = info['label_evbox']
        child = evbox.get_child()
        if child is not None:
            evbox.remove(child)
        evbox.add(info['label'])
        evbox.show_all()

    def _terminal_on_spawned(self, terminal, pid, error, scroll):
        info = self._terminals.get(scroll)
        if info is None:
            return
        if error is not None:
            self._show_error("Terminal spawn failed", str(error))
            page = self._console_notebook.page_num(scroll)
            if page >= 0:
                self._console_notebook.remove_page(page)
            self._terminals.pop(scroll, None)
            return
        info['pid'] = pid

    def _terminal_close(self, scroll):
        info = self._terminals.get(scroll)
        if info is None:
            return
        term = info['term']
        pid = info['pid']

        # Detect a busy terminal: the foreground process group of the PTY is
        # not the shell itself.
        busy = False
        if pid:
            try:
                pty = term.get_pty()
                if pty:
                    fg = os.tcgetpgrp(pty.get_fd())
                    busy = (fg > 0 and fg != pid)
            except Exception:
                busy = False

        if busy:
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Close terminal?",
            )
            dlg.format_secondary_text(
                "A process is still running in this terminal. Close anyway?")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return

        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        page = self._console_notebook.page_num(scroll)
        if page >= 0:
            self._console_notebook.remove_page(page)
        self._terminals.pop(scroll, None)

    def _terminal_on_exit(self, scroll):
        # Shell exited on its own (user typed `exit`, or we SIGTERMed it).
        page = self._console_notebook.page_num(scroll)
        if page >= 0:
            self._console_notebook.remove_page(page)
        self._terminals.pop(scroll, None)
