"""compare.py under GTK4 — headless.

Guards the Gtk.Dialog -> Gtk.Window + explicit-buttons migration for
`_on_compare_tabs` (the module's one `.run()` site — custom content, a pair
of combo boxes, so no Adw.AlertDialog per the migration plan's dialog
shape), the Gtk.EventBox -> Gtk.GestureClick + CSS-class migration for the
diff minimap, and the extensive pack_start/pack_end -> append() packing
conversion in `_show_diff` / `_show_conflict_diff` (both already plain
Gtk.Window in the GTK3 original, so — unlike `_on_compare_tabs` — neither
gets new Escape-to-close behavior, since none existed before).

Never touches real config/session/keyring: this module doesn't call
save_config/load_config or secrets_store at all, so no monkeypatching is
required here (unlike remote.py).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import compare
from compare import CompareMixin
from tab import OpenTab

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


def find_all(widget, cls):
    """Walk a widget's child tree collecting instances of `cls`, depth-first
    in append order (= left-to-right / top-to-bottom visual order for a
    Gtk.Box)."""
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            found.append(child)
        found.extend(find_all(child, cls))
        child = child.get_next_sibling()
    return found


def labeled_buttons(win):
    """A bare Gtk.Window's child tree contains an internal CSD close
    button with no label — filter those out."""
    return [b for b in find_all(win, Gtk.Button) if b.get_label()]


def escape_key_controllers(win):
    return [c for c in win.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)]


def press_escape(win):
    for kc in escape_key_controllers(win):
        kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)


def capture_window(build_fn):
    """Capture the Gtk.Window a mixin method builds, by temporarily
    subclassing Gtk.Window at the shared gi.repository module level —
    compare.py's `Gtk` name is that same module object."""
    captured = []
    class _CapturingWindow(Gtk.Window):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
    _Real = Gtk.Window
    Gtk.Window = _CapturingWindow
    try:
        build_fn()
    finally:
        Gtk.Window = _Real
    return captured[0] if captured else None


class FakeNotebook:
    """Stands in for the real Adw.TabView (Task 5's Gtk.Notebook -> Adw.
    TabView conversion) — get_page_position() is identity here since this
    test's self.tabs is still keyed by plain int for simplicity, which
    sorts the same way a real TabPage's position would."""
    def __init__(self, current=0):
        self._current = current
    def get_selected_page(self): return self._current
    def get_page_position(self, page): return page


class FakeItemSave:
    def __init__(self): self.sensitive = []
    def set_sensitive(self, v): self.sensitive.append(v)


class Host(CompareMixin, Gtk.Window):
    """A real Gtk.Window subclass — every dialog/window here uses
    `transient_for=self`, so the host must be an actual window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.tabs = {}
        self.config = {}
        self.notebook = FakeNotebook()
        self.status = []
        self.errors = []
        self.infos = []
        self.console = []
        self.item_save = FakeItemSave()
        self.diff_calls = []
        self.upload_done = []
        self.upload_failed = []
        self.tab_label_updates = []

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _show_info(self, title, msg): self.infos.append((title, msg))
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _show_diff(self, tab_a, tab_b): self.diff_calls.append((tab_a, tab_b))
    def _on_upload_done(self, tab, page_num): self.upload_done.append((tab, page_num))
    def _on_upload_failed(self, err): self.upload_failed.append(err)
    def _update_tab_label(self, tab, name): self.tab_label_updates.append((tab, name))


class DiffHost(CompareMixin, Gtk.Window):
    """A second host WITHOUT a `_show_diff` stub — `Host` above stubs
    `_show_diff` to spy on `_on_compare_tabs`'s call-through, which would
    shadow CompareMixin's real `_show_diff` if reused here. This class
    exercises the real `_show_diff` / `_show_conflict_diff` bodies."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.tabs = {}
        self.config = {}
        self.status = []
        self.errors = []
        self.infos = []
        self.console = []
        self.item_save = FakeItemSave()
        self.upload_done = []
        self.upload_failed = []
        self.tab_label_updates = []

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _show_info(self, title, msg): self.infos.append((title, msg))
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _on_upload_done(self, tab, page_num): self.upload_done.append((tab, page_num))
    def _on_upload_failed(self, err): self.upload_failed.append(err)
    def _update_tab_label(self, tab, name): self.tab_label_updates.append((tab, name))


def make_tab(remote_path, text, is_local=False, server_guid=''):
    buf = Gtk.TextBuffer()
    buf.set_text(text)
    return OpenTab(remote_path, remote_path, None, buf, is_local=is_local,
                   server_guid=server_guid)


# =====================================================================
# _on_compare_tabs — Gtk.Dialog -> Gtk.Window + explicit buttons
# =====================================================================

h = Host()

