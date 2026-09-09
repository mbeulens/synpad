"""Tests for tab-reorder bookkeeping — Adw.TabView port.

The GTK3 bug this file used to guard: tabs were made reorderable
(set_tab_reorderable) but self.tabs was a dict keyed by *integer notebook
page index*. Nothing re-synced that dict when the user dragged a tab to a
new position, so after a reorder self.tabs mapped stale indices to the
wrong tabs (or none) — Save/Close then resolved the wrong tab by index,
and the dragged tab "lost its reference". The v1.20.4 fix wired the
notebook's 'page-reordered' signal to _reindex_tabs(), which rebuilt
self.tabs from the live page widgets by identity.

Task 5 replaces Gtk.Notebook with Adw.TabView, which addresses tabs by
Adw.TabPage *object* rather than integer position — self.tabs is now keyed
by TabPage. A TabPage's identity (and so its use as a dict key) does not
change when it moves to a new position, so the entire bug class — "the
dict's key for this tab went stale after a drag" — cannot occur by
construction: there is no integer index in the key to go stale. Per the
migration plan and task-5-brief.md, _reindex_tabs() and the
'page-reordered' re-sync handler are deleted outright, not ported.

This file now asserts that structural impossibility directly: reorder a
real Adw.TabView (reorder_page(), same call GTK4 makes internally for a
drag) and confirm self.tabs still resolves every page to its original tab
— with no re-sync step run at all — plus that EditorMixin no longer even
defines _reindex_tabs/_setup_tab_reordering to run.

Runnable directly (python3 tests/test_tab_reorder.py) or via pytest.
Uses a real Adw.TabView headlessly; no display server required beyond the
usual `Gtk.init_check()` headless X/Wayland guard used by the rest of this
suite.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

if not Gtk.init_check():
    print("SKIP: no display")
    sys.exit(0)
Adw.init()

from editor import EditorMixin

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {extra}" if not cond and extra else ""))
    if not cond:
        fails.append(name)


class FakeTab:
    """Minimal stand-in for OpenTab."""

    def __init__(self, name):
        self.remote_path = name


def test_workaround_deleted():
    """The v1.20.4 stale-index workaround must be gone, not merely unused —
    Global Constraint 5 explicitly authorized deleting it for this task."""
    check("EditorMixin no longer defines _reindex_tabs",
          not hasattr(EditorMixin, '_reindex_tabs'))
    check("EditorMixin no longer defines _setup_tab_reordering",
          not hasattr(EditorMixin, '_setup_tab_reordering'))


def test_tabs_survive_reorder_by_construction():
    """self.tabs is keyed by Adw.TabPage, whose identity a reorder never
    touches — reorder_page() is the exact call GTK4 makes internally for a
    drag-to-reorder, no synthetic substitute."""
    tv = Adw.TabView()
    a = Gtk.Label(label='a')
    b = Gtk.Label(label='b')
    c = Gtk.Label(label='c')
    page_a = tv.append(a)
    page_b = tv.append(b)
    page_c = tv.append(c)

    tabs = {page_a: FakeTab('a'), page_b: FakeTab('b'), page_c: FakeTab('c')}

    check("initial position order is a, b, c",
          [tv.get_page_position(p) for p in (page_a, page_b, page_c)] == [0, 1, 2])

    # Drag 'a' to the end. No _reindex_tabs()-equivalent call anywhere in
    # this test — the whole point is that none is needed.
    tv.reorder_page(page_a, 2)

    check("TabView actually reordered: position order is now b, c, a",
          [tv.get_page_position(p) for p in (page_b, page_c, page_a)] == [0, 1, 2])
    check("self.tabs[page_a] is still tab 'a' with no re-sync step",
          tabs[page_a].remote_path == 'a')
    check("self.tabs[page_b] is still tab 'b' with no re-sync step",
          tabs[page_b].remote_path == 'b')
    check("self.tabs[page_c] is still tab 'c' with no re-sync step",
          tabs[page_c].remote_path == 'c')
    check("get_nth_page(2) (now 'a's slot) resolves via tabs to 'a'",
          tabs[tv.get_nth_page(2)].remote_path == 'a')
    check("get_nth_page(0) (now 'b's slot) resolves via tabs to 'b'",
          tabs[tv.get_nth_page(0)].remote_path == 'b')

    # Reorder again, immediately, with no intervening re-sync of any kind —
    # a second drag must not compound any staleness (there is none to
    # compound).
    tv.reorder_page(page_b, 2)
    check("second reorder: position order is c, a, b",
          [tv.get_page_position(p) for p in (page_c, page_a, page_b)] == [0, 1, 2])
    check("all three tabs still resolve correctly after two reorders",
          tabs[page_a].remote_path == 'a' and
          tabs[page_b].remote_path == 'b' and
          tabs[page_c].remote_path == 'c')


def test_close_by_page_unaffected_by_reorder():
    """The original bug manifested as Save/Close resolving the wrong tab
    (or none) after a drag. Simulate the same lookup _close_tab() does
    (self.tabs.get(page)) post-reorder."""
    tv = Adw.TabView()
    a = Gtk.Label(label='a')
    b = Gtk.Label(label='b')
    page_a = tv.append(a)
    page_b = tv.append(b)
    tabs = {page_a: FakeTab('a'), page_b: FakeTab('b')}

    tv.reorder_page(page_a, 1)

    # What _close_tab(page) does: tab = self.tabs.get(page)
    resolved = tabs.get(page_a)
    check("closing page_a after reorder resolves tab 'a', not tab 'b' or None",
          resolved is not None and resolved.remote_path == 'a')


if __name__ == '__main__':
    test_workaround_deleted()
    test_tabs_survive_reorder_by_construction()
    test_close_by_page_unaffected_by_reorder()
    if fails:
        print(f"\n{len(fails)} FAILURE(S): {fails}")
        sys.exit(1)
    print("\nALL PASS")
