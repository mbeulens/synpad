"""SynPad remote file tree and connection mixin.

GTK4 notes:

- `_on_connect` reuses `connection.ConnectDialog.choose()` exactly per the
  Task 2 async dialog API (see `.superpowers/sdd/gtk4-migration/
  task-2-report.md`, "Async dialog API for downstream tasks" —
  `ConnectDialog` is driven from two call sites, this module's `_on_connect`
  being the second) — no second dialog pattern invented here.
- `_ask_name` and `_confirm_delete` are called not only by this module's
  own tree handlers but also by `local_files.py` (`_on_local_new_file`,
  `_on_local_new_dir`, `_on_local_rename`, `_on_local_delete`). Both were
  originally kept *synchronous* via a private `GLib.MainLoop` pumped until
  the dialog resolved, mirroring `Gtk.Dialog.run()`'s old blocking
  contract — that was reviewed and rejected (see the "Fix round 1" section
  appended to `task-3-report.md`): a plain `Gtk.Window` has no
  `close-request` handler by default, so closing the window via its own
  titlebar, Alt-F4, or a destroyed transient parent never called
  `finish()`, and the app froze with the mainloop never returning; on this
  GTK/libadwaita stack `Adw.AlertDialog`'s own Escape/close handling
  additionally never invokes the `choose()` callback at all when its
  parent is a plain `Gtk.Window` (verified independently of any SynPad
  code) — an unconditional freeze under the nested loop, since
  `loop.quit()` would then never run. Both methods are now plain
  async/callback-based instead (`callback(name_or_None)` /
  `callback(confirmed_bool)`), matching `_show_permissions_dialog`'s and
  `_on_compare_tabs`'s shape — every caller in both this module and
  `local_files.py` was updated to the callback form (Global Constraint 5
  was explicitly lifted for `local_files.py`'s four call sites by the
  controller for this fix).
- The tree right-click context menu and the quick-connect menu convert
  `Gtk.Menu`/`Gtk.MenuItem` to `Gio.Menu` + actions, following
  `local_files.py`'s established pattern; the quick-connect menu attaches
  directly to `self.quick_btn` (a `Gtk.MenuButton`) via
  `set_menu_model()`, which manages its own popover — no manual
  `Gtk.PopoverMenu` needed there, unlike the tree's arbitrary-position
  right-click menu.
- `_show_permissions_dialog` has no external caller relying on a return
  value (it's invoked via `GLib.idle_add`), so it converts straight to the
  async button-callback pattern (Shape B), matching `local_files.py`'s
  `_show_local_permissions_dialog`.
"""

import hashlib
import os
import threading
import uuid

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Gio, GLib, Adw

from config import save_config, find_server_by_guid
from connection import FTPManager, SFTPManager, ConnectDialog
import secrets_store


