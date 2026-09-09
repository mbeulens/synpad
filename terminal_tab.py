"""Terminal tabs for the Tools pane — local PTY-backed shells via VTE.

Adds a `+` action widget to the Tools notebook for spawning new terminals,
each running $SHELL in the local tree's current directory. Closing a tab
prompts when a foreground process is still running.

GTK4: Gtk.EventBox is gone (label rename uses a plain Gtk.Box as a swap
slot with a GestureClick attached directly), button/key/focus events
arrive via event controllers, and the busy-terminal confirmation uses
Adw.AlertDialog's async .choose() instead of Gtk.MessageDialog.run()."""

import os
import signal
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, GLib, Adw

try:
    gi.require_version('Vte', '3.91')
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
                    "synpad: VTE 3.91 not available; terminal tabs disabled. "
                    "Install gir1.2-vte-3.91 (apt) or vte3 (dnf/pacman).\n")

    def _terminal_default_cwd(self):
        """Best directory for a fresh terminal:
        1. parent dir of the active editor tab (if it's a local file)
        2. local file tree's current path
        3. $HOME as a last resort"""
        try:
            page = self.notebook.get_selected_page()
            tab = self.tabs.get(page)
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
        btn.set_icon_name('list-add-symbolic')
        btn.add_css_class('flat')
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
        scroll.set_child(term)

        self._terminal_counter += 1
        name = f"Terminal {self._terminal_counter}"
        label_box, label_slot, label = self._terminal_make_tab_label(name, scroll)

        page = self._console_notebook.append_page(scroll, label_box)
        self._console_notebook.set_current_page(page)

        self._terminals[scroll] = {
            'term': term, 'pid': None,
            'label_slot': label_slot, 'label': label,
            'renaming': False,
        }
        term.connect('child-exited',
                     lambda _t, _s, sc=scroll: self._terminal_on_exit(sc))

        # Spawn the shell. Vte's spawn_async does not block. Verified via
        # introspection (Vte.Terminal.spawn_async.__doc__) that the
        # installed Vte 3.91 keeps the same call shape as 2.91 here —
        # (pty_flags, cwd, argv, envv, spawn_flags, child_setup,
        # child_setup_data, timeout, cancellable, callback, user_data) —
        # so no reshaping of this call was needed, only the version bump.
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
        icon = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
        icon.set_pixel_size(16)
        box.append(icon)
        # Label lives in a swap slot so double-click rename can replace it
        # with an entry in place. GTK4 has no Gtk.EventBox; a plain Box
        # acts as the slot and a GestureClick attaches directly to it.
        label_slot = Gtk.Box()
        label = Gtk.Label(label=name)
        label_slot.append(label)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect('pressed', self._terminal_on_label_press, scroll)
        label_slot.add_controller(click)
        box.append(label_slot)
        close_btn = Gtk.Button()
        close_btn.set_icon_name('window-close-symbolic')
        close_btn.add_css_class('flat')
        close_btn.set_focus_on_click(False)
        close_btn.connect('clicked', lambda _: self._terminal_close(scroll))
        box.append(close_btn)
        return box, label_slot, label

    # -- In-place tab rename ------------------------------------------------

    def _terminal_on_label_press(self, _gesture, n_press, _x, _y, scroll):
        if n_press == 2:
            self._terminal_begin_rename(scroll)
            return True
        return False

    def _terminal_begin_rename(self, scroll):
        info = self._terminals.get(scroll)
        if not info or info.get('renaming'):
            return
        info['renaming'] = True
        slot = info['label_slot']
        label = info['label']
        current = label.get_text()
        entry = Gtk.Entry()
        entry.set_text(current)
        entry.set_width_chars(max(8, len(current) + 2))
        entry.set_has_frame(False)
        entry.connect('activate',
                      lambda _e: self._terminal_commit_rename(scroll, entry))
        focus = Gtk.EventControllerFocus()
        focus.connect('leave',
                      lambda _c: self._terminal_commit_rename(scroll, entry))
        entry.add_controller(focus)
        key = Gtk.EventControllerKey()
        key.connect('key-pressed',
                    self._terminal_rename_keypress, scroll, entry)
        entry.add_controller(key)
        slot.remove(label)
        slot.append(entry)
        entry.grab_focus()
        entry.select_region(0, -1)

    def _terminal_rename_keypress(self, _ctrl, keyval, _keycode, _state,
                                   scroll, entry):
        if keyval == Gdk.KEY_Escape:
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
        slot = info['label_slot']
        child = slot.get_first_child()
        if child is not None:
            slot.remove(child)
        slot.append(info['label'])

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
            dlg = Adw.AlertDialog(
                heading="Close terminal?",
                body="A process is still running in this terminal. Close anyway?",
            )
            dlg.add_response('no', "No")
            dlg.add_response('yes', "Yes")
            dlg.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_default_response('no')
            dlg.set_close_response('no')
            dlg.choose(self, None, self._terminal_close_response, scroll)
            return

        self._terminal_finish_close(scroll)

    def _terminal_close_response(self, dlg, result, scroll):
        """Async continuation of the busy-terminal confirm — the decision
        logic (only proceed on Yes) is unchanged from the old .run() check,
        just moved from an inline return into this callback."""
        response = dlg.choose_finish(result)
        if response != 'yes':
            return
        self._terminal_finish_close(scroll)

    def _terminal_finish_close(self, scroll):
        info = self._terminals.get(scroll)
        if info is None:
            return
        pid = info['pid']
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
