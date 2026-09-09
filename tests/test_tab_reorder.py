"""Tests for tab-reorder bookkeeping — Adw.TabView port.

The GTK3 bug this file exists to guard: tabs were made reorderable
(set_tab_reorderable) but self.tabs was a dict keyed by *integer notebook
page index*. Nothing re-synced that dict when the user dragged a tab to a
new position, so after a reorder self.tabs mapped stale indices to the
wrong tabs (or none) — Save/Close then resolved the wrong tab by index,
and the dragged tab "lost its reference". The v1.20.4 fix wired the
notebook's 'page-reordered' signal to _reindex_tabs(), which rebuilt
self.tabs from the live page widgets by identity.

Task 5 replaces Gtk.Notebook with Adw.TabView, which addresses tabs by
Adw.TabPage *object* rather than integer position — self.tabs is now keyed
by TabPage, a TabPage's identity doesn't change when it moves to a new
position, and _reindex_tabs()/'page-reordered' are deleted outright, not
ported.

Fix round 1 (I-3): the first version of this rewrite built its own local
`{TabPage: FakeTab}` dict and reordered *that* — it asserted properties of
Python dicts and libadwaita, not of SynPad, and its only contact with
production code was two `hasattr` checks. Two real, literal
reintroductions of the v1.20.4 bug shape left it green: making
`_close_tab` close `self.notebook.get_nth_page(0)` instead of the `page`
it was given, and re-keying `self.tabs` by `self.notebook.
get_page_position(page)` instead of by `page` itself. This version drives
the *real* `EditorMixin` (`_create_editor_tab`, `_close_tab`,
`_update_tab_label`) against a real `Adw.TabView`, reorders it with
`reorder_page()` — the exact call GTK4 makes internally for a drag — and
then resolves tabs strictly through `self.tabs.get(page)`/`_close_tab
(page)`/`_update_tab_label(tab, name)` the way production code does, so
that both of the named mutations (and any other position-vs-identity
confusion) show up as a wrong tab closed/renamed or a lookup miss, not as
a property of a test-local dict.

Runnable directly (python3 tests/test_tab_reorder.py) or via pytest.
Uses a real Adw.TabView headlessly; no display server required beyond the
usual `Gtk.init_check()` headless X/Wayland guard used by the rest of this
suite.

Never touches the real ~/.config/synpad/config.json or the OS keyring —
editor.py calls save_config() from code paths not exercised here (no
dialog/upload/settings save is driven by this file), but save_config is
monkeypatched anyway, matching every other suite's convention.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')  # needed before `import editor` below
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

if not Gtk.init_check():
    print("SKIP: no display")
    sys.exit(0)
Adw.init()

import editor
from editor import EditorMixin

editor.save_config = lambda cfg: None

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {extra}" if not cond and extra else ""))
    if not cond:
        fails.append(name)


class FakeWidget:
    def set_sensitive(self, v):
        pass


class FakeHeader:
    def set_subtitle(self, s):
        pass


class Host(EditorMixin, Gtk.Window):
    """A real Gtk.Window subclass hosting the real EditorMixin tab-
    management code against a real Adw.TabView — same shape as test_
    editor_gtk4.py's Host (close-page/setup-menu wired here exactly as
    window.py's _connect_signals() does in production, since this
    isolated harness has no window.py of its own)."""

    def __init__(self):
        Gtk.Window.__init__(self)
        self.config = {}
        self.tabs = {}
        self.ftp_mgr = None
        self.current_server_guid = ''
        self.notebook = Adw.TabView()
        self.notebook.connect('close-page', self._on_tab_close_page)
        self.notebook.connect('setup-menu', self._on_tab_setup_menu)
        self._pending_close_callbacks = {}
        self.set_child(self.notebook)
        self.item_save = FakeWidget()
        self.header = FakeHeader()
        self._search_window = None
        self.status = []
        self.console = []

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): pass
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _debug(self, msg): pass
    def _get_scheme(self): return None
    def _get_file_ext(self, filepath):
        return filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
    def _update_symbols(self, tab): pass
    def _sighelp_attach(self, view, buf): pass
    def _claude_handle_trigger(self, preset_key=None): pass


def make_tab(h, name, content='x'):
    h._create_editor_tab(name, name, content, is_local=True)
    page = h.notebook.get_selected_page()
    return page, h.tabs[page]


def test_workaround_deleted():
    """The v1.20.4 stale-index workaround must be gone, not merely unused —
    Global Constraint 5 explicitly authorized deleting it for this task."""
    check("EditorMixin no longer defines _reindex_tabs",
          not hasattr(EditorMixin, '_reindex_tabs'))
    check("EditorMixin no longer defines _setup_tab_reordering",
          not hasattr(EditorMixin, '_setup_tab_reordering'))


def test_close_by_page_survives_real_reorder():
    """Drives the real _create_editor_tab/_close_tab against a real
    Adw.TabView. Reorders with reorder_page() (the exact call GTK4 makes
    internally for a drag), then closes by TabPage reference — this is
    the scenario that would fail if _close_tab regressed to closing
    get_nth_page(0) (a stand-in for "whatever the old integer page_num
    happened to be") instead of the page it was actually given."""
    h = Host()
    page_a, tab_a = make_tab(h, 'a.txt', 'A')
    page_b, tab_b = make_tab(h, 'b.txt', 'B')
    page_c, tab_c = make_tab(h, 'c.txt', 'C')

    check("three real tabs created", len(h.tabs) == 3)
    check("initial position order is a, b, c",
          [h.notebook.get_page_position(p) for p in (page_a, page_b, page_c)] == [0, 1, 2])

    # Drag 'a' from position 0 to position 2 — b and c shift down to fill
    # the gap, so position 0 now holds 'b', not 'a'.
    h.notebook.reorder_page(page_a, 2)
    check("reorder_page actually moved the tabs: b, c, a",
          [h.notebook.get_page_position(p) for p in (page_b, page_c, page_a)] == [0, 1, 2])

    # Close 'a' by its TabPage reference — real _close_tab, real
    # 'close-page' signal, real _on_tab_close_page. 'a' is unmodified, so
    # this resolves synchronously with no confirm dialog.
    h._close_tab(page_a)

    check("_close_tab(page_a) actually closed 'a', not whatever now sits "
          "at position 0 ('b') — the get_nth_page(0)-regression shape",
          page_a not in h.tabs)
    check("'b' (now at position 0) was NOT closed by mistake",
          page_b in h.tabs and h.tabs[page_b].remote_path == 'b.txt')
    check("'c' was NOT closed by mistake",
          page_c in h.tabs and h.tabs[page_c].remote_path == 'c.txt')
    check("self.tabs.get(page_b) resolves 'b' by TabPage identity, not by "
          "position (a get_page_position(page)-keyed self.tabs would "
          "return None here after a reorder, since real production code "
          "only ever looks up by TabPage, never by int)",
          h.tabs.get(page_b) is tab_b)
    check("self.tabs.get(page_c) resolves 'c' by TabPage identity",
          h.tabs.get(page_c) is tab_c)


def test_close_all_tabs_except_survives_real_reorder():
    """_close_all_tabs_except, driven for real, after a reorder that moves
    the tab being kept away from position 0."""
    h = Host()
    page_a, tab_a = make_tab(h, 'a2.txt', 'A')
    page_b, tab_b = make_tab(h, 'b2.txt', 'B')
    page_c, tab_c = make_tab(h, 'c2.txt', 'C')

    # Move the tab we're about to "keep" (b) away from wherever a fresh/
    # position-based scheme would expect it.
    h.notebook.reorder_page(page_b, 0)
    check("'b' moved to position 0 ahead of the close-all-but-this call",
          h.notebook.get_page_position(page_b) == 0)

    h._close_all_tabs_except(page_b)

    check("kept tab ('b') still open after reorder + close-all-but-this",
          page_b in h.tabs and h.tabs[page_b].remote_path == 'b2.txt')
    check("'a' closed", page_a not in h.tabs)
    check("'c' closed", page_c not in h.tabs)


def test_update_tab_label_addresses_the_given_tab_not_the_selected_one():
    """I-5: _update_tab_label must retitle the TabPage belonging to the
    `tab` argument it's given (via tab.source_view.get_parent() ->
    notebook.get_page(...)), not whatever's currently selected. Covers
    the exact regression shape named in fix round 1: swapping that lookup
    for self.notebook.get_selected_page() left the whole suite green
    because every prior test happened to rename the selected tab."""
    h = Host()
    page_a, tab_a = make_tab(h, 'rename_a.txt', 'A')
    page_b, tab_b = make_tab(h, 'rename_b.txt', 'B')
    page_c, tab_c = make_tab(h, 'rename_c.txt', 'C')

    # Reorder so creation order, position, and "currently selected" all
    # diverge from each other.
    h.notebook.reorder_page(page_a, 2)
    # Select 'c' — deliberately NOT the tab we're about to rename ('b').
    h.notebook.set_selected_page(page_c)
    check("'c' is the selected page, not 'b' (the one we're about to rename)",
          h.notebook.get_selected_page() is page_c)

    h._update_tab_label(tab_b, "b_renamed.txt")

    check("_update_tab_label renamed 'b' (the tab it was given)",
          page_b.get_title() == "b_renamed.txt", page_b.get_title())
    check("_update_tab_label did NOT rename 'c' (the selected tab) instead "
          "— the get_selected_page()-regression shape",
          page_c.get_title() != "b_renamed.txt", page_c.get_title())
    check("'a' and 'c' keep their original titles",
          page_a.get_title() == "rename_a.txt" and page_c.get_title() == "rename_c.txt")


if __name__ == '__main__':
    test_workaround_deleted()
    test_close_by_page_survives_real_reorder()
    test_close_all_tabs_except_survives_real_reorder()
    test_update_tab_label_addresses_the_given_tab_not_the_selected_one()
    if fails:
        print(f"\n{len(fails)} FAILURE(S): {fails}")
        sys.exit(1)
    print("\nALL PASS")