# --- fewer than 2 tabs: error only, no window built ---
h.tabs = {0: make_tab('/a.txt', 'hello')}
win = capture_window(lambda: h._on_compare_tabs())
check("<2 tabs: no window built", win is None)
check("<2 tabs: error shown", h.errors and h.errors[-1] ==
      ("Compare", "Need at least 2 open tabs to compare."), h.errors)

# --- 2+ tabs: window built with combo boxes ---
h.errors.clear()
h.tabs = {0: make_tab('/a.txt', 'hello'), 1: make_tab('/b.txt', 'world')}
h.notebook = FakeNotebook(current=1)
win = capture_window(lambda: h._on_compare_tabs())
check("compare window created", win is not None)
check("compare window title", win is not None and win.get_title() == "Compare Tabs",
      win.get_title() if win else None)

combos = find_all(win, Gtk.ComboBoxText)
check("two combo boxes present", len(combos) == 2, len(combos))
combo_a, combo_b = combos
check("left combo preselected to current page",
      combo_a.get_active_id() == '1', combo_a.get_active_id())

btns = labeled_buttons(win)
labels = [b.get_label() for b in btns]
check("Cancel and Compare present", set(labels) == {"Cancel", "Compare"}, labels)
by_label = {b.get_label(): b for b in btns}
compare_i, cancel_i = labels.index("Compare"), labels.index("Cancel")
check("button visual order has Compare before Cancel (matches old pack_end reversal)",
      compare_i < cancel_i, labels)

# --- Compare with two different tabs opens the diff ---
combo_a.set_active_id('0')
combo_b.set_active_id('1')
h.diff_calls.clear()
by_label["Compare"].emit('clicked')
check("Compare with 2 different tabs calls _show_diff",
      len(h.diff_calls) == 1, h.diff_calls)
check("_show_diff called with the right tab objects",
      h.diff_calls and h.diff_calls[0] == (h.tabs[0], h.tabs[1]))
check("compare window closes after Compare", win.get_visible() is False)

# --- Compare with the same tab picked twice: error, no diff ---
win2 = capture_window(lambda: h._on_compare_tabs())
combos2 = find_all(win2, Gtk.ComboBoxText)
combos2[0].set_active_id('0')
combos2[1].set_active_id('0')
h.diff_calls.clear()
h.errors.clear()
by_label2 = {b.get_label(): b for b in labeled_buttons(win2)}
by_label2["Compare"].emit('clicked')
check("same-tab pick shows an error", h.errors and h.errors[-1] ==
      ("Compare", "Please select two different tabs."), h.errors)
check("same-tab pick does not call _show_diff", h.diff_calls == [], h.diff_calls)
check("window closes even on the same-tab error", win2.get_visible() is False)

# --- Cancel makes no diff call ---
win3 = capture_window(lambda: h._on_compare_tabs())
h.diff_calls.clear()
by_label3 = {b.get_label(): b for b in labeled_buttons(win3)}
by_label3["Cancel"].emit('clicked')
check("Cancel does not call _show_diff", h.diff_calls == [], h.diff_calls)
check("Cancel closes the window", win3.get_visible() is False)

# --- Escape behaves like Cancel (Gtk.Dialog had a built-in close-on-Escape) ---
win4 = capture_window(lambda: h._on_compare_tabs())
key_ctrls = escape_key_controllers(win4)
check("compare window has at least one key controller (ours + GTK's own)",
      len(key_ctrls) >= 1, key_ctrls)
h.diff_calls.clear()
press_escape(win4)
check("Escape does not call _show_diff", h.diff_calls == [], h.diff_calls)
check("Escape closes the window", win4.get_visible() is False)

# --- Closing via the titlebar/Alt-F4/destroyed-transient-parent path ---
# (fix-round review, Important 5): a bare Gtk.Window's 'close-request'
# fires on win.close() without going through Cancel/Compare/Escape at
# all — verified separately that win.close() raises the same
# 'close-request' signal a real titlebar click does. This is the
# specific path a nested GLib.MainLoop-based dialog would have frozen
# on (no default close-request handler existed), so it gets its own
# dedicated check rather than being assumed covered by the Escape test.
win5 = capture_window(lambda: h._on_compare_tabs())
h.diff_calls.clear()
win5.close()
check("closing the window (titlebar/Alt-F4 path) does not call _show_diff",
      h.diff_calls == [], h.diff_calls)
check("closing the window actually closes it", win5.get_visible() is False)
# A second close() (as a real second Alt-F4 might do) must not double-fire.
win5.close()
check("closing twice does not call _show_diff a second time",
      h.diff_calls == [], h.diff_calls)


# =====================================================================
# _show_diff — plain Gtk.Window (was already Gtk.Window in GTK3, so no
# new Escape wiring is expected — none existed before).
# =====================================================================

h2 = DiffHost()