class RemoteMixin:
    """Mixin for SynPadWindow — remote file tree and operations."""

    def _rebuild_quick_menu(self):
        """Rebuild the quick-connect menu with grouped submenus.

        GTK4: `Gtk.MenuButton.set_popup(Gtk.Menu)` doesn't exist anymore —
        `Gtk.MenuButton.set_menu_model(Gio.Menu)` replaces it and manages
        its own popover internally, so (unlike the tree context menu) no
        manual Gtk.PopoverMenu is needed here."""
        servers = self.config.get('servers', [])
        if not servers:
            self.quick_btn.set_visible(False)
            return
        self.quick_btn.set_visible(True)

        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()

        def add_action(section, action_name, label, guid):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect('activate', lambda _a, _p, g=guid: self._on_quick_connect(g))
            group.add_action(action)
            section.append(label, f'quickconn.{action_name}')

        # Group servers
        groups = {}  # group_name -> [srv, ...]
        ungrouped = []
        for srv in servers:
            group_name = srv.get('group', '').strip()
            if group_name:
                groups.setdefault(group_name, []).append(srv)
            else:
                ungrouped.append(srv)

        # Add ungrouped servers first, as their own section (GTK draws a
        # separator between sections automatically, matching the old
        # "separator only if both ungrouped and grouped exist" behavior:
        # an empty/absent section renders nothing).
        if ungrouped:
            sec_ungrouped = Gio.Menu()
            for i, srv in enumerate(ungrouped):
                label = f"{srv['name']} ({srv.get('protocol','sftp').upper()})"
                add_action(sec_ungrouped, f'connect_u{i}', label, srv['guid'])
            menu.append_section(None, sec_ungrouped)

        # Add grouped servers as submenus, one shared section holding all
        # of them (matches the old flat sequence of submenu MenuItems).
        if groups:
            sec_groups = Gio.Menu()
            # Index the group by its position in the sorted list rather
            # than sanitising its name into the action name: two
            # differently-named groups (e.g. "A B" and "A-B") sanitise to
            # the same string, which would make add_action() silently
            # overwrite one group's actions with the other's and connect
            # a menu entry to the wrong server's guid.
            for group_idx, group_name in enumerate(sorted(groups.keys())):
                submenu = Gio.Menu()
                for i, srv in enumerate(groups[group_name]):
                    label = f"{srv['name']} ({srv.get('protocol','sftp').upper()})"
                    add_action(submenu, f'connect_g{group_idx}_{i}', label, srv['guid'])
                sec_groups.append_submenu(group_name, submenu)
            menu.append_section(None, sec_groups)

        self.quick_btn.set_menu_model(menu)
        self.quick_btn.insert_action_group('quickconn', group)

    def _on_quick_connect(self, server_guid):
        """Instantly connect to a saved server."""
        # Disconnect first if connected
        if self.ftp_mgr and self.ftp_mgr.connected:
            self._on_disconnect(None)
        srv = find_server_by_guid(self.config, server_guid)
        if srv:
            vals = dict(srv)
            stored_pwd = secrets_store.get_password(srv['guid'])
            if stored_pwd is not None:
                vals['password'] = stored_pwd
            vals['remember'] = True
            vals['server_guid'] = srv['guid']
            vals['server_name'] = srv['name']
            self._do_connect(vals)

    def _on_connect(self, _btn):
        """GTK4: ConnectDialog.choose() replaces the old blocking
        `dlg.run()` — see connection.py and Task 2's worked example.
        Decision logic (save nothing on cancel, connect on ok, always
        rebuild the quick-connect menu) is unchanged."""
        dlg = ConnectDialog(self, self.config)
        dlg.choose(self._on_connect_dialog_response)

    def _on_connect_dialog_response(self, dlg, response):
        if response == 'ok':
            vals = dlg.get_values()
            self._rebuild_quick_menu()
            self._do_connect(vals)
        else:
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
            pwd = vals.get('password', '')
            stored = secrets_store.set_password(server_guid, pwd) if pwd else False
            profile = {
                'guid': server_guid,
                'name': vals.get('host', 'Unknown'),
                'protocol': vals.get('protocol', 'sftp'),
                'host': vals['host'],
                'port': vals['port'],
                'username': vals['username'],
                'password': '' if stored else pwd,
                'ssh_key_path': vals.get('ssh_key_path', ''),
                'home_directory': vals.get('home_directory', ''),
                'max_upload_size_mb': vals['max_upload_size_mb'],
            }
            self.config.setdefault('servers', []).append(profile)
            vals['server_guid'] = server_guid
            vals['server_name'] = profile['name']
        else:
            # Existing profile: refresh stored password in case user changed it
            pwd = vals.get('password', '')
            if pwd:
                secrets_store.set_password(server_guid, pwd)

        self.current_server_guid = server_guid
        self.config['host'] = vals['host']
        self.config['port'] = vals['port']
        self.config['username'] = vals['username']
        # Top-level 'password' is no longer persisted; secret store keyed by
        # last_server holds it. Clear any leftover plaintext if it survived
        # the migration (e.g. keyring was unavailable on previous launch).
        self.config['password'] = ''
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

    def _remote_attach_tree_controllers(self, view):
        """Wire the remote file tree's right-click context menu.

        GTK4 has no button-press-event; right-click arrives via a
        Gtk.GestureClick restricted to the secondary (right) button — same
        pattern as local_files.py's _local_attach_tree_controllers."""
        click = Gtk.GestureClick()
        click.set_button(3)                    # secondary only, was event.button != 3
        click.connect('pressed', self._on_tree_right_click)
        view.add_controller(click)

    def _remote_ensure_ctx_popover(self, view):
        """Lazily create the context-menu popover and (re-)anchor it to
        `view`. GTK4 popovers hold exactly one parent: unparent before
        re-parenting."""
        if getattr(self, '_remote_ctx_popover', None) is None:
            self._remote_ctx_popover = Gtk.PopoverMenu()
        pop = self._remote_ctx_popover
        if pop.get_parent() is not view:
            if pop.get_parent() is not None:
                pop.unparent()
            pop.set_parent(view)
        return pop

    def _on_tree_right_click(self, gesture, n_press, x, y):
        """Show context menu on right-click in file tree.

        GTK4: Gtk.Menu/Gtk.MenuItem -> Gio.Menu model + Gtk.PopoverMenu,
        actions via Gio.SimpleAction, following local_files.py's
        established pattern for the equivalent local-tree menu. Every
        item, label, and connect target is unchanged."""
        view = gesture.get_widget()
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            return False

        # Get the clicked row and select it
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
            section.append(label, f'remotectx.{action_name}')

        if path_info:
            tree_path, _col, _cx, _cy = path_info
            tree_iter = self.tree_store.get_iter(tree_path)
            is_dir = self.tree_store[tree_iter][3]
            remote_path = self.tree_store[tree_iter][2]
            name = self.tree_store[tree_iter][0]

            if is_dir:
                # Right-clicked on a directory
                sec1 = Gio.Menu()
                add_action(sec1, 'new_file', "New File...",
                           lambda: self._on_tree_new_file(remote_path, tree_iter))
                add_action(sec1, 'new_dir', "New Directory...",
                           lambda: self._on_tree_new_dir(remote_path, tree_iter))
                menu.append_section(None, sec1)

                sec2 = Gio.Menu()
                add_action(sec2, 'rename', f"Rename '{name}'...",
                           lambda: self._on_tree_rename(remote_path, name, tree_iter))
                add_action(sec2, 'permissions', f"Permissions '{name}'...",
                           lambda: self._on_tree_permissions(remote_path, name))
                add_action(sec2, 'delete', f"Delete Directory '{name}'",
                           lambda: self._on_tree_delete_dir(remote_path, tree_iter))
                menu.append_section(None, sec2)

                if name == '.git' and isinstance(self.ftp_mgr, SFTPManager):
                    sec3 = Gio.Menu()
                    add_action(sec3, 'git_history', "Show git history",
                               lambda: self._git_show_history_sftp(remote_path))
                    menu.append_section(None, sec3)
            else:
                # Right-clicked on a file
                sec = Gio.Menu()
                add_action(sec, 'rename', f"Rename '{name}'...",
                           lambda: self._on_tree_rename(remote_path, name, tree_iter))
                add_action(sec, 'permissions', f"Permissions '{name}'...",
                           lambda: self._on_tree_permissions(remote_path, name))
                add_action(sec, 'delete', f"Delete '{name}'",
                           lambda: self._on_tree_delete_file(remote_path, tree_iter))
                menu.append_section(None, sec)
        else:
            # Right-clicked on empty space — use the root/home dir
            start_dir = self.config.get('home_directory', '').strip()
            if not start_dir:
                start_dir = self.ftp_mgr.home_dir

            sec = Gio.Menu()
            add_action(sec, 'new_file', "New File...",
                       lambda: self._on_tree_new_file(start_dir, None))
            add_action(sec, 'new_dir', "New Directory...",
                       lambda: self._on_tree_new_dir(start_dir, None))
            menu.append_section(None, sec)

        popover = self._remote_ensure_ctx_popover(view)
        popover.set_menu_model(menu)
        view.insert_action_group('remotectx', group)

        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.popup()
        return True

    def _ask_name(self, title, prompt, callback, default_value='', ok_label='Create'):
        """Show a simple dialog asking for a name.

        Calls `callback(name)` exactly once, with the entered (stripped,
        non-empty) name, or `callback(None)` if cancelled, confirmed with
        an empty/whitespace-only entry, or closed via Escape, the
        titlebar's own close control, Alt-F4, or the transient parent
        being destroyed.

        GTK4: `Gtk.Dialog.run()` no longer exists at all, so this is a
        plain Gtk.Window with explicit buttons (custom content, per the
        migration plan's dialog shape), driven by this callback instead of
        a blocking return value. An earlier version of this method kept
        the old synchronous return-value contract by pumping a private
        GLib.MainLoop — that was reverted after review: a bare Gtk.Window
        has no `close-request` handler by default, so closing it via its
        own titlebar/Alt-F4/a destroyed transient parent never reached
        `finish()`, and `loop.run()` never returned — a guaranteed freeze,
        reachable from every one of this method's nine call sites. This
        version instead resolves via `close-request` explicitly (see
        `on_close_request` below) in addition to Cancel/OK/Escape, and a
        `resolved` guard makes calling the callback more than once for a
        single dialog impossible regardless of which path fires first.
        Decision logic is unchanged: Cancel, Escape, closing the window
        outright, or an empty entry all resolve to None; a non-empty name
        on Create/Rename resolves to that name. Every caller — in this
        module and in local_files.py — was updated to this callback shape
        (Global Constraint 5 lifted for local_files.py's four call sites
        by the controller for this fix)."""
        win = Gtk.Window(title=title, transient_for=self, modal=True)
        win.set_default_size(300, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        win.set_child(box)

        box.append(Gtk.Label(label=prompt, halign=Gtk.Align.START))

        entry = Gtk.Entry(text=default_value)
        entry.set_activates_default(False)
        if default_value:
            entry.select_region(0, -1)
        box.append(entry)

        resolved = [False]

        def resolve(name):
            # Guards against being invoked twice for one dialog — e.g.
            # win.close() below raises 'close-request', whose handler
            # also calls resolve(); without this guard the callback would
            # fire a second time. The window is closed before callback()
            # runs (not after) so a raising callback can't strand this
            # modal=True window open.
            if resolved[0]:
                return
            resolved[0] = True
            win.close()
            callback(name)

        def finish(accepted):
            name = None
            if accepted:
                typed = entry.get_text().strip()
                if typed:
                    name = typed
            resolve(name)

        entry.connect('activate', lambda _e: finish(True))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: finish(False))
        btn_ok = Gtk.Button(label=ok_label)
        btn_ok.add_css_class('suggested-action')
        btn_ok.connect('clicked', lambda _b: finish(True))
        # GTK3's pack_end(cancel) then pack_end(ok) rendered as
        # [ok_label, Cancel] left-to-right (pack_end stacks toward the
        # center) — append in that same visual order.
        btn_row.append(btn_ok)
        btn_row.append(btn_cancel)
        box.append(btn_row)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, treated as not-OK -> None); a bare
        # Gtk.Window has no such built-in behavior, so wire it explicitly
        # to the same cancel path the Cancel button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                finish(False)
                return True
            return False

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        # Closing via the titlebar's own close control, Alt-F4, or the
        # transient parent being destroyed all raise 'close-request'
        # without going through Cancel/OK/Escape — a bare Gtk.Window has
        # no other hook for this. Resolve as cancelled (None) so the
        # caller's callback still fires exactly once, and let the default
        # handling actually tear the window down (return False) rather
        # than calling win.close() ourselves from inside its own
        # close-request handling.
        def on_close_request(_win):
            resolve(None)
            return False

        win.connect('close-request', on_close_request)

        win.present()

    def _confirm_delete(self, what, callback):
        """Ask for confirmation before deleting.

        Calls `callback(True)` if the user confirms ('Yes'), or
        `callback(False)` if they choose 'No'.

        GTK4: a genuine heading/body/Yes-No confirm, so per the migration
        plan's dialog shapes this uses Adw.AlertDialog, driven by this
        callback instead of a blocking return value. An earlier version
        kept the old synchronous return-value contract by pumping a
        private GLib.MainLoop — that was reverted after review: verified
        independently of any SynPad code, with a plain Gtk.Window parent
        (exactly SynPadWindow's own class, unaffected by Task 5),
        Adw.AlertDialog's own Escape/close handling closes the dialog
        without ever emitting its 'closed' signal or invoking the
        choose() callback at all. Under the nested-mainloop version this
        was an unconditional freeze (loop.quit() would never run); now
        that this method is plain async with no blocking wait, a callback
        that simply never fires is inert — no delete happens, which is the
        same safe outcome as an explicit False, just reached by
        non-invocation rather than a call. `choose_finish()` is wrapped in
        a try/except so an exception there can't propagate through
        PyGObject's C-callback boundary and get silently swallowed with no
        `callback` call at all. Decision logic (True only on 'Yes') is
        unchanged. Every caller — in this module and in local_files.py's
        `_on_local_delete` — was updated to this callback shape (Global
        Constraint 5 lifted for local_files.py by the controller for this
        fix)."""
        dlg = Adw.AlertDialog(
            heading="Confirm Delete",
            body=f"Are you sure you want to delete:\n\n{what}\n\nThis cannot be undone.",
        )
        dlg.add_response('no', "No")
        dlg.add_response('yes', "Yes")
        dlg.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response('no')
        dlg.set_close_response('no')

        def on_response(dlg, res):
            try:
                response = dlg.choose_finish(res)
            except Exception:
                return
            callback(response == 'yes')

        dlg.choose(self, None, on_response)

    def _on_tree_new_file(self, parent_dir, parent_iter):
        """Create a new empty file in the given directory."""
        def on_name(name):
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

        self._ask_name("New File", "File name:", on_name)

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
        def on_name(name):
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

        self._ask_name("New Directory", "Directory name:", on_name)

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
        """Display the permissions editing dialog.

        GTK4: this is a custom-content dialog (grid of checkboxes + an
        octal entry), so per the migration plan's dialog shape it becomes
        a plain Gtk.Window with explicit buttons rather than
        Adw.AlertDialog — same structure as local_files.py's
        `_show_local_permissions_dialog`. Nothing reads a return value
        from this method (it's invoked via GLib.idle_add), so it converts
        straight to the async button-callback pattern. `.run()`'s blocking
        return-value branch becomes the `on_response` callback below; the
        decision logic (validate octal, apply, report) is unchanged.
        `on_response` also fires from 'close-request' (titlebar close/
        Alt-F4/destroyed transient parent), guarded so it can't run twice
        for one dialog."""
        self._set_status(f"Permissions: {name}")

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

        # --- Permission checkboxes ---
        box.append(Gtk.Label(label=f"<b>{remote_path}</b>",
                             use_markup=True, halign=Gtk.Align.START))

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

        box.append(grid)

        # --- Octal display ---
        octal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        octal_row.append(Gtk.Label(label="Octal:"))
        octal_entry = Gtk.Entry(text=f"{mode:03o}", width_chars=6)
        octal_row.append(octal_entry)
        box.append(octal_row)

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
        resolved = [False]

        def on_response(accepted):
            # Guards against double-invocation: win.close() below raises
            # 'close-request', whose handler also calls on_response().
            if resolved[0]:
                return
            resolved[0] = True
            if accepted:
                try:
                    new_mode = int(octal_entry.get_text().strip(), 8)
                except ValueError:
                    self._show_error("Invalid Permissions",
                                     "Octal value is not valid.")
                    win.close()
                    return
                self._apply_permissions(remote_path, name, new_mode)
            win.close()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(8)
        btn_row.set_halign(Gtk.Align.END)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: on_response(False))

        btn_apply = Gtk.Button(label="Apply")
        btn_apply.add_css_class('suggested-action')
        btn_apply.connect('clicked', lambda _b: on_response(True))

        # GTK3's pack_end(cancel) then pack_end(apply) rendered as
        # [Apply, Cancel] left-to-right (pack_end stacks toward the
        # center) — append in that same visual order.
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

        # Closing via the titlebar's own close control, Alt-F4, or the
        # transient parent being destroyed all raise 'close-request'
        # without going through Cancel/Apply/Escape — resolve as
        # cancelled (no chmod), and let the default handling actually
        # tear the window down (return False) rather than calling
        # win.close() ourselves from inside its own close-request
        # handling.
        def on_close_request(_win):
            on_response(False)
            return False

        win.connect('close-request', on_close_request)

        win.present()

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
        def on_name(new_name):
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

        self._ask_name("Rename", f"New name for '{old_name}':", on_name,
                       default_value=old_name, ok_label="Rename")

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
        def on_confirmed(confirmed):
            if not confirmed:
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

        self._confirm_delete(remote_path, on_confirmed)

    def _on_tree_delete_dir(self, remote_path, tree_iter):
        """Delete a directory from the server."""
        def on_confirmed(confirmed):
            if not confirmed:
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

        self._confirm_delete(remote_path, on_confirmed)

    def _on_tree_item_deleted(self, tree_iter, remote_path):
        """Remove the deleted item from the tree."""
        self.tree_store.remove(tree_iter)
        self._set_status(f"Deleted {remote_path}")
        self._console_log(f"DELETE {remote_path}", 'success')

        # Close any open tab for this file
        for page, tab in list(self.tabs.items()):
            if tab.remote_path == remote_path:
                self._close_tab(page)
                break

    def _open_file(self, remote_path):
        # Check if already open
        for page, tab in self.tabs.items():
            if tab.remote_path == remote_path:
                self.notebook.set_selected_page(page)
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
                    page = self.notebook.get_selected_page()
                    tab = self.tabs.get(page)
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
