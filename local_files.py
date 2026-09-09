"""SynPad local file browser mixin."""

import os
import stat
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, Gio, GLib


class LocalFilesMixin:
    """Mixin for SynPadWindow — local file tree and operations."""

    def _on_toggle_file_view(self, btn, view_name):
        """Toggle between remote and local file trees."""
        if not btn.get_active():
            return
        # Deactivate the other toggle
        if view_name == 'remote':
            self.btn_local_tree.handler_block_by_func(self._on_toggle_file_view)
            self.btn_local_tree.set_active(False)
            self.btn_local_tree.handler_unblock_by_func(self._on_toggle_file_view)
        else:
            self.btn_remote_tree.handler_block_by_func(self._on_toggle_file_view)
            self.btn_remote_tree.set_active(False)
            self.btn_remote_tree.handler_unblock_by_func(self._on_toggle_file_view)

        self._file_stack.set_visible_child_name(view_name)

        # Load local tree on first switch
        if view_name == 'local' and self._local_store.get_iter_first() is None:
            self._load_local_tree(str(Path.home()))

    def _load_local_tree(self, path, parent_iter=None):
        """Load local filesystem directory into the local tree."""
        # Update path bar when loading a root directory
        if parent_iter is None:
            self._local_path_entry.set_text(path)
        try:
            entries = []
            show_hidden = self.config.get('show_hidden_files', False)
            for name in sorted(os.listdir(path), key=str.lower):
                if name.startswith('.') and not show_hidden:
                    continue
                full = os.path.join(path, name)
                is_dir = os.path.isdir(full)
                entries.append((name, is_dir, full))
        except PermissionError:
            return

        if parent_iter:
            # Remove placeholder children
            old = []
            child = self._local_store.iter_children(parent_iter)
            while child:
                old.append(self._local_store.get_path(child))
                child = self._local_store.iter_next(child)
            self._local_store[parent_iter][4] = True
        else:
            old = None
            self._local_store.clear()

        # Sort: dirs first, then files
        entries.sort(key=lambda x: (not x[1], x[0].lower()))

        for name, is_dir, full in entries:
            icon = 'folder' if is_dir else self._icon_for_file(name)
            it = self._local_store.append(parent_iter, [name, icon, full, is_dir, False])
            if is_dir:
                self._local_store.append(it, ['Loading...', 'content-loading-symbolic', '', False, False])

        if old:
            for tp in reversed(old):
                try:
                    oi = self._local_store.get_iter(tp)
                    self._local_store.remove(oi)
                except ValueError:
                    pass

    def _on_local_tree_expanded(self, _view, tree_iter, _path):
        """Load subdirectory on expand."""
        is_dir = self._local_store[tree_iter][3]
        loaded = self._local_store[tree_iter][4]
        if is_dir and not loaded:
            local_path = self._local_store[tree_iter][2]
            self._load_local_tree(local_path, tree_iter)

    def _on_local_tree_activated(self, _view, path, _col):
        """Open file or toggle directory on double-click."""
        tree_iter = self._local_store.get_iter(path)
        is_dir = self._local_store[tree_iter][3]
        if is_dir:
            if self._local_view.row_expanded(path):
                self._local_view.collapse_row(path)
            else:
                self._local_view.expand_row(path, False)
        else:
            filepath = self._local_store[tree_iter][2]
            if self._is_editor_file(filepath):
                self._open_local_file(filepath)
            else:
                self._open_external(filepath)

    def _on_local_refresh(self, _btn):
        """Refresh the local file tree from current root."""
        it = self._local_store.get_iter_first()
        if it:
            # Find the root directory from the first item's parent path
            first_path = self._local_store[it][2]
            root = os.path.dirname(first_path)
        else:
            root = str(Path.home())
        self._load_local_tree(root)

    def _on_local_home(self, _btn):
        """Navigate local tree to home directory."""
        home = str(Path.home())
        self._local_path_entry.set_text(home)
        self._load_local_tree(home)

    def _on_local_up(self, _btn):
        """Navigate to parent directory."""
        current = self._local_path_entry.get_text().strip()
        if not current:
            current = str(Path.home())
        parent = os.path.dirname(current)
        if parent and os.path.isdir(parent):
            self._local_path_entry.set_text(parent)
            self._load_local_tree(parent)

    def _on_local_path_enter(self, entry):
        """Navigate to the path typed in the entry."""
        path = entry.get_text().strip()
        if path and os.path.isdir(path):
            self._load_local_tree(path)
        elif path:
            self._show_error("Invalid Path", f"'{path}' is not a valid directory.")

    # -- Local File Tree Context Menu -------------------------------------------

    def _local_attach_tree_controllers(self, view):
        """Wire the local file tree's right-click context menu.

        GTK4 has no button-press-event; right-click arrives via a
        Gtk.GestureClick restricted to the secondary (right) button."""
        click = Gtk.GestureClick()
        click.set_button(3)                    # secondary only, was event.button != 3
        click.connect('pressed', self._on_local_tree_right_click)
        view.add_controller(click)

    def _local_ensure_ctx_popover(self, view):
        """Lazily create the context-menu popover and (re-)anchor it to
        `view`. GTK4 popovers hold exactly one parent: unparent before
        re-parenting."""
        if getattr(self, '_local_ctx_popover', None) is None:
            self._local_ctx_popover = Gtk.PopoverMenu()
        pop = self._local_ctx_popover
        if pop.get_parent() is not view:
            if pop.get_parent() is not None:
                pop.unparent()
            pop.set_parent(view)
        return pop

    def _on_local_tree_right_click(self, gesture, n_press, x, y):
        """Show context menu on right-click in local file tree."""
        view = gesture.get_widget()
        path_info = view.get_path_at_pos(int(x), int(y))

        if path_info:
            tree_path = path_info[0]
            view.get_selection().select_path(tree_path)
            view.set_cursor(tree_path, None, False)

        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()

        def add_action(section, action_name, label, callback):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect('activate', lambda _a, _p: callback())
            group.add_action(action)
            section.append(label, f'localctx.{action_name}')

        if path_info:
            tree_path, _col, _cx, _cy = path_info
            tree_iter = self._local_store.get_iter(tree_path)
            is_dir = self._local_store[tree_iter][3]
            local_path = self._local_store[tree_iter][2]
            name = self._local_store[tree_iter][0]

            if is_dir:
                sec1 = Gio.Menu()
                add_action(sec1, 'new_file', "New File...",
                           lambda: self._on_local_new_file(local_path, tree_iter))
                add_action(sec1, 'new_dir', "New Directory...",
                           lambda: self._on_local_new_dir(local_path, tree_iter))
                menu.append_section(None, sec1)

                sec2 = Gio.Menu()
                add_action(sec2, 'rename', f"Rename '{name}'...",
                           lambda: self._on_local_rename(local_path, name, tree_iter))
                add_action(sec2, 'permissions', f"Permissions '{name}'...",
                           lambda: self._on_local_permissions(local_path, name))
                add_action(sec2, 'delete', f"Delete Directory '{name}'",
                           lambda: self._on_local_delete(local_path, name, tree_iter, is_dir=True))
                menu.append_section(None, sec2)

                # Repo root: directory contains a `.git` child
                child_dot_git = os.path.join(local_path, '.git')
                if name == '.git' or os.path.isdir(child_dot_git):
                    target = local_path if name == '.git' else child_dot_git
                    sec3 = Gio.Menu()
                    add_action(sec3, 'git_history', "Show git history",
                               lambda: self._git_show_history_local(target))
                    menu.append_section(None, sec3)
            else:
                sec = Gio.Menu()
                add_action(sec, 'rename', f"Rename '{name}'...",
                           lambda: self._on_local_rename(local_path, name, tree_iter))
                add_action(sec, 'permissions', f"Permissions '{name}'...",
                           lambda: self._on_local_permissions(local_path, name))
                add_action(sec, 'delete', f"Delete '{name}'",
                           lambda: self._on_local_delete(local_path, name, tree_iter, is_dir=False))
                menu.append_section(None, sec)
        else:
            # Right-clicked on empty space
            root = self._local_path_entry.get_text().strip() or str(Path.home())
            sec = Gio.Menu()
            add_action(sec, 'new_file', "New File...",
                       lambda: self._on_local_new_file(root, None))
            add_action(sec, 'new_dir', "New Directory...",
                       lambda: self._on_local_new_dir(root, None))
            menu.append_section(None, sec)

        popover = self._local_ensure_ctx_popover(view)
        popover.set_menu_model(menu)
        view.insert_action_group('localctx', group)

        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.popup()
        return True

    def _on_local_new_file(self, parent_dir, parent_iter):
        """Create a new empty local file.

        GTK4: `_ask_name` (owned by remote.py's RemoteMixin, shared onto
        this same `self` via SynPadWindow's mixins) is async — it calls
        back instead of returning a value, since GTK4 has no synchronous
        dialog API at all. See remote.py's `_ask_name` docstring for why
        (a nested GLib.MainLoop was tried and reverted: it hung on
        titlebar-close, which a bare Gtk.Window has no default handler
        for). The body after the old `if not name: return` guard becomes
        this callback's continuation, unchanged."""
        def on_name(name):
            if not name:
                return
            filepath = os.path.join(parent_dir, name)
            try:
                with open(filepath, 'w') as f:
                    pass
                self._set_status(f"Created {filepath}")
                if parent_iter:
                    self._local_store[parent_iter][4] = False
                    self._load_local_tree(parent_dir, parent_iter)
                else:
                    self._on_local_refresh(None)
            except Exception as e:
                self._show_error("Create Failed", str(e))

        self._ask_name("New File", "File name:", on_name)

    def _on_local_new_dir(self, parent_dir, parent_iter):
        """Create a new local directory. See `_on_local_new_file` for why
        `_ask_name` is now callback-based."""
        def on_name(name):
            if not name:
                return
            dirpath = os.path.join(parent_dir, name)
            try:
                os.makedirs(dirpath, exist_ok=True)
                self._set_status(f"Created {dirpath}")
                if parent_iter:
                    self._local_store[parent_iter][4] = False
                    self._load_local_tree(parent_dir, parent_iter)
                else:
                    self._on_local_refresh(None)
            except Exception as e:
                self._show_error("Create Failed", str(e))

        self._ask_name("New Directory", "Directory name:", on_name)

    def _on_local_rename(self, local_path, old_name, tree_iter):
        """Rename a local file or directory. See `_on_local_new_file` for
        why `_ask_name` is now callback-based."""
        def on_name(new_name):
            if not new_name or new_name == old_name:
                return
            parent_dir = os.path.dirname(local_path)
            new_path = os.path.join(parent_dir, new_name)
            try:
                os.rename(local_path, new_path)
                is_dir = self._local_store[tree_iter][3]
                self._local_store[tree_iter][0] = new_name
                self._local_store[tree_iter][2] = new_path
                if not is_dir:
                    self._local_store[tree_iter][1] = self._icon_for_file(new_name)
                # Update any open tab
                for tab in self.tabs.values():
                    if tab.is_local and tab.local_path == local_path:
                        tab.local_path = new_path
                        tab.remote_path = new_path
                        self._update_tab_label(tab, new_name)
                        break
                self._set_status(f"Renamed to {new_path}")
            except Exception as e:
                self._show_error("Rename Failed", str(e))

        self._ask_name("Rename", f"New name for '{old_name}':", on_name,
                       default_value=old_name, ok_label="Rename")

    def _on_local_delete(self, local_path, name, tree_iter, is_dir=False):
        """Delete a local file or directory with confirmation.

        GTK4: `_confirm_delete` (owned by remote.py's RemoteMixin, shared
        onto this same `self`) is likewise now callback-based — see
        remote.py's `_confirm_delete` docstring for why (on this
        GTK/libadwaita stack, Adw.AlertDialog's own Escape/close handling
        never invokes the choose() callback at all when its parent is a
        plain Gtk.Window, exactly what SynPadWindow is; a nested
        GLib.MainLoop waiting on that callback would hang forever)."""
        def on_confirmed(confirmed):
            if not confirmed:
                return
            try:
                if is_dir:
                    import shutil
                    shutil.rmtree(local_path)
                else:
                    os.unlink(local_path)
                self._local_store.remove(tree_iter)
                self._set_status(f"Deleted {local_path}")
                # Close any open tab for this file
                for page, tab in list(self.tabs.items()):
                    if tab.is_local and tab.local_path == local_path:
                        self._close_tab(page)
                        break
            except Exception as e:
                self._show_error("Delete Failed", str(e))

        self._confirm_delete(local_path, on_confirmed)

    def _on_local_permissions(self, local_path, name):
        """Show chmod dialog for a local file or directory."""
        try:
            mode = stat.S_IMODE(os.stat(local_path).st_mode)
        except Exception as e:
            self._show_error("Permission Error", str(e))
            return

        self._show_local_permissions_dialog(local_path, name, mode)

    def _show_local_permissions_dialog(self, local_path, name, mode):
        """Display permissions editing dialog for a local file.

        GTK4: this is a custom-content dialog (grid of checkboxes + an
        octal entry), so it becomes a plain Gtk.Window with explicit
        buttons rather than Adw.AlertDialog. `.run()`'s blocking return
        value becomes a button-click callback; the decision logic itself
        (validate octal, chmod, report) is unchanged."""
        win = Gtk.Window(
            title=f"Permissions — {name}",
            transient_for=self,
            modal=True,
        )
        win.set_default_size(350, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        win.set_child(box)

        box.append(Gtk.Label(label=f"<b>{local_path}</b>",
                             use_markup=True, halign=Gtk.Align.START))

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        grid.set_margin_top(8)

        grid.attach(Gtk.Label(label=""), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Read</b>", use_markup=True), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Write</b>", use_markup=True), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Execute</b>", use_markup=True), 3, 0, 1, 1)

        checks = {}
        labels = [('Owner', 6), ('Group', 3), ('Others', 0)]
        for row_i, (label, shift) in enumerate(labels, start=1):
            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, row_i, 1, 1)
            for col_i, (perm, bit) in enumerate(
                    [('r', 2), ('w', 1), ('x', 0)], start=1):
                chk = Gtk.CheckButton()
                chk.set_active(bool(mode & (1 << (shift + bit))))
                grid.attach(chk, col_i, row_i, 1, 1)
                checks[(label, perm)] = chk

        box.append(grid)

        octal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        octal_row.append(Gtk.Label(label="Octal:"))
        octal_entry = Gtk.Entry(text=f"{mode:03o}", width_chars=6)
        octal_row.append(octal_entry)
        box.append(octal_row)

        def update_octal(*_args):
            val = 0
            for (lbl, perm), chk in checks.items():
                shift = {'Owner': 6, 'Group': 3, 'Others': 0}[lbl]
                bit = {'r': 2, 'w': 1, 'x': 0}[perm]
                if chk.get_active():
                    val |= 1 << (shift + bit)
            octal_entry.set_text(f"{val:03o}")

        for chk in checks.values():
            chk.connect('toggled', update_octal)

        def update_checks(*_args):
            txt = octal_entry.get_text().strip()
            try:
                val = int(txt, 8)
            except ValueError:
                return
            for (lbl, perm), chk in checks.items():
                shift = {'Owner': 6, 'Group': 3, 'Others': 0}[lbl]
                bit = {'r': 2, 'w': 1, 'x': 0}[perm]
                chk.handler_block_by_func(update_octal)
                chk.set_active(bool(val & (1 << (shift + bit))))
                chk.handler_unblock_by_func(update_octal)

        octal_entry.connect('changed', update_checks)

        def on_response(accepted):
            if accepted:
                try:
                    new_mode = int(octal_entry.get_text().strip(), 8)
                    os.chmod(local_path, new_mode)
                    self._set_status(f"Permissions set: {name} → {oct(new_mode)}")
                except ValueError:
                    self._show_error("Invalid Permissions", "Octal value is not valid.")
                except Exception as e:
                    self._show_error("Permission Error", str(e))
            win.close()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(8)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: on_response(False))
        btn_apply = Gtk.Button(label="Apply")
        btn_apply.add_css_class('suggested-action')
        btn_apply.connect('clicked', lambda _b: on_response(True))
        # GTK3's pack_end(cancel) then pack_end(apply) rendered as
        # [Apply, Cancel] left-to-right (pack_end stacks toward the
        # center); append() keeps call order, so append in that same
        # visual order to preserve the layout exactly.
        btn_row.append(btn_apply)
        btn_row.append(btn_cancel)
        box.append(btn_row)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, so no chmod ran); a bare Gtk.Window has no
        # such built-in behavior, so wire it explicitly to the same cancel
        # path the Cancel button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                on_response(False)
                return True
            return False

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        win.present()