# Identical content: shows an info dialog, no window opens.
tab_x = make_tab('/same.txt', 'a\nb\nc')
tab_y = make_tab('/same2.txt', 'a\nb\nc')
win = capture_window(lambda: h2._show_diff(tab_x, tab_y))
check("identical tabs: no diff window built", win is None)
check("identical tabs: info shown",
      h2.infos and h2.infos[-1][0] == "Compare" and "identical" in h2.infos[-1][1],
      h2.infos)

# Differing content: a.txt = [a,b,c], b.txt = [a,x,c] -> equal, replace, equal.
tab_a = make_tab('/a.txt', 'a\nb\nc')
tab_b = make_tab('/b.txt', 'a\nx\nc')
win = capture_window(lambda: h2._show_diff(tab_a, tab_b))
check("diff window created", win is not None)
if win is not None:
    press_escape(win)
    check("diff window has no built-in Escape-to-close behavior (GTK3's "
          "original was already a plain Gtk.Window, not Gtk.Dialog, so "
          "none was added here)", win.get_visible() is True)
    check("diff window title", win.get_title() == "Diff: a.txt vs b.txt",
          win.get_title())

    text_views = find_all(win, Gtk.TextView)
    check("two text views (left/right panes)", len(text_views) == 2, len(text_views))
    left_view, right_view = text_views
    left_text = left_view.get_buffer().get_text(
        left_view.get_buffer().get_start_iter(),
        left_view.get_buffer().get_end_iter(), True)
    right_text = right_view.get_buffer().get_text(
        right_view.get_buffer().get_start_iter(),
        right_view.get_buffer().get_end_iter(), True)
    check("left pane shows a's lines", "a" in left_text and "b" in left_text
          and "c" in left_text, left_text)
    check("right pane shows b's lines", "a" in right_text and "x" in right_text
          and "c" in right_text, right_text)

    # Minimap: 3 diff rows -> 3 boxes, only the middle ('replace') carries
    # a color class; the CSS-class approach replaces the old per-EventBox
    # override_background_color() call (removed in GTK4).
    boxes_20x2 = [b for b in find_all(win, Gtk.Box) if b.get_size_request()[0] == 20]
    check("minimap has 3 row boxes (one per diff row)", len(boxes_20x2) == 3,
          len(boxes_20x2))
    # Gtk.Box always carries a default 'horizontal'/'vertical' CSS class,
    # so filter for our own synpad-diff-* classes specifically.
    colored = [b for b in boxes_20x2
               if any(c.startswith('synpad-diff-') for c in b.get_css_classes())]
    check("exactly 1 colored minimap box (the 'replace' row)", len(colored) == 1,
          [b.get_css_classes() for b in boxes_20x2])
    if colored:
        check("colored box uses the replace CSS class",
              'synpad-diff-replace' in colored[0].get_css_classes(),
              colored[0].get_css_classes())
        gestures = [c for c in colored[0].observe_controllers()
                    if isinstance(c, Gtk.GestureClick)]
        check("colored minimap box has a click gesture", len(gestures) >= 1)
        if gestures:
            # MINOR 4 (final review): a bare Gtk.GestureClick defaults to
            # button 1 only. The old GTK3 handler had no button guard at
            # all (any button scrolled the minimap), so set_button(0) is
            # required to match — without it, a middle/right click would
            # silently do nothing.
            check("minimap gesture is configured for any button (set_button(0))",
                  gestures[0].get_button() == 0, gestures[0].get_button())
            try:
                gestures[0].emit('pressed', 1, 5.0, 1.0)
                click_ok = True
            except Exception:
                click_ok = False
            check("clicking the minimap box doesn't raise", click_ok)

    # Nav bar: prev/next change buttons update the change label.
    change_labels = [l for l in find_all(win, Gtk.Label)
                      if l.get_text().startswith("Change ") or l.get_text() == "No changes"]
    check("change-count label present", len(change_labels) == 1, change_labels)
    if change_labels:
        check("initial label shows 1 of 1 change",
              change_labels[0].get_text() == "Change 1 of 1", change_labels[0].get_text())

    nav_buttons = [b for b in find_all(win, Gtk.Button) if b.get_icon_name()]
    check("2 icon-only nav buttons (prev/next change)", len(nav_buttons) == 2,
          [b.get_icon_name() for b in nav_buttons])
    icon_names = sorted(b.get_icon_name() for b in nav_buttons)
    check("nav icons are go-up/go-down symbolic (was set_image+IconSize)",
          icon_names == ['go-down-symbolic', 'go-up-symbolic'], icon_names)
    for b in nav_buttons:
        check(f"nav button '{b.get_icon_name()}' is flat (was set_relief(NONE))",
              'flat' in b.get_css_classes(), b.get_css_classes())


# =====================================================================
# _show_conflict_diff — plain Gtk.Window with action buttons
# =====================================================================

