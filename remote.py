"""SynPad remote file tree and connection mixin."""

import hashlib
import os
import threading
import uuid

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from config import save_config, find_server_by_guid
from connection import FTPManager, SFTPManager, ConnectDialog


class RemoteMixin:
    """Mixin for SynPadWindow — remote file tree and operations."""

    def _rebuild_quick_menu(self):
        """Rebuild the quick-connect menu with grouped submenus."""
        servers = self.config.get('servers', [])
        if not servers:
            self.quick_btn.set_visible(False)
            return
        self.quick_btn.set_visible(True)

        menu = Gtk.Menu()

        # Group servers
        groups = {}  # group_name -> [srv, ...]
        ungrouped = []
        for srv in servers:
            group = srv.get('group', '').strip()
            if group:
                groups.setdefault(group, []).append(srv)
            else:
                ungrouped.append(srv)

        # Add ungrouped servers first
        for srv in ungrouped:
            label = f"{srv['name']} ({srv.get('protocol','sftp').upper()})"
            item = Gtk.MenuItem(label=label)
            guid = srv['guid']
            item.connect('activate', lambda _i, g=guid: self._on_quick_connect(g))
            menu.append(item)

        # Add separator if both ungrouped and grouped exist
        if ungrouped and groups:
            menu.append(Gtk.SeparatorMenuItem())

        # Add grouped servers as submenus
        for group_name in sorted(groups.keys()):
            group_item = Gtk.MenuItem(label=group_name)
            submenu = Gtk.Menu()
            for srv in groups[group_name]:
                label = f"{srv['name']} ({srv.get('protocol','sftp').upper()})"
                item = Gtk.MenuItem(label=label)
                guid = srv['guid']
                item.connect('activate', lambda _i, g=guid: self._on_quick_connect(g))
                submenu.append(item)
            group_item.set_submenu(submenu)
            menu.append(group_item)

        menu.show_all()
        self.quick_btn.set_popup(menu)

    def _on_quick_connect(self, server_guid):
        """Instantly connect to a saved server."""
        # Disconnect first if connected
        if self.ftp_mgr and self.ftp_mgr.connected:
            self._on_disconnect(None)
        srv = find_server_by_guid(self.config, server_guid)
        if srv:
            vals = dict(srv)
            vals['remember'] = True
            vals['server_guid'] = srv['guid']
            vals['server_name'] = srv['name']
            self._do_connect(vals)

    def _on_connect(self, _btn):
        dlg = ConnectDialog(self, self.config)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            vals = dlg.get_values()
            dlg.destroy()
            self._rebuild_quick_menu()
            self._do_connect(vals)
        else:
            dlg.destroy()
            self._rebuild_quick_menu()

    def _do_connect(self, vals):
        protocol = vals.get('protocol', 'sftp')
        self._set_status(f"Connecting via {protocol.upper()} to {vals['host']}...")
        self._console_log(f"{protocol.upper()} CONNECT {vals['username']}@{vals['host']}:{vals['port']}")
        self.btn_connect.set_sensitive(False)

        def work():
            try:
                if protocol == 'sftp':
                    self.ftp_mgr = SFTPManager()
                    self.ftp_mgr.connect(
                        vals['host'], vals['port'],
                        vals['username'], vals['password'],
                        vals.get('ssh_key_path', ''),
                    )
                else:
                    self.ftp_mgr = FTPManager()
                    self.ftp_mgr.connect(
                        vals['host'], vals['port'],
                        vals['username'], vals['password'],
                    )
                GLib.idle_add(self._on_connected, vals)
            except Exception as e:
                GLib.idle_add(self._on_connect_failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_connected(self, vals):
        # Auto-save server profile if connecting without one
        server_guid = vals.get('server_guid', '')
        if not server_guid:
            server_guid = str(uuid.uuid4())
            profile = {
                'guid': server_guid,
                'name': vals.get('host', 'Unknown'),
                'protocol': vals.get('protocol', 'sftp'),
                'host': vals['host'],
                'port': vals['port'],
                'username': vals['username'],
                'password': vals['password'],
                'ssh_key_path': vals.get('ssh_key_path', ''),
                'home_directory': vals.get('home_directory', ''),
                'max_upload_size_mb': vals['max_upload_size_mb'],
            }
            self.config.setdefault('servers', []).append(profile)
            vals['server_guid'] = server_guid
            vals['server_name'] = profile['name']

        self.current_server_guid = server_guid
        self.config['host'] = vals['host']
        self.config['port'] = vals['port']
        self.config['username'] = vals['username']
        self.config['password'] = vals['password']
        self.config['max_upload_size_mb'] = vals['max_upload_size_mb']
        self.config['protocol'] = vals.get('protocol', 'sftp')
        self.config['ssh_key_path'] = vals.get('ssh_key_path', '')
        self.config['home_directory'] = vals.get('home_directory', '')
        self.config['last_server'] = server_guid
        save_config(self.config)

        proto_label = vals.get('protocol', 'sftp').upper()
        server_name = vals.get('server_name', '')
        if server_name:
            self.header.set_subtitle(f"[{server_name}] {proto_label}: {vals['username']}@{vals['host']}")
        else:
            self.header.set_subtitle(f"{proto_label}: {vals['username']}@{vals['host']}")
        self._rebuild_quick_menu()
        self.btn_connect.set_sensitive(False)
        self.btn_disconnect.set_sensitive(True)
        self.item_save.set_sensitive(True)
        self.btn_refresh.set_sensitive(True)
        self._set_status("Connected")
        self._console_log(f"Connected to {vals['host']} — home: {self.ftp_mgr.home_dir}", 'success')
        # Use manual home dir if set, otherwise auto-detected from server
        start_dir = vals.get('home_directory', '').strip()
        if not start_dir:
            start_dir = self.ftp_mgr.home_dir
        self._load_tree(start_dir)

    def _on_connect_failed(self, err):
        self.btn_connect.set_sensitive(True)
        self._set_status("Connection failed")
        self._console_log(f"Connection failed: {err}", 'error')
        self._show_error("Connection Failed", err)

    def _on_disconnect(self, _btn):
        if self.ftp_mgr:
            self._console_log("DISCONNECT")
            self.ftp_mgr.disconnect()
            self.ftp_mgr = None
        self.current_server_guid = ''
        self.tree_store.clear()
        self.header.set_subtitle("Disconnected")
        self.btn_connect.set_sensitive(True)
        self.btn_disconnect.set_sensitive(False)
        self.item_save.set_sensitive(False)
        self.btn_refresh.set_sensitive(False)
        self._set_status("Disconnected")

    def _load_tree_and_expand(self, target_dir, vals):
        """Load the tree starting from home, then expand down to target_dir."""
        start_dir = vals.get('home_directory', '').strip()
        if not start_dir and self.ftp_mgr:
            start_dir = self.ftp_mgr.home_dir
        if not start_dir:
            start_dir = '/'

        # If the target dir is the same as or under home, we need to
        # load home first, then expand each segment
        self._expand_target = target_dir
        self._expand_segments = []

        # Build the list of directories to expand from home to target
        if target_dir.startswith(start_dir):
            relative = target_dir[len(start_dir):].strip('/')
            if relative:
                self._expand_segments = relative.split('/')
        self._expand_home = start_dir

        self._set_status(f"Navigating to {target_dir}...")
        self._load_tree(start_dir)

        # After tree loads, start expanding segments
        if self._expand_segments:
            # We hook into _populate_tree completion via idle_add chain
            GLib.timeout_add(500, self._expand_next_segment)

    def _expand_next_segment(self):
        """Expand the next directory segment in the tree."""
        if not self._expand_segments:
            return False  # stop the chain

        segment = self._expand_segments.pop(0)

        # Find the segment in the tree
        def _find_and_expand(parent_iter=None):
            model = self.tree_store
            if parent_iter:
                child = model.iter_children(parent_iter)
            else:
                child = model.get_iter_first()

            while child:
                name = model[child][0]
                is_dir = model[child][3]
                if is_dir and name == segment:
                    path = model.get_path(child)
                    self.tree_view.expand_row(path, False)
                    # If more segments, wait for the expand to load then continue
                    if self._expand_segments:
                        GLib.timeout_add(500, self._expand_next_segment)
                    return
                child = model.iter_next(child)

        # Search from root or find the deepest expanded node
        _find_and_expand(self._find_expanded_parent())
        return False

    def _find_expanded_parent(self):
        """Find the deepest expanded tree iter matching the path so far."""
        path_so_far = self._expand_home
        model = self.tree_store
        parent = None
        it = model.get_iter_first()

        while it:
            full_path = model[it][2]
            is_dir = model[it][3]
            if is_dir and self._expand_target.startswith(full_path + '/'):
                tree_path = model.get_path(it)
                if self.tree_view.row_expanded(tree_path):
                    parent = it
                    it = model.iter_children(it)
                    continue
            it = model.iter_next(it)

        return parent

    def _load_tree(self, path, parent_iter=None):
        self._set_status(f"Loading {path}...")
        self._console_log(f"LIST {path}")

        def work():
            try:
                entries = self.ftp_mgr.list_dir(path)
                self._console_log(f"LIST {path} — {len(entries)} items")
                GLib.idle_add(self._populate_tree, path, parent_iter, entries)
            except Exception as e:
                self._console_log(f"LIST FAILED {path}: {e}", 'error')
                GLib.idle_add(self._set_status, f"Error listing {path}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _populate_tree(self, path, parent_iter, entries):
        if parent_iter:
            # Collect old placeholder children to remove AFTER adding new ones.
            # If we remove first, GTK sees 0 children and auto-collapses the row.
            old_children = []
            child = self.tree_store.iter_children(parent_iter)
            while child:
                old_children.append(self.tree_store.get_path(child))
                child = self.tree_store.iter_next(child)
            self.tree_store[parent_iter][4] = True  # mark loaded
        else:
            old_children = None
            self.tree_store.clear()

        # Add new entries
        norm = path.rstrip('/') or ''
        for name, is_dir in entries:
            full = f"{norm}/{name}"
            icon = 'folder' if is_dir else self._icon_for_file(name)
            it = self.tree_store.append(parent_iter, [name, icon, full, is_dir, False])
            if is_dir:
                self.tree_store.append(it, ['Loading...', 'content-loading-symbolic', '', False, False])

        # Now remove old placeholders (row is still expanded because it has new children)
        if old_children:
            for tree_path in reversed(old_children):
                try:
                    old_iter = self.tree_store.get_iter(tree_path)
                    self.tree_store.remove(old_iter)
                except ValueError:
                    pass

        self._set_status(f"Loaded {path} ({len(entries)} items)")

    def _icon_for_file(self, name):
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        mapping = {
            'php': 'text-x-script',
            'js': 'text-x-script',
            'ts': 'text-x-script',
            'py': 'text-x-script',
            'html': 'text-html',
            'htm': 'text-html',
            'css': 'text-css',
            'json': 'text-x-generic',
            'xml': 'text-xml',
            'sql': 'text-x-sql',
            'md': 'text-x-generic',
            'txt': 'text-x-generic',
            'sh': 'text-x-script',
            'yml': 'text-x-generic',
            'yaml': 'text-x-generic',
            'ini': 'text-x-generic',
            'conf': 'text-x-generic',
            'env': 'text-x-generic',
        }
        return mapping.get(ext, 'text-x-generic')

    def _on_tree_row_expanded(self, _view, tree_iter, _path):
        is_dir = self.tree_store[tree_iter][3]
        loaded = self.tree_store[tree_iter][4]
        if is_dir and not loaded:
            remote_path = self.tree_store[tree_iter][2]
            self._load_tree(remote_path, tree_iter)

    def _on_tree_row_activated(self, _view, path, _col):
        tree_iter = self.tree_store.get_iter(path)
        is_dir = self.tree_store[tree_iter][3]
        if is_dir:
            if self.tree_view.row_expanded(path):
                self.tree_view.collapse_row(path)
            else:
                self.tree_view.expand_row(path, False)
        else:
            remote_path = self.tree_store[tree_iter][2]
            if self._is_editor_file(remote_path):
                self._open_file(remote_path)
            else:
                self._open_remote_external(remote_path)

    def _open_remote_external(self, remote_path):
        """Download a remote file to temp and open with system default app."""
        self._set_status(f"Downloading {remote_path} for external open...")
        self._console_log(f"GET (external) {remote_path}")
        filename = os.path.basename(remote_path)
        local_path = os.path.join(self.tmp_dir, filename)

        def work():
            try:
                self.ftp_mgr.download(remote_path, local_path)
                GLib.idle_add(self._open_external, local_path)
                GLib.idle_add(self._set_status, f"Opened {filename} externally")
            except Exception as e:
                GLib.idle_add(self._show_error, "Download Failed", str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_right_click(self, _view, event):
        """Show context menu on right-click in file tree."""
        if event.button != 3:
            return False
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            return False

        # Get the clicked row and select it
        path_info = self.tree_view.get_path_at_pos(int(event.x), int(event.y))

        if path_info:
            tree_path = path_info[0]
            self.tree_view.get_selection().select_path(tree_path)
            self.tree_view.set_cursor(tree_path, None, False)

        menu = Gtk.Menu()

        if path_info:
            tree_path, _col, _cx, _cy = path_info
            tree_iter = self.tree_store.get_iter(tree_path)
            is_dir = self.tree_store[tree_iter][3]
            remote_path = self.tree_store[tree_iter][2]
            name = self.tree_store[tree_iter][0]

            if is_dir:
                # Right-clicked on a directory
                item = Gtk.MenuItem(label="New File...")
                item.connect('activate', lambda _: self._on_tree_new_file(remote_path, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label="New Directory...")
                item.connect('activate', lambda _: self._on_tree_new_dir(remote_path, tree_iter))
                menu.append(item)

                menu.append(Gtk.SeparatorMenuItem())

                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_tree_rename(remote_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_tree_permissions(remote_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete Directory '{name}'")
                item.connect('activate', lambda _: self._on_tree_delete_dir(remote_path, tree_iter))
                menu.append(item)
            else:
                # Right-clicked on a file
                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_tree_rename(remote_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_tree_permissions(remote_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete '{name}'")
                item.connect('activate', lambda _: self._on_tree_delete_file(remote_path, tree_iter))
                menu.append(item)
        else:
            # Right-clicked on empty space — use the root/home dir
            start_dir = self.config.get('home_directory', '').strip()
            if not start_dir:
                start_dir = self.ftp_mgr.home_dir

            item = Gtk.MenuItem(label="New File...")
            item.connect('activate', lambda _: self._on_tree_new_file(start_dir, None))
            menu.append(item)

            item = Gtk.MenuItem(label="New Directory...")
            item.connect('activate', lambda _: self._on_tree_new_dir(start_dir, None))
            menu.append(item)

        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _ask_name(self, title, prompt, default_value='', ok_label='Create'):
        """Show a simple dialog asking for a name. Returns name or None."""
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True,
                         use_header_bar=False)
        dlg.set_default_size(300, -1)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        box.pack_start(Gtk.Label(label=prompt, halign=Gtk.Align.START), False, False, 0)

        entry = Gtk.Entry(text=default_value)
        entry.set_activates_default(False)
        entry.connect('activate', lambda _: dlg.response(Gtk.ResponseType.OK))
        if default_value:
            entry.select_region(0, -1)
        box.pack_start(entry, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_row.pack_end(btn_cancel, False, False, 0)
        btn_ok = Gtk.Button(label=ok_label)
        btn_ok.get_style_context().add_class('suggested-action')
        btn_ok.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_end(btn_ok, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        dlg.show_all()
        resp = dlg.run()
        name = entry.get_text().strip()
        dlg.destroy()

        if resp == Gtk.ResponseType.OK and name:
            return name
        return None

    def _confirm_delete(self, what):
        """Ask for confirmation before deleting. Returns True if confirmed."""
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Confirm Delete",
        )
        dlg.format_secondary_text(f"Are you sure you want to delete:\n\n{what}\n\nThis cannot be undone.")
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    def _on_tree_new_file(self, parent_dir, parent_iter):
        """Create a new empty file in the given directory."""
        name = self._ask_name("New File", "File name:")
        if not name:
            return
        remote_path = f"{parent_dir.rstrip('/')}/{name}"
        self._set_status(f"Creating {remote_path}...")

        def work():
            try:
                self.ftp_mgr.mkfile(remote_path)
                GLib.idle_add(self._on_tree_file_created, parent_dir, parent_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Create Failed", str(e))
                GLib.idle_add(self._set_status, "Create failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_file_created(self, parent_dir, parent_iter, remote_path):
        self._set_status(f"Created {remote_path}")
        self._console_log(f"MKDIR/MKFILE {remote_path}", 'success')
        # Refresh the parent directory
        if parent_iter:
            self.tree_store[parent_iter][4] = False  # mark as not loaded
            self._load_tree(parent_dir, parent_iter)
        else:
            self._on_refresh(None)

    def _on_tree_new_dir(self, parent_dir, parent_iter):
        """Create a new directory in the given directory."""
        name = self._ask_name("New Directory", "Directory name:")
        if not name:
            return
        remote_path = f"{parent_dir.rstrip('/')}/{name}"
        self._set_status(f"Creating directory {remote_path}...")

        def work():
            try:
                self.ftp_mgr.mkdir(remote_path)
                GLib.idle_add(self._on_tree_file_created, parent_dir, parent_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Create Failed", str(e))
                GLib.idle_add(self._set_status, "Create failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_permissions(self, remote_path, name):
        """Show chmod/chown dialog for a file or directory."""
        self._set_status(f"Reading permissions for {name}...")

        def work():
            try:
                mode, owner, group = self.ftp_mgr.get_stat(remote_path)
                GLib.idle_add(self._show_permissions_dialog,
                              remote_path, name, mode, owner, group)
            except Exception as e:
                GLib.idle_add(self._show_error, "Permission Error", str(e))
                GLib.idle_add(self._set_status, "Failed to read permissions")

        threading.Thread(target=work, daemon=True).start()

    def _show_permissions_dialog(self, remote_path, name, mode, owner, group):
        """Display the permissions editing dialog."""
        self._set_status(f"Permissions: {name}")

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

        # --- Permission checkboxes ---
        box.pack_start(Gtk.Label(label=f"<b>{remote_path}</b>",
                                 use_markup=True, halign=Gtk.Align.START),
                       False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        grid.set_margin_top(8)

        # Headers
        grid.attach(Gtk.Label(label=""), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Read</b>", use_markup=True), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Write</b>", use_markup=True), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Execute</b>", use_markup=True), 3, 0, 1, 1)

        # Build checkboxes for owner/group/others
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

        # --- Octal display ---
        octal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        octal_row.pack_start(Gtk.Label(label="Octal:"), False, False, 0)
        octal_entry = Gtk.Entry(text=f"{mode:03o}", width_chars=6)
        octal_row.pack_start(octal_entry, False, False, 0)
        box.pack_start(octal_row, False, False, 0)

        # Sync checkboxes → octal entry
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

        # Sync octal entry → checkboxes
        def update_checks(*_args):
            txt = octal_entry.get_text().strip()
            try:
                val = int(txt, 8)
            except ValueError:
                return
            for (lbl, perm), chk in checks.items():
                shift = {'Owner': 6, 'Group': 3, 'Others': 0}[lbl]
                bit = {'r': 2, 'w': 1, 'x': 0}[perm]
                # Block signal to avoid loop
                chk.handler_block_by_func(update_octal)
                chk.set_active(bool(val & (1 << (shift + bit))))
                chk.handler_unblock_by_func(update_octal)

        octal_entry.connect('changed', update_checks)

        # --- Buttons ---
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
            except ValueError:
                self._show_error("Invalid Permissions",
                                 "Octal value is not valid.")
                dlg.destroy()
                return

            self._apply_permissions(remote_path, name, new_mode)
        dlg.destroy()

    def _apply_permissions(self, remote_path, name, new_mode):
        """Apply chmod in a background thread."""
        self._set_status(f"Applying permissions to {name}...")

        def work():
            try:
                self.ftp_mgr.chmod(remote_path, new_mode)
                GLib.idle_add(self._set_status,
                              f"Permissions set: {name} → {oct(new_mode)}")
                self._console_log(f"CHMOD {oct(new_mode)} {remote_path}", 'success')
            except Exception as e:
                GLib.idle_add(self._show_error, "Permission Error", str(e))
                GLib.idle_add(self._set_status, "Permission update failed")
                self._console_log(f"CHMOD FAILED {remote_path}: {e}", 'error')

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_rename(self, remote_path, old_name, tree_iter):
        """Rename a file or directory on the server."""
        new_name = self._ask_name("Rename", f"New name for '{old_name}':",
                                          default_value=old_name, ok_label="Rename")
        if not new_name or new_name == old_name:
            return
        parent_dir = os.path.dirname(remote_path)
        new_path = f"{parent_dir.rstrip('/')}/{new_name}"
        self._set_status(f"Renaming {old_name} to {new_name}...")

        def work():
            try:
                self.ftp_mgr.rename(remote_path, new_path)
                GLib.idle_add(self._on_tree_renamed, tree_iter,
                              remote_path, new_path, new_name)
            except Exception as e:
                GLib.idle_add(self._show_error, "Rename Failed", str(e))
                GLib.idle_add(self._set_status, "Rename failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_renamed(self, tree_iter, old_path, new_path, new_name):
        """Update the tree and any open tabs after a rename."""
        # Update tree store
        is_dir = self.tree_store[tree_iter][3]
        self.tree_store[tree_iter][0] = new_name
        self.tree_store[tree_iter][2] = new_path
        if not is_dir:
            self.tree_store[tree_iter][1] = self._icon_for_file(new_name)

        # Update any open tab pointing to the old path
        for tab in self.tabs.values():
            if tab.remote_path == old_path:
                tab.remote_path = new_path
                self._update_tab_label(tab, new_name)
                break

        self._set_status(f"Renamed to {new_path}")
        self._console_log(f"RENAME {old_path} → {new_path}", 'success')

    def _on_tree_delete_file(self, remote_path, tree_iter):
        """Delete a file from the server."""
        if not self._confirm_delete(remote_path):
            return
        self._set_status(f"Deleting {remote_path}...")

        def work():
            try:
                self.ftp_mgr.rmfile(remote_path)
                GLib.idle_add(self._on_tree_item_deleted, tree_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Delete Failed", str(e))
                GLib.idle_add(self._set_status, "Delete failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_delete_dir(self, remote_path, tree_iter):
        """Delete a directory from the server."""
        if not self._confirm_delete(remote_path):
            return
        self._set_status(f"Deleting directory {remote_path}...")

        def work():
            try:
                self.ftp_mgr.rmdir(remote_path)
                GLib.idle_add(self._on_tree_item_deleted, tree_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Delete Failed",
                              f"{str(e)}\n\nNote: directory must be empty to delete.")
                GLib.idle_add(self._set_status, "Delete failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_item_deleted(self, tree_iter, remote_path):
        """Remove the deleted item from the tree."""
        self.tree_store.remove(tree_iter)
        self._set_status(f"Deleted {remote_path}")
        self._console_log(f"DELETE {remote_path}", 'success')

        # Close any open tab for this file
        for page_num, tab in list(self.tabs.items()):
            if tab.remote_path == remote_path:
                self._close_tab(page_num)
                break

    def _open_file(self, remote_path):
        # Check if already open
        for page_num, tab in self.tabs.items():
            if tab.remote_path == remote_path:
                self.notebook.set_current_page(page_num)
                return

        self._set_status(f"Downloading {remote_path}...")
        self._console_log(f"GET {remote_path}")
        filename = os.path.basename(remote_path)
        local_path = os.path.join(self.tmp_dir, filename.replace('/', '_') + f'_{id(remote_path)}')
        srv_guid = self.current_server_guid

        def work():
            try:
                # Capture remote file stats before download
                r_mtime = self.ftp_mgr.get_remote_mtime(remote_path)
                r_size = self.ftp_mgr.get_remote_size(remote_path)
                self.ftp_mgr.download(remote_path, local_path)
                with open(local_path, 'rb') as f:
                    r_hash = hashlib.sha256(f.read()).hexdigest()
                with open(local_path, 'r', errors='replace') as f:
                    content = f.read()

                def _create_and_set_stats():
                    self._create_editor_tab(remote_path, local_path,
                                            content, False, srv_guid)
                    # Set remote stats on the newly created tab
                    page_num = self.notebook.get_current_page()
                    tab = self.tabs.get(page_num)
                    if tab:
                        tab.remote_mtime = r_mtime
                        tab.remote_size = r_size
                        tab.remote_hash = r_hash
                        self._debug(f"Stored remote stats: mtime={r_mtime}, size={r_size}, hash={r_hash[:12]}...")

                GLib.idle_add(_create_and_set_stats)
            except Exception as e:
                GLib.idle_add(self._show_error, "Download Failed", str(e))
                GLib.idle_add(self._set_status, "Download failed")

        threading.Thread(target=work, daemon=True).start()
