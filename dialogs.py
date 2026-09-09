"""SynPad dialogs mixin — settings, color schemes, file types.

GTK4: every dialog here builds real body content (a server-manager form via
ConnectDialog, an extension list, a style-scheme list, a Dark/Light
color-picker notebook), so per the migration plan's dialog gotcha none of
them are Adw.AlertDialog candidates — that widget only fits a plain
heading/body/response-button confirm. Each becomes a Gtk.Window with
explicit buttons instead. Gtk.Dialog's built-in Escape -> close is re-added
explicitly on every one of them, via an EventControllerKey routed to the
same cancel path the Cancel button takes. `.run()`'s blocking return value
becomes a button-click (or ConnectDialog.choose()) callback — the decision
logic that used to run right after `.run()` returned is unchanged, only the
control flow moves into that callback."""

import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GtkSource, Gdk, GLib, Adw

from config import save_config, CONFIG_DIR
from connection import ConnectDialog


class DialogsMixin:
    """Mixin for SynPadWindow — settings, color schemes, file types dialogs."""

    STYLE_ITEMS = [
        ('def:comment',         'Comments'),
        ('def:string',          'Strings'),
        ('def:keyword',         'Keywords'),
        ('def:type',            'Types'),
        ('def:identifier',      'Identifiers / Functions'),
        ('def:statement',       'Statements'),
        ('def:preprocessor',    'Preprocessor'),
        ('def:constant',        'Constants'),
        ('def:special-char',    'Special Characters'),
        ('def:floating-point',  'Numbers'),
        ('def:error',           'Errors'),
        ('def:warning',         'Warnings'),
        ('text',                'Editor Text'),
        ('current-line',        'Current Line'),
        ('line-numbers',        'Line Numbers'),
    ]

    def _on_open_settings(self, _item):
        """Open the server manager dialog."""
        dlg = ConnectDialog(self, self.config, start_new=True)
        dlg.choose(self._on_open_settings_response)

    def _on_open_settings_response(self, dlg, response):
        """Async continuation of the settings dialog (see ConnectDialog.choose()
        in connection.py). The decision logic — save fields only when
        Remember is checked, always rebuild the quick-connect menu — is
        unchanged from the old `resp == Gtk.ResponseType.OK` check."""
        if response == 'ok':
            vals = dlg.get_values()
            if vals.get('remember'):
                self.config['host'] = vals['host']
                self.config['port'] = vals['port']
                self.config['username'] = vals['username']
                self.config['password'] = vals['password']
                self.config['max_upload_size_mb'] = vals['max_upload_size_mb']
                self.config['protocol'] = vals.get('protocol', 'sftp')
                self.config['ssh_key_path'] = vals.get('ssh_key_path', '')
                self.config['home_directory'] = vals.get('home_directory', '')
                self.config['last_server'] = vals.get('server_guid', '')
                save_config(self.config)
        # Always rebuild quick connect — renames/saves/deletes may have happened
        self._rebuild_quick_menu()

    def _on_edit_file_types(self, _item):
        """Dialog to manage which file extensions open in the editor."""
        win = Gtk.Window(
            title="File Types — Editor Extensions",
            transient_for=self,
            modal=True,
        )
        win.set_default_size(400, 450)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        win.set_child(box)

        box.append(Gtk.Label(
            label="File extensions that open in the editor.\n"
                  "All other files open with the system default app.",
            halign=Gtk.Align.START, wrap=True))

        # Extensions list
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        ext_store = Gtk.ListStore(str)
        for ext in sorted(self.config.get('editor_extensions', [])):
            ext_store.append([ext])

        ext_view = Gtk.TreeView(model=ext_store)
        ext_view.set_headers_visible(False)
        col = Gtk.TreeViewColumn("Extension")
        cell = Gtk.CellRendererText()
        col.pack_start(cell, True)
        col.add_attribute(cell, 'text', 0)
        ext_view.append_column(col)

        scroll.set_child(ext_view)
        box.append(scroll)

        # Add/Remove row
        edit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ext_entry = Gtk.Entry(placeholder_text="e.g. jsx")
        ext_entry.set_hexpand(True)
        edit_row.append(ext_entry)

        btn_add = Gtk.Button(label="Add")
        def _on_add(_btn):
            ext = ext_entry.get_text().strip().lower().lstrip('.')
            if ext:
                # Check not already in list
                for row in ext_store:
                    if row[0] == ext:
                        return
                ext_store.append([ext])
                ext_entry.set_text('')
        btn_add.connect('clicked', _on_add)
        ext_entry.connect('activate', _on_add)
        edit_row.append(btn_add)

        btn_remove = Gtk.Button(label="Remove")
        def _on_remove(_btn):
            sel = ext_view.get_selection()
            model, it = sel.get_selected()
            if it:
                model.remove(it)
        btn_remove.connect('clicked', _on_remove)
        edit_row.append(btn_remove)

        box.append(edit_row)

        # Buttons
        def on_response(accepted):
            """.run()'s blocking return value becomes this button-click
            callback; the decision logic (save on Apply, do nothing on
            Cancel/Escape) is unchanged."""
            if accepted:
                new_exts = []
                for row in ext_store:
                    new_exts.append(row[0])
                self.config['editor_extensions'] = sorted(new_exts)
                save_config(self.config)
            win.close()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_apply = Gtk.Button(label="Apply")
        btn_apply.add_css_class('suggested-action')
        btn_apply.connect('clicked', lambda _b: on_response(True))
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: on_response(False))
        # GTK3's pack_end(cancel) then pack_end(apply) rendered as
        # [Apply, Cancel] left-to-right (pack_end stacks toward the
        # center); append() keeps call order, so append in that same
        # visual order to preserve the layout exactly.
        btn_row.append(btn_apply)
        btn_row.append(btn_cancel)
        box.append(btn_row)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, so nothing was saved); a bare Gtk.Window
        # has no such built-in behavior, so wire it explicitly to the same
        # cancel path the Cancel button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                on_response(False)
                return True
            return False

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        win.present()

    def _on_toggle_theme(self, _btn):
        dark = not self.config.get('dark_theme', True)
        self.config['dark_theme'] = dark
        # Switch to a matching base scheme
        if dark:
            self.config['color_scheme'] = 'oblivion'
        else:
            self.config['color_scheme'] = 'classic'
        save_config(self.config)
        self._update_theme_icon()
        self._apply_gtk_theme()
        self._apply_scheme_to_all()

    def _apply_scheme_to_all(self):
        """Apply the current color scheme to all open editor tabs."""
        scheme = self._get_scheme()
        if scheme:
            for tab in self.tabs.values():
                tab.buffer.set_style_scheme(scheme)

    def _apply_gtk_theme(self):
        """Set the application-wide dark/light preference.

        GTK4/libadwaita: GtkSettings:gtk-application-prefer-dark-theme is
        explicitly ignored by libadwaita — verified empirically: setting it
        True or False either way leaves Adw.StyleManager.get_dark() stuck
        at False, while Adw.StyleManager.set_color_scheme() actually flips
        it (and this is also why every launch logged an Adwaita-WARNING).
        The dark/light decision logic itself (self.config['dark_theme'])
        is unchanged; only the API used to apply it changes."""
        style_manager = Adw.StyleManager.get_default()
        if self.config.get('dark_theme', True):
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

    def _on_pick_scheme(self, _item):
        """Dialog to pick a GtkSourceView color scheme."""
        win = Gtk.Window(
            title="Color Scheme",
            transient_for=self,
            modal=True,
        )
        win.set_default_size(350, 400)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        win.set_child(box)

        box.append(Gtk.Label(label="Select a color scheme:", halign=Gtk.Align.START))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        # ListStore: scheme_id, display_name, description
        store = Gtk.ListStore(str, str, str)
        mgr = GtkSource.StyleSchemeManager.get_default()
        current = self.config.get('color_scheme', 'oblivion')

        select_iter = None
        for sid in sorted(mgr.get_scheme_ids()):
            s = mgr.get_scheme(sid)
            it = store.append([sid, s.get_name(), s.get_description() or ''])
            if sid == current:
                select_iter = it

        tv = Gtk.TreeView(model=store)
        tv.set_headers_visible(False)
        col = Gtk.TreeViewColumn("Scheme")
        cell = Gtk.CellRendererText()
        col.pack_start(cell, True)
        col.add_attribute(cell, 'text', 1)
        tv.append_column(col)

        if select_iter:
            tv.get_selection().select_iter(select_iter)

        # Live preview on selection change
        def on_sel_changed(sel):
            model, it = sel.get_selected()
            if it:
                sid = model[it][0]
                scheme = mgr.get_scheme(sid)
                if scheme:
                    for tab in self.tabs.values():
                        tab.buffer.set_style_scheme(scheme)

        tv.get_selection().connect('changed', on_sel_changed)

        scroll.set_child(tv)
        box.append(scroll)

        resolved = [False]

        def on_response(accepted):
            """.run()'s blocking return value becomes this button-click
            callback; the decision logic (apply on OK, revert the live
            preview otherwise — Cancel, Escape, or closing the window via
            its titlebar/Alt-F4) is unchanged. Under GTK3, dlg.run()
            returned RESPONSE_DELETE_EVENT for a titlebar close and the
            revert branch ran; a bare Gtk.Window has no equivalent
            built-in behavior, so close-request is wired below to this
            same path. The `resolved` guard makes this safe to call twice
            for one dialog — e.g. win.close() below raises 'close-request',
            whose handler also calls on_response()."""
            if resolved[0]:
                return
            resolved[0] = True
            if accepted:
                model, it = tv.get_selection().get_selected()
                if it:
                    self.config['color_scheme'] = model[it][0]
                    self.config['custom_colors'] = {}  # reset custom when changing base
                    save_config(self.config)
                    self._apply_scheme_to_all()
            else:
                # Revert preview
                self._apply_scheme_to_all()
            win.close()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(8)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: on_response(False))
        btn_ok = Gtk.Button(label="OK")
        btn_ok.add_css_class('suggested-action')
        btn_ok.connect('clicked', lambda _b: on_response(True))
        # add_buttons(CANCEL, OK) preserves call order visually
        # (Cancel, then OK) — append in that same order.
        btn_row.append(btn_cancel)
        btn_row.append(btn_ok)
        box.append(btn_row)

        win.set_default_widget(btn_ok)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, treated as not-OK, so the preview was
        # reverted); a bare Gtk.Window has no such built-in behavior, so
        # wire it explicitly to the same cancel path the Cancel button
        # takes.
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
        # without going through Cancel/OK/Escape — a bare Gtk.Window has
        # no other hook for this. Route it to the same revert path Cancel
        # takes, and let the default handling actually tear the window
        # down (return False) rather than calling win.close() ourselves
        # from inside its own close-request handling.
        def on_close_request(_win):
            on_response(False)
            return False

        win.connect('close-request', on_close_request)

        win.present()

    def _build_color_tab(self, colors):
        """Build a scrolled grid of color pickers for one theme mode.
        Returns (scroll_widget, buttons_dict, read_fn, load_fn)."""
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        grid.set_margin_top(8)
        grid.set_margin_start(4)
        grid.set_margin_end(4)
        grid.attach(Gtk.Label(label="<b>Element</b>", use_markup=True,
                              halign=Gtk.Align.START), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Foreground</b>", use_markup=True), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Background</b>", use_markup=True), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>B</b>", use_markup=True), 3, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>I</b>", use_markup=True), 4, 0, 1, 1)

        buttons = {}
        for row_i, (style_id, label) in enumerate(self.STYLE_ITEMS, start=1):
            props = colors.get(style_id, {})
            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, row_i, 1, 1)

            fg_chk = Gtk.CheckButton()
            fg_btn = Gtk.ColorButton()
            fg_btn.props.use_alpha = False
            fg_btn.set_title(f"{label} Foreground")
            if props.get('fg'):
                rgba = Gdk.RGBA()
                rgba.parse(props['fg'])
                fg_btn.set_rgba(rgba)
                fg_chk.set_active(True)
            fg_btn.set_sensitive(fg_chk.get_active())
            fg_chk.connect('toggled', lambda c, b: b.set_sensitive(c.get_active()), fg_btn)
            fg_btn.connect('color-set', lambda b, c: c.set_active(True), fg_chk)
            fg_box = Gtk.Box(spacing=2)
            fg_box.append(fg_chk)
            fg_box.append(fg_btn)
            grid.attach(fg_box, 1, row_i, 1, 1)

            bg_chk = Gtk.CheckButton()
            bg_btn = Gtk.ColorButton()
            bg_btn.props.use_alpha = False
            bg_btn.set_title(f"{label} Background")
            if props.get('bg'):
                rgba = Gdk.RGBA()
                rgba.parse(props['bg'])
                bg_btn.set_rgba(rgba)
                bg_chk.set_active(True)
            bg_btn.set_sensitive(bg_chk.get_active())
            bg_chk.connect('toggled', lambda c, b: b.set_sensitive(c.get_active()), bg_btn)
            bg_btn.connect('color-set', lambda b, c: c.set_active(True), bg_chk)
            bg_box = Gtk.Box(spacing=2)
            bg_box.append(bg_chk)
            bg_box.append(bg_btn)
            grid.attach(bg_box, 2, row_i, 1, 1)

            bold_chk = Gtk.CheckButton()
            bold_chk.set_active(props.get('bold', False))
            grid.attach(bold_chk, 3, row_i, 1, 1)

            italic_chk = Gtk.CheckButton()
            italic_chk.set_active(props.get('italic', False))
            grid.attach(italic_chk, 4, row_i, 1, 1)

            buttons[style_id] = {
                'fg_btn': fg_btn, 'fg_chk': fg_chk,
                'bg_btn': bg_btn, 'bg_chk': bg_chk,
                'bold_chk': bold_chk, 'italic_chk': italic_chk,
            }

        scroll.set_child(grid)

        def read_colors():
            result = {}
            for sid, w in buttons.items():
                p = {}
                if w['fg_chk'].get_active():
                    p['fg'] = self._rgba_to_hex(w['fg_btn'].get_rgba())
                if w['bg_chk'].get_active():
                    p['bg'] = self._rgba_to_hex(w['bg_btn'].get_rgba())
                if w['bold_chk'].get_active():
                    p['bold'] = True
                if w['italic_chk'].get_active():
                    p['italic'] = True
                if p:
                    result[sid] = p
            return result

        def load_colors(colors):
            for sid, w in buttons.items():
                p = colors.get(sid, {})
                if p.get('fg'):
                    rgba = Gdk.RGBA()
                    rgba.parse(p['fg'])
                    w['fg_btn'].set_rgba(rgba)
                    w['fg_chk'].set_active(True)
                else:
                    w['fg_chk'].set_active(False)
                w['fg_btn'].set_sensitive(w['fg_chk'].get_active())
                if p.get('bg'):
                    rgba = Gdk.RGBA()
                    rgba.parse(p['bg'])
                    w['bg_btn'].set_rgba(rgba)
                    w['bg_chk'].set_active(True)
                else:
                    w['bg_chk'].set_active(False)
                w['bg_btn'].set_sensitive(w['bg_chk'].get_active())
                w['bold_chk'].set_active(p.get('bold', False))
                w['italic_chk'].set_active(p.get('italic', False))

        return scroll, buttons, read_colors, load_colors

    def _on_custom_colors(self, _item):
        """Dialog to customize syntax colors with Dark and Light tabs."""
        dark_colors = dict(self.config.get('custom_colors_dark', {}))
        light_colors = dict(self.config.get('custom_colors_light', {}))

        win = Gtk.Window(
            title="Custom Colors",
            transient_for=self,
            modal=True,
        )
        win.set_default_size(540, 620)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        win.set_child(box)

        # --- Load saved scheme row ---
        load_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        load_row.append(Gtk.Label(label="Load scheme:", halign=Gtk.Align.START))

        scheme_combo = Gtk.ComboBoxText()
        scheme_combo.append('__none__', '(none)')
        for name in sorted(self.config.get('saved_color_schemes', {}).keys()):
            scheme_combo.append(name, name)
        active = self.config.get('active_custom_scheme', '')
        scheme_combo.set_active_id(active if active else '__none__')
        scheme_combo.set_hexpand(True)
        load_row.append(scheme_combo)

        btn_delete_scheme = Gtk.Button(label="Delete")
        btn_delete_scheme.set_tooltip_text("Delete selected scheme")
        load_row.append(btn_delete_scheme)

        box.append(load_row)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # --- Notebook with Dark / Light tabs ---
        # (This is a plain internal picker, not the main-window file tab
        # strip Task 5 replaces with Adw.TabView — Gtk.Notebook is still a
        # valid GTK4 widget and its API here is unchanged, so it stays.)
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)

        dark_scroll, dark_btns, read_dark, load_dark = self._build_color_tab(dark_colors)
        notebook.append_page(dark_scroll, Gtk.Label(label="Dark Mode"))

        light_scroll, light_btns, read_light, load_light = self._build_color_tab(light_colors)
        notebook.append_page(light_scroll, Gtk.Label(label="Light Mode"))

        # Start on the tab matching current theme
        notebook.set_current_page(0 if self.config.get('dark_theme', True) else 1)

        box.append(notebook)

        # --- Save button ---
        def on_save_scheme(_btn):
            name = scheme_name_entry.get_text().strip()
            if not name:
                self._show_error("Save Failed", "Please enter a scheme name.")
                return
            saved = self.config.get('saved_color_schemes', {})
            saved[name] = {
                'base': self.config.get('color_scheme', 'oblivion'),
                'colors_dark': read_dark(),
                'colors_light': read_light(),
            }
            self.config['saved_color_schemes'] = saved
            self.config['active_custom_scheme'] = name
            save_config(self.config)
            if scheme_combo.set_active_id(name) is None:
                scheme_combo.append(name, name)
                scheme_combo.set_active_id(name)
            self._set_status(f"Saved color scheme '{name}'")

        # --- Load on combo change ---
        def on_scheme_changed(combo):
            sid = combo.get_active_id()
            if sid == '__none__':
                load_dark({})
                load_light({})
                return
            saved = self.config.get('saved_color_schemes', {})
            if sid in saved:
                scheme_data = saved[sid]
                base = scheme_data.get('base', 'oblivion')
                self.config['color_scheme'] = base
                load_dark(scheme_data.get('colors_dark', {}))
                load_light(scheme_data.get('colors_light', {}))

        scheme_combo.connect('changed', on_scheme_changed)

        # --- Delete button ---
        def on_delete_scheme(_btn):
            sid = scheme_combo.get_active_id()
            if sid == '__none__':
                return
            saved = self.config.get('saved_color_schemes', {})
            if sid in saved:
                del saved[sid]
                self.config['saved_color_schemes'] = saved
                if self.config.get('active_custom_scheme') == sid:
                    self.config['active_custom_scheme'] = ''
                save_config(self.config)
                scheme_combo.remove_all()
                scheme_combo.append('__none__', '(none)')
                for n in sorted(saved.keys()):
                    scheme_combo.append(n, n)
                scheme_combo.set_active_id('__none__')

        btn_delete_scheme.connect('clicked', on_delete_scheme)

        # --- Save scheme row ---
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        save_row.append(Gtk.Label(label="Save as:", halign=Gtk.Align.START))
        scheme_name_entry = Gtk.Entry()
        scheme_name_entry.set_placeholder_text("Enter scheme name")
        scheme_name_entry.set_hexpand(True)
        current_name = self.config.get('active_custom_scheme', '')
        if current_name:
            scheme_name_entry.set_text(current_name)
        save_row.append(scheme_name_entry)
        btn_save_scheme = Gtk.Button(label="Save")
        btn_save_scheme.add_css_class('suggested-action')
        btn_save_scheme.connect('clicked', on_save_scheme)
        save_row.append(btn_save_scheme)
        box.append(save_row)

        # --- Bottom buttons ---
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        def on_response(kind):
            """.run()'s blocking return value becomes this button-click
            callback; `kind` is 'apply' / 'reset' / 'cancel', matching the
            old three-way `Gtk.ResponseType.OK` / `REJECT` / (anything
            else) branch exactly."""
            if kind == 'apply':
                self.config['custom_colors_dark'] = read_dark()
                self.config['custom_colors_light'] = read_light()
                active = scheme_combo.get_active_id()
                self.config['active_custom_scheme'] = active if active != '__none__' else ''
                save_config(self.config)
                self._apply_scheme_to_all()
            elif kind == 'reset':
                self.config['custom_colors_dark'] = {}
                self.config['custom_colors_light'] = {}
                self.config['active_custom_scheme'] = ''
                save_config(self.config)
                self._apply_scheme_to_all()
            win.close()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_bottom(8)
        btn_reset = Gtk.Button(label="Reset All")
        btn_reset.set_tooltip_text("Clear all custom colors (both modes)")
        btn_reset.connect('clicked', lambda _b: on_response('reset'))
        btn_row.append(btn_reset)

        # GTK3 packed Reset at the box's start and Cancel/Apply at its end
        # (pack_end(cancel) then pack_end(apply), reversing to [Apply,
        # Cancel] visually) — reproduce that split with an END-aligned,
        # hexpanding sub-box so Reset stays pinned left of Apply/Cancel.
        right_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right_group.set_hexpand(True)
        right_group.set_halign(Gtk.Align.END)
        btn_apply = Gtk.Button(label="Apply")
        btn_apply.add_css_class('suggested-action')
        btn_apply.connect('clicked', lambda _b: on_response('apply'))
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: on_response('cancel'))
        right_group.append(btn_apply)
        right_group.append(btn_cancel)
        btn_row.append(right_group)

        box.append(btn_row)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, so neither Apply nor Reset ran); a bare
        # Gtk.Window has no such built-in behavior, so wire it explicitly
        # to the same cancel path the Cancel button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                on_response('cancel')
                return True
            return False

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        win.present()

    def _rgba_to_hex(self, rgba):
        """Convert a Gdk.RGBA to #rrggbb hex string."""
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        return f'#{r:02x}{g:02x}{b:02x}'