class FakeMgr:
    def __init__(self):
        self.uploads = []
    def upload(self, remote_path, local_path, max_mb):
        self.uploads.append((remote_path, local_path, max_mb))
    def get_remote_mtime(self, remote_path): return 12345
    def get_remote_size(self, remote_path): return 42


import tempfile
tmpdir = tempfile.mkdtemp()
local_file = os.path.join(tmpdir, 'conflict.txt')
with open(local_file, 'w') as f:
    f.write('mine\n')

h3 = DiffHost()
tab_c = OpenTab('/remote/conflict.txt', local_file, None, Gtk.TextBuffer(),
                is_local=False, server_guid='g1')
tab_c.buffer.set_text('mine\n')
mgr = FakeMgr()

win = capture_window(lambda: h3._show_conflict_diff(
    tab_c, 'mine\n', 'theirs\n', 3, 5, mgr))
check("conflict window created", win is not None)
if win is not None:
    check("conflict window title",
          win.get_title() == "Conflict: conflict.txt — My Changes vs Server",
          win.get_title())

    btns = labeled_buttons(win)
    labels = [b.get_label() for b in btns]
    check("all three action buttons present",
          set(labels) == {"Use My Changes (Overwrite Server)",
                           "Use Server Version", "Cancel"}, labels)
    # GTK3 mixed pack_start(mine, server) with pack_end(cancel) on one row
    # -> visual [Use My Changes, Use Server Version, ..., Cancel]. find_all
    # walks in append order, which (via the nested end-aligned sub-box for
    # Cancel) matches that same visual left-to-right order.
    mine_i = labels.index("Use My Changes (Overwrite Server)")
    server_i = labels.index("Use Server Version")
    cancel_i = labels.index("Cancel")
    check("visual order is [Use My Changes, Use Server Version, ..., Cancel]",
          mine_i < server_i < cancel_i, labels)
    by_label = {b.get_label(): b for b in btns}
    check("'Use My Changes' is destructive-styled",
          'destructive-action' in by_label["Use My Changes (Overwrite Server)"].get_css_classes())

    # --- Cancel ---
    h3.status.clear()
    by_label["Cancel"].emit('clicked')
    check("Cancel sets status", h3.status == ["Upload cancelled"], h3.status)
    check("Cancel closes the window", win.get_visible() is False)

    # --- Use Server Version ---
    win2 = capture_window(lambda: h3._show_conflict_diff(
        tab_c, 'mine\n', 'theirs\n', 3, 5, mgr))
    by_label2 = {b.get_label(): b for b in labeled_buttons(win2)}
    h3.status.clear()
    by_label2["Use Server Version"].emit('clicked')
    buf_text = tab_c.buffer.get_text(tab_c.buffer.get_start_iter(),
                                      tab_c.buffer.get_end_iter(), True)
    check("Use Server Version overwrites the tab buffer", buf_text == 'theirs\n', buf_text)
    check("Use Server Version sets status",
          h3.status and 'Loaded server version' in h3.status[-1], h3.status)
    with open(local_file) as f:
        on_disk = f.read()
    check("Use Server Version writes the local file", on_disk == 'theirs\n', on_disk)
    check("Use Server Version closes the window", win2.get_visible() is False)

    # --- Use My Changes (Overwrite Server): run the background upload
    # synchronously by faking threading.Thread, so this exercises the full
    # callback chain instead of only the pre-thread side effects. ---
    import threading as _threading_mod
    class SyncThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
        def start(self):
            self._target()
    _orig_thread = compare.threading.Thread
    _orig_idle_add = compare.GLib.idle_add
    compare.threading.Thread = SyncThread
    compare.GLib.idle_add = lambda fn, *a: fn(*a)
    try:
        tab_c.buffer.set_text('mine\n')
        win3 = capture_window(lambda: h3._show_conflict_diff(
            tab_c, 'mine\n', 'theirs\n', 3, 5, mgr))
        by_label3 = {b.get_label(): b for b in labeled_buttons(win3)}
        h3.status.clear()
        by_label3["Use My Changes (Overwrite Server)"].emit('clicked')
        check("Use My Changes closes the window immediately", win3.get_visible() is False)
        check("Use My Changes disables item_save",
              h3.item_save.sensitive and h3.item_save.sensitive[-1] is False,
              h3.item_save.sensitive)
        check("Use My Changes uploads the local file to the remote path",
              mgr.uploads and mgr.uploads[-1] == ('/remote/conflict.txt', local_file, 5),
              mgr.uploads)
        check("Use My Changes reports completion via _on_upload_done",
              len(h3.upload_done) == 1 and h3.upload_done[0] == (tab_c, 3),
              h3.upload_done)
    finally:
        compare.threading.Thread = _orig_thread
        compare.GLib.idle_add = _orig_idle_add

import shutil
shutil.rmtree(tmpdir, ignore_errors=True)


print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
