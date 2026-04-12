"""SynPad local file browser mixin."""

import os
import stat
from pathlib import Path

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib


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
            for name in sorted(os.listdir(path), key=str.lower):
                if name.startswith('.'):
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

    def _on_local_tree_right_click(self, _view, event):
        """Show context menu on right-click in local file tree."""
        if event.button != 3:
            return False

        path_info = self._local_view.get_path_at_pos(int(event.x), int(event.y))

        if path_info:
            tree_path = path_info[0]
            self._local_view.get_selection().select_path(tree_path)
            self._local_view.set_cursor(tree_path, None, False)

        menu = Gtk.Menu()

        if path_info:
            tree_path, _col, _cx, _cy = path_info
            tree_iter = self._local_store.get_iter(tree_path)
            is_dir = self._local_store[tree_iter][3]
            local_path = self._local_store[tree_iter][2]
            name = self._local_store[tree_iter][0]

            if is_dir:
                item = Gtk.MenuItem(label="New File...")
                item.connect('activate', lambda _: self._on_local_new_file(local_path, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label="New Directory...")
                item.connect('activate', lambda _: self._on_local_new_dir(local_path, tree_iter))
                menu.append(item)

                menu.append(Gtk.SeparatorMenuItem())

                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_local_rename(local_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_local_permissions(local_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete Directory '{name}'")
                item.connect('activate', lambda _: self._on_local_delete(local_path, name, tree_iter, is_dir=True))
                menu.append(item)
            else:
                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_local_rename(local_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_local_permissions(local_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete '{name}'")
                item.connect('activate', lambda _: self._on_local_delete(local_path, name, tree_iter, is_dir=False))
                menu.append(item)
        else:
            # Right-clicked on empty space
            root = self._local_path_entry.get_text().strip() or str(Path.home())
            item = Gtk.MenuItem(label="New File...")
            item.connect('activate', lambda _: self._on_local_new_file(root, None))
            menu.append(item)

            item = Gtk.MenuItem(label="New Directory...")
            item.connect('activate', lambda _: self._on_local_new_dir(root, None))
            menu.append(item)

        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _on_local_new_file(self, parent_dir, parent_iter):
        """Create a new empty local file."""
        name = self._ask_name("New File", "File name:")
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

    def _on_local_new_dir(self, parent_dir, parent_iter):
        """Create a new local directory."""
        name = self._ask_name("New Directory", "Directory name:")
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

    def _on_local_rename(self, local_path, old_name, tree_iter):
        """Rename a local file or directory."""
        new_name = self._ask_name("Rename", f"New name for '{old_name}':",
                                          default_value=old_name, ok_label="Rename")
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

    def _on_local_delete(self, local_path, name, tree_iter, is_dir=False):
        """Delete a local file or directory with confirmation."""
        if not self._confirm_delete(local_path):
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
            for page_num, tab in list(self.tabs.items()):
                if tab.is_local and tab.local_path == local_path:
                    self._close_tab(page_num)
                    break
        except Exception as e:
            self._show_error("Delete Failed", str(e))

    def _on_local_permissions(self, local_path, name):
        """Show chmod dialog for a local file or directory."""
        try:
            mode = stat.S_IMODE(os.stat(local_path).st_mode)
        except Exception as e:
            self._show_error("Permission Error", str(e))
            return

        self._show_local_permissions_dialog(local_path, name, mode)

    def _show_local_permissions_dialog(self, local_path, name, mode):
        """Display permissions editing dialog for a local file."""
        dlg = Gtk.Dialog(
            title=f"Permissions — {name}",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(350, -1)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        box.pack_start(Gtk.Label(label=f"<b>{local_path}</b>",
                                 use_markup=True, halign=Gtk.Align.START),
                       False, False, 0)

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

        box.pack_start(grid, False, False, 0)

        octal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        octal_row.pack_start(Gtk.Label(label="Octal:"), False, False, 0)
        octal_entry = Gtk.Entry(text=f"{mode:03o}", width_chars=6)
        octal_row.pack_start(octal_entry, False, False, 0)
        box.pack_start(octal_row, False, False, 0)

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

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(8)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_row.pack_end(btn_cancel, False, False, 0)
        btn_apply = Gtk.Button(label="Apply")
        btn_apply.get_style_context().add_class('suggested-action')
        btn_apply.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_end(btn_apply, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        dlg.show_all()
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            try:
                new_mode = int(octal_entry.get_text().strip(), 8)
                os.chmod(local_path, new_mode)
                self._set_status(f"Permissions set: {name} → {oct(new_mode)}")
            except ValueError:
                self._show_error("Invalid Permissions", "Octal value is not valid.")
            except Exception as e:
                self._show_error("Permission Error", str(e))
        dlg.destroy()
