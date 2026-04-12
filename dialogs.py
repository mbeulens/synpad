"""SynPad dialogs mixin — settings, color schemes, file types."""

import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GtkSource, Gdk, GLib

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
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
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
        dlg.destroy()

    def _on_edit_file_types(self, _item):
        """Dialog to manage which file extensions open in the editor."""
        dlg = Gtk.Dialog(
            title="File Types — Editor Extensions",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(400, 450)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        box.pack_start(Gtk.Label(
            label="File extensions that open in the editor.\n"
                  "All other files open with the system default app.",
            halign=Gtk.Align.START, wrap=True), False, False, 0)

        # Extensions list
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

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

        scroll.add(ext_view)
        box.pack_start(scroll, True, True, 0)

        # Add/Remove row
        edit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ext_entry = Gtk.Entry(placeholder_text="e.g. jsx")
        ext_entry.set_hexpand(True)
        edit_row.pack_start(ext_entry, True, True, 0)

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
        edit_row.pack_start(btn_add, False, False, 0)

        btn_remove = Gtk.Button(label="Remove")
        def _on_remove(_btn):
            sel = ext_view.get_selection()
            model, it = sel.get_selected()
            if it:
                model.remove(it)
        btn_remove.connect('clicked', _on_remove)
        edit_row.pack_start(btn_remove, False, False, 0)

        box.pack_start(edit_row, False, False, 0)

        # Buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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
            new_exts = []
            for row in ext_store:
                new_exts.append(row[0])
            self.config['editor_extensions'] = sorted(new_exts)
            save_config(self.config)

        dlg.destroy()

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
        """Set the GTK application-wide dark/light preference."""
        settings = Gtk.Settings.get_default()
        settings.set_property(
            'gtk-application-prefer-dark-theme',
            self.config.get('dark_theme', True),
        )

    def _on_pick_scheme(self, _item):
        """Dialog to pick a GtkSourceView color scheme."""
        dlg = Gtk.Dialog(
            title="Color Scheme",
            transient_for=self,
            modal=True,
        )
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dlg.set_default_size(350, 400)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)

        box.add(Gtk.Label(label="Select a color scheme:", halign=Gtk.Align.START))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

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

        scroll.add(tv)
        box.pack_start(scroll, True, True, 0)
        dlg.show_all()

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            model, it = tv.get_selection().get_selected()
            if it:
                self.config['color_scheme'] = model[it][0]
                self.config['custom_colors'] = {}  # reset custom when changing base
                save_config(self.config)
                self._apply_scheme_to_all()
        else:
            # Revert preview
            self._apply_scheme_to_all()
        dlg.destroy()

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
            fg_box.pack_start(fg_chk, False, False, 0)
            fg_box.pack_start(fg_btn, False, False, 0)
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
            bg_box.pack_start(bg_chk, False, False, 0)
            bg_box.pack_start(bg_btn, False, False, 0)
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

        scroll.add(grid)

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

        dlg = Gtk.Dialog(
            title="Custom Colors",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(540, 620)

        box = dlg.get_content_area()
        box.set_spacing(4)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)

        # --- Load saved scheme row ---
        load_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        load_row.pack_start(Gtk.Label(label="Load scheme:", halign=Gtk.Align.START), False, False, 0)

        scheme_combo = Gtk.ComboBoxText()
        scheme_combo.append('__none__', '(none)')
        for name in sorted(self.config.get('saved_color_schemes', {}).keys()):
            scheme_combo.append(name, name)
        active = self.config.get('active_custom_scheme', '')
        scheme_combo.set_active_id(active if active else '__none__')
        load_row.pack_start(scheme_combo, True, True, 0)

        btn_delete_scheme = Gtk.Button(label="Delete")
        btn_delete_scheme.set_tooltip_text("Delete selected scheme")
        load_row.pack_start(btn_delete_scheme, False, False, 0)

        box.pack_start(load_row, False, False, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        # --- Notebook with Dark / Light tabs ---
        notebook = Gtk.Notebook()

        dark_scroll, dark_btns, read_dark, load_dark = self._build_color_tab(dark_colors)
        notebook.append_page(dark_scroll, Gtk.Label(label="Dark Mode"))

        light_scroll, light_btns, read_light, load_light = self._build_color_tab(light_colors)
        notebook.append_page(light_scroll, Gtk.Label(label="Light Mode"))

        # Start on the tab matching current theme
        notebook.set_current_page(0 if self.config.get('dark_theme', True) else 1)

        box.pack_start(notebook, True, True, 0)

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
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        save_row.pack_start(Gtk.Label(label="Save as:", halign=Gtk.Align.START), False, False, 0)
        scheme_name_entry = Gtk.Entry()
        scheme_name_entry.set_placeholder_text("Enter scheme name")
        current_name = self.config.get('active_custom_scheme', '')
        if current_name:
            scheme_name_entry.set_text(current_name)
        save_row.pack_start(scheme_name_entry, True, True, 0)
        btn_save_scheme = Gtk.Button(label="Save")
        btn_save_scheme.get_style_context().add_class('suggested-action')
        btn_save_scheme.connect('clicked', on_save_scheme)
        save_row.pack_start(btn_save_scheme, False, False, 0)
        box.pack_start(save_row, False, False, 0)

        # --- Bottom buttons ---
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_bottom(8)
        btn_reset = Gtk.Button(label="Reset All")
        btn_reset.set_tooltip_text("Clear all custom colors (both modes)")
        btn_row.pack_start(btn_reset, False, False, 0)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_row.pack_end(btn_cancel, False, False, 0)
        btn_apply = Gtk.Button(label="Apply")
        btn_apply.get_style_context().add_class('suggested-action')
        btn_row.pack_end(btn_apply, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        btn_apply.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_reset.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.REJECT))

        dlg.show_all()
        resp = dlg.run()

        if resp == Gtk.ResponseType.OK:
            self.config['custom_colors_dark'] = read_dark()
            self.config['custom_colors_light'] = read_light()
            active = scheme_combo.get_active_id()
            self.config['active_custom_scheme'] = active if active != '__none__' else ''
            save_config(self.config)
            self._apply_scheme_to_all()
        elif resp == Gtk.ResponseType.REJECT:
            self.config['custom_colors_dark'] = {}
            self.config['custom_colors_light'] = {}
            self.config['active_custom_scheme'] = ''
            save_config(self.config)
            self._apply_scheme_to_all()

        dlg.destroy()

    def _rgba_to_hex(self, rgba):
        """Convert a Gdk.RGBA to #rrggbb hex string."""
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        return f'#{r:02x}{g:02x}{b:02x}'
