"""SynPad compare/diff mixin."""

import difflib
import hashlib
import os
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from config import find_server_by_guid


class CompareMixin:
    """Mixin for SynPadWindow — tab comparison and conflict diff."""

    def _on_compare_tabs(self):
        """Compare two open tabs side by side with diff highlighting."""
        if len(self.tabs) < 2:
            self._show_error("Compare", "Need at least 2 open tabs to compare.")
            return

        # Pick two tabs dialog
        dlg = Gtk.Dialog(
            title="Compare Tabs",
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

        box.pack_start(Gtk.Label(label="Select two tabs to compare:",
                                 halign=Gtk.Align.START), False, False, 0)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.attach(Gtk.Label(label="Left:", halign=Gtk.Align.END), 0, 0, 1, 1)
        combo_a = Gtk.ComboBoxText()
        grid.attach(combo_a, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Right:", halign=Gtk.Align.END), 0, 1, 1, 1)
        combo_b = Gtk.ComboBoxText()
        grid.attach(combo_b, 1, 1, 1, 1)

        for page_num, tab in sorted(self.tabs.items()):
            filename = os.path.basename(tab.remote_path)
            if tab.is_local:
                parent = os.path.dirname(tab.remote_path)
                label = f"{filename}  ({parent})" if parent else filename
            else:
                srv = find_server_by_guid(self.config, tab.server_guid)
                srv_name = srv['name'] if srv else 'unknown'
                parent = os.path.dirname(tab.remote_path)
                label = f"{filename}  ({srv_name}:{parent})"
            combo_a.append(str(page_num), label)
            combo_b.append(str(page_num), label)

        # Pre-select current tab as left
        current = self.notebook.get_current_page()
        combo_a.set_active_id(str(current))

        box.pack_start(grid, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_row.pack_end(btn_cancel, False, False, 0)
        btn_compare = Gtk.Button(label="Compare")
        btn_compare.get_style_context().add_class('suggested-action')
        btn_compare.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_end(btn_compare, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        dlg.show_all()
        resp = dlg.run()

        if resp == Gtk.ResponseType.OK:
            id_a = combo_a.get_active_id()
            id_b = combo_b.get_active_id()
            dlg.destroy()
            if id_a is None or id_b is None:
                return
            if id_a == id_b:
                self._show_error("Compare", "Please select two different tabs.")
                return
            tab_a = self.tabs.get(int(id_a))
            tab_b = self.tabs.get(int(id_b))
            if tab_a and tab_b:
                self._show_diff(tab_a, tab_b)
        else:
            dlg.destroy()

    def _show_diff(self, tab_a, tab_b):
        """Show a side-by-side diff window comparing two tabs."""
        import difflib

        buf_a = tab_a.buffer
        buf_b = tab_b.buffer
        text_a = buf_a.get_text(buf_a.get_start_iter(), buf_a.get_end_iter(), True)
        text_b = buf_b.get_text(buf_b.get_start_iter(), buf_b.get_end_iter(), True)

        name_a = os.path.basename(tab_a.remote_path)
        name_b = os.path.basename(tab_b.remote_path)

        lines_a = text_a.splitlines()
        lines_b = text_b.splitlines()

        if lines_a == lines_b:
            self._show_info("Compare", f"'{name_a}' and '{name_b}' are identical.")
            return

        # Build side-by-side lines with opcodes
        sm = difflib.SequenceMatcher(None, lines_a, lines_b)
        opcodes = sm.get_opcodes()

        # Each entry: (left_text, right_text, tag)
        # tag: 'equal', 'replace', 'delete', 'insert'
        diff_rows = []
        change_positions = []  # line indices where changes occur

        for op, i1, i2, j1, j2 in opcodes:
            if op == 'equal':
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    diff_rows.append((lines_a[i], lines_b[j], 'equal'))
            elif op == 'replace':
                # Pair up lines: both exist = replace, only left = delete, only right = insert
                left_lines = list(range(i1, i2))
                right_lines = list(range(j1, j2))
                paired = min(len(left_lines), len(right_lines))
                for k in range(paired):
                    change_positions.append(len(diff_rows))
                    diff_rows.append((lines_a[left_lines[k]], lines_b[right_lines[k]], 'replace'))
                # Remaining left lines are deletions
                for k in range(paired, len(left_lines)):
                    change_positions.append(len(diff_rows))
                    diff_rows.append((lines_a[left_lines[k]], '', 'delete'))
                # Remaining right lines are insertions
                for k in range(paired, len(right_lines)):
                    change_positions.append(len(diff_rows))
                    diff_rows.append(('', lines_b[right_lines[k]], 'insert'))
            elif op == 'delete':
                for i in range(i1, i2):
                    change_positions.append(len(diff_rows))
                    diff_rows.append((lines_a[i], '', 'delete'))
            elif op == 'insert':
                for j in range(j1, j2):
                    change_positions.append(len(diff_rows))
                    diff_rows.append(('', lines_b[j], 'insert'))

        total_lines = len(diff_rows)

        # --- Build window ---
        win = Gtk.Window(
            title=f"Diff: {name_a} vs {name_b}",
            transient_for=self,
            destroy_with_parent=True,
        )
        win.set_default_size(1000, 700)

        # De-duplicate change_positions to get unique change blocks
        change_blocks = []
        prev_pos = -2
        for pos in change_positions:
            if pos != prev_pos + 1:
                change_blocks.append(pos)
            prev_pos = pos
        current_change = [0]

        # --- Navigation toolbar ---
        nav_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        nav_bar.set_margin_start(6)
        nav_bar.set_margin_end(6)
        nav_bar.set_margin_top(4)
        nav_bar.set_margin_bottom(4)

        btn_prev_change = Gtk.Button()
        btn_prev_change.set_image(Gtk.Image.new_from_icon_name(
            'go-up-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_prev_change.set_relief(Gtk.ReliefStyle.NONE)
        btn_prev_change.set_tooltip_text("Previous change")
        nav_bar.pack_start(btn_prev_change, False, False, 0)

        btn_next_change = Gtk.Button()
        btn_next_change.set_image(Gtk.Image.new_from_icon_name(
            'go-down-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_next_change.set_relief(Gtk.ReliefStyle.NONE)
        btn_next_change.set_tooltip_text("Next change")
        nav_bar.pack_start(btn_next_change, False, False, 0)

        change_label = Gtk.Label()
        if change_blocks:
            change_label.set_text(f"Change 1 of {len(change_blocks)}")
        else:
            change_label.set_text("No changes")
        nav_bar.pack_start(change_label, False, False, 8)

        # Left + Right panes in a horizontal box with synced scrolling
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        # --- Left pane ---
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_header = Gtk.Label()
        left_header.set_markup(f"<b>{name_a}</b>")
        left_header.set_margin_start(6)
        left_header.set_margin_top(4)
        left_header.set_margin_bottom(4)
        left_header.set_halign(Gtk.Align.START)
        left_box.pack_start(left_header, False, False, 0)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        left_buf = Gtk.TextBuffer()
        left_buf.create_tag('replace', background='#edd400', foreground='#000000')
        left_buf.create_tag('delete', background='#ef2929', foreground='#ffffff')
        left_buf.create_tag('insert', background='#555753', foreground='#888a85')
        left_buf.create_tag('linenum', foreground='#888a85')

        left_view = Gtk.TextView(buffer=left_buf)
        left_view.set_editable(False)
        left_view.set_cursor_visible(False)
        left_view.set_monospace(True)
        left_scroll.add(left_view)
        left_box.pack_start(left_scroll, True, True, 0)

        # --- Right pane ---
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_header = Gtk.Label()
        right_header.set_markup(f"<b>{name_b}</b>")
        right_header.set_margin_start(6)
        right_header.set_margin_top(4)
        right_header.set_margin_bottom(4)
        right_header.set_halign(Gtk.Align.START)
        right_box.pack_start(right_header, False, False, 0)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        right_buf = Gtk.TextBuffer()
        right_buf.create_tag('replace', background='#edd400', foreground='#000000')
        right_buf.create_tag('insert', background='#73d216', foreground='#000000')
        right_buf.create_tag('delete', background='#555753', foreground='#888a85')
        right_buf.create_tag('linenum', foreground='#888a85')

        right_view = Gtk.TextView(buffer=right_buf)
        right_view.set_editable(False)
        right_view.set_cursor_visible(False)
        right_view.set_monospace(True)
        right_scroll.add(right_view)
        right_box.pack_start(right_scroll, True, True, 0)

        # Separator between panes
        content_box.pack_start(left_box, True, True, 0)
        content_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                               False, False, 0)
        content_box.pack_start(right_box, True, True, 0)

        # --- Fill buffers with line numbers ---
        max_digits = len(str(max(len(lines_a), len(lines_b))))
        left_num = 0
        right_num = 0
        for left_text, right_text, tag in diff_rows:
            # Left side
            end_l = left_buf.get_end_iter()
            if tag == 'insert':
                left_buf.insert_with_tags_by_name(end_l,
                    f"{'':>{max_digits}}  \n", tag)
            else:
                left_num += 1
                left_buf.insert_with_tags_by_name(end_l,
                    f"{left_num:>{max_digits}}  ", 'linenum')
                end_l = left_buf.get_end_iter()
                if tag == 'equal':
                    left_buf.insert(end_l, f"{left_text}\n")
                else:
                    left_buf.insert_with_tags_by_name(end_l, f"{left_text}\n", tag)

            # Right side
            end_r = right_buf.get_end_iter()
            if tag == 'delete':
                right_buf.insert_with_tags_by_name(end_r,
                    f"{'':>{max_digits}}  \n", tag)
            else:
                right_num += 1
                right_buf.insert_with_tags_by_name(end_r,
                    f"{right_num:>{max_digits}}  ", 'linenum')
                end_r = right_buf.get_end_iter()
                if tag == 'equal':
                    right_buf.insert(end_r, f"{right_text}\n")
                else:
                    right_buf.insert_with_tags_by_name(end_r, f"{right_text}\n", tag)

        # --- Sync scrolling between left and right ---
        # Use left's vadjustment as the master
        _syncing = [False]

        def _sync_left_to_right(*_args):
            if _syncing[0]:
                return
            _syncing[0] = True
            left_vadj = left_scroll.get_vadjustment()
            right_vadj = right_scroll.get_vadjustment()
            right_vadj.set_value(left_vadj.get_value())
            _syncing[0] = False

        def _sync_right_to_left(*_args):
            if _syncing[0]:
                return
            _syncing[0] = True
            left_vadj = left_scroll.get_vadjustment()
            right_vadj = right_scroll.get_vadjustment()
            left_vadj.set_value(right_vadj.get_value())
            _syncing[0] = False

        left_scroll.get_vadjustment().connect('value-changed', _sync_left_to_right)
        right_scroll.get_vadjustment().connect('value-changed', _sync_right_to_left)

        # --- Navigation button handlers ---
        def _goto_change(idx):
            if not change_blocks:
                return
            idx = max(0, min(idx, len(change_blocks) - 1))
            current_change[0] = idx
            change_label.set_text(f"Change {idx + 1} of {len(change_blocks)}")
            # Scroll to the change line
            line = change_blocks[idx]
            vadj = left_scroll.get_vadjustment()
            if total_lines > 0 and vadj.get_upper() > 0:
                fraction = line / total_lines
                target = fraction * vadj.get_upper()
                vadj.set_value(max(0, target - vadj.get_page_size() / 3))

        def _on_prev_change(_btn):
            _goto_change(current_change[0] - 1)

        def _on_next_change(_btn):
            _goto_change(current_change[0] + 1)

        btn_prev_change.connect('clicked', _on_prev_change)
        btn_next_change.connect('clicked', _on_next_change)

        # --- Change minimap using colored labels in a scrolled list ---
        minimap_box_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Build a colored bar for each line
        colors = {'equal': None, 'replace': '#edd400', 'delete': '#ef2929', 'insert': '#73d216'}
        minimap_labels = []
        for i, (_, _, tag) in enumerate(diff_rows):
            color = colors.get(tag)
            if color:
                lbl = Gtk.EventBox()
                lbl.set_size_request(20, 2)
                lbl.override_background_color(
                    Gtk.StateFlags.NORMAL,
                    Gdk.RGBA(*[c / 255.0 for c in bytes.fromhex(color[1:])], 1.0))
                minimap_labels.append((i, lbl))
                minimap_box_inner.pack_start(lbl, False, False, 0)
            else:
                spacer = Gtk.Box()
                spacer.set_size_request(20, 2)
                minimap_box_inner.pack_start(spacer, False, False, 0)

        minimap_scroll = Gtk.ScrolledWindow()
        minimap_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        minimap_scroll.set_size_request(28, -1)
        minimap_scroll.add(minimap_box_inner)

        # Click on minimap label scrolls to that line
        for line_idx, lbl in minimap_labels:
            lbl.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            def _on_click(_w, _ev, idx=line_idx):
                vadj = left_scroll.get_vadjustment()
                if total_lines > 0 and vadj.get_upper() > 0:
                    fraction = idx / total_lines
                    target = fraction * vadj.get_upper()
                    vadj.set_value(max(0, target - vadj.get_page_size() / 2))
                return True
            lbl.connect('button-press-event', _on_click)

        # Sync minimap scroll with content scroll
        def _sync_minimap(*_args):
            vadj = left_scroll.get_vadjustment()
            madj = minimap_scroll.get_vadjustment()
            if vadj.get_upper() > 0 and madj.get_upper() > 0:
                fraction = vadj.get_value() / vadj.get_upper()
                madj.set_value(fraction * madj.get_upper())
        left_scroll.get_vadjustment().connect('value-changed', _sync_minimap)

        # Status bar with change count
        status = Gtk.Label()
        changes = len(change_blocks)
        status.set_markup(f"  <b>{changes}</b> change(s) found")
        status.set_halign(Gtk.Align.START)
        status.set_margin_start(8)
        status.set_margin_top(4)
        status.set_margin_bottom(4)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Top row: content + minimap
        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top_box.pack_start(content_box, True, True, 0)
        top_box.pack_start(minimap_scroll, False, False, 0)
        outer.pack_start(nav_bar, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 0)
        outer.pack_start(top_box, True, True, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 0)
        outer.pack_start(status, False, False, 0)

        win.add(outer)
        win.show_all()

        # Redraw minimap after window is fully laid out
        def _init_minimap():
            minimap.queue_draw()
            return False
        GLib.timeout_add(200, _init_minimap)
        GLib.timeout_add(500, _init_minimap)

    def _show_conflict_diff(self, tab, local_content, remote_content,
                            page_num, max_mb, mgr):
        """Show a diff between local changes and server version with action buttons."""
        import difflib

        name = os.path.basename(tab.remote_path)
        lines_local = local_content.splitlines()
        lines_remote = remote_content.splitlines()

        sm = difflib.SequenceMatcher(None, lines_local, lines_remote)
        opcodes = sm.get_opcodes()

        diff_rows = []
        for op, i1, i2, j1, j2 in opcodes:
            if op == 'equal':
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    diff_rows.append((lines_local[i], lines_remote[j], 'equal'))
            elif op == 'replace':
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    diff_rows.append((lines_local[i1 + k], lines_remote[j1 + k], 'replace'))
                for k in range(paired, i2 - i1):
                    diff_rows.append((lines_local[i1 + k], '', 'delete'))
                for k in range(paired, j2 - j1):
                    diff_rows.append(('', lines_remote[j1 + k], 'insert'))
            elif op == 'delete':
                for i in range(i1, i2):
                    diff_rows.append((lines_local[i], '', 'delete'))
            elif op == 'insert':
                for j in range(j1, j2):
                    diff_rows.append(('', lines_remote[j], 'insert'))

        win = Gtk.Window(
            title=f"Conflict: {name} — My Changes vs Server",
            transient_for=self,
            destroy_with_parent=True,
        )
        win.set_default_size(1000, 700)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Action buttons at top
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_start(8)
        action_bar.set_margin_end(8)
        action_bar.set_margin_top(6)
        action_bar.set_margin_bottom(6)

        btn_use_mine = Gtk.Button(label="Use My Changes (Overwrite Server)")
        btn_use_mine.get_style_context().add_class('destructive-action')
        action_bar.pack_start(btn_use_mine, False, False, 0)

        btn_use_server = Gtk.Button(label="Use Server Version")
        action_bar.pack_start(btn_use_server, False, False, 0)

        btn_cancel = Gtk.Button(label="Cancel")
        action_bar.pack_end(btn_cancel, False, False, 0)

        outer.pack_start(action_bar, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 0)

        # Build side-by-side diff view (reuse the same approach as Compare Tabs)
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left_header = Gtk.Label()
        left_header.set_markup(f"<b>My Changes</b>")
        left_header.set_margin_start(6)
        left_header.set_margin_top(4)
        left_header.set_margin_bottom(4)
        left_header.set_halign(Gtk.Align.START)
        left_box.pack_start(left_header, False, False, 0)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        left_buf = Gtk.TextBuffer()
        left_buf.create_tag('replace', background='#edd400', foreground='#000000')
        left_buf.create_tag('delete', background='#ef2929', foreground='#ffffff')
        left_buf.create_tag('insert', background='#555753', foreground='#888a85')
        left_view = Gtk.TextView(buffer=left_buf)
        left_view.set_editable(False)
        left_view.set_cursor_visible(False)
        left_view.set_monospace(True)
        left_scroll.add(left_view)
        left_box.pack_start(left_scroll, True, True, 0)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right_header = Gtk.Label()
        right_header.set_markup(f"<b>Server Version</b>")
        right_header.set_margin_start(6)
        right_header.set_margin_top(4)
        right_header.set_margin_bottom(4)
        right_header.set_halign(Gtk.Align.START)
        right_box.pack_start(right_header, False, False, 0)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_buf = Gtk.TextBuffer()
        right_buf.create_tag('replace', background='#edd400', foreground='#000000')
        right_buf.create_tag('insert', background='#73d216', foreground='#000000')
        right_buf.create_tag('delete', background='#555753', foreground='#888a85')
        right_view = Gtk.TextView(buffer=right_buf)
        right_view.set_editable(False)
        right_view.set_cursor_visible(False)
        right_view.set_monospace(True)
        right_scroll.add(right_view)
        right_box.pack_start(right_scroll, True, True, 0)

        # Sync scrolling
        _syncing = [False]
        def _sync_lr(*_a):
            if _syncing[0]: return
            _syncing[0] = True
            right_scroll.get_vadjustment().set_value(left_scroll.get_vadjustment().get_value())
            _syncing[0] = False
        def _sync_rl(*_a):
            if _syncing[0]: return
            _syncing[0] = True
            left_scroll.get_vadjustment().set_value(right_scroll.get_vadjustment().get_value())
            _syncing[0] = False
        left_scroll.get_vadjustment().connect('value-changed', _sync_lr)
        right_scroll.get_vadjustment().connect('value-changed', _sync_rl)

        content_box.pack_start(left_box, True, True, 0)
        content_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                               False, False, 0)
        content_box.pack_start(right_box, True, True, 0)

        # Fill buffers with line numbers
        left_buf.create_tag('linenum', foreground='#888a85')
        right_buf.create_tag('linenum', foreground='#888a85')
        max_digits = len(str(max(len(lines_local), len(lines_remote))))
        left_num = 0
        right_num = 0
        for left_text, right_text, tag in diff_rows:
            end_l = left_buf.get_end_iter()
            if tag == 'insert':
                left_buf.insert_with_tags_by_name(end_l,
                    f"{'':>{max_digits}}  \n", tag)
            else:
                left_num += 1
                left_buf.insert_with_tags_by_name(end_l,
                    f"{left_num:>{max_digits}}  ", 'linenum')
                end_l = left_buf.get_end_iter()
                if tag == 'equal':
                    left_buf.insert(end_l, f"{left_text}\n")
                else:
                    left_buf.insert_with_tags_by_name(end_l, f"{left_text}\n", tag)

            end_r = right_buf.get_end_iter()
            if tag == 'delete':
                right_buf.insert_with_tags_by_name(end_r,
                    f"{'':>{max_digits}}  \n", tag)
            else:
                right_num += 1
                right_buf.insert_with_tags_by_name(end_r,
                    f"{right_num:>{max_digits}}  ", 'linenum')
                end_r = right_buf.get_end_iter()
                if tag == 'equal':
                    right_buf.insert(end_r, f"{right_text}\n")
                else:
                    right_buf.insert_with_tags_by_name(end_r, f"{right_text}\n", tag)

        outer.pack_start(content_box, True, True, 0)

        # Button actions
        def _on_use_mine(_btn):
            win.destroy()
            self._set_status(f"Uploading {tab.remote_path} (overwrite)...")
            self.item_save.set_sensitive(False)
            def _upload():
                try:
                    mgr.upload(tab.remote_path, tab.local_path, max_mb)
                    tab.remote_mtime = mgr.get_remote_mtime(tab.remote_path)
                    tab.remote_size = mgr.get_remote_size(tab.remote_path)
                    with open(tab.local_path, 'rb') as f:
                        tab.remote_hash = hashlib.sha256(f.read()).hexdigest()
                    GLib.idle_add(self._on_upload_done, tab, page_num)
                except Exception as e:
                    GLib.idle_add(self._on_upload_failed, str(e))
            threading.Thread(target=_upload, daemon=True).start()

        def _on_use_server(_btn):
            win.destroy()
            tab.buffer.begin_user_action()
            tab.buffer.set_text(remote_content)
            tab.buffer.end_user_action()
            tab.buffer.set_modified(False)
            tab.remote_hash = hashlib.sha256(
                remote_content.encode('utf-8')).hexdigest()
            tab.remote_mtime = mgr.get_remote_mtime(tab.remote_path)
            tab.remote_size = mgr.get_remote_size(tab.remote_path)
            with open(tab.local_path, 'w') as f:
                f.write(remote_content)
            self._set_status(f"Loaded server version of {name}")
            self._console_log(f"Using server version: {tab.remote_path}", 'success')

        def _on_cancel(_btn):
            win.destroy()
            self._set_status("Upload cancelled")

        btn_use_mine.connect('clicked', _on_use_mine)
        btn_use_server.connect('clicked', _on_use_server)
        btn_cancel.connect('clicked', _on_cancel)

        win.add(outer)
        win.show_all()
