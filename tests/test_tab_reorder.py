"""Tests for tab-reorder bookkeeping.

The bug: tabs are made reorderable (set_tab_reorderable) but self.tabs is a
dict keyed by notebook page index. Nothing re-syncs that dict when the user
drags a tab to a new position, so after a reorder self.tabs maps stale indices
to tabs. Save/close then resolve the wrong tab (or none) by index — the tab
"loses its reference" and can no longer be saved or closed.

The fix wires the notebook's 'page-reordered' signal to _reindex_tabs(), which
already rebuilds self.tabs from the live page widgets by identity.

Runnable directly (python3 tests/test_tab_reorder.py) or via pytest.
Uses a real Gtk.Notebook headlessly; no display server required.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from editor import EditorMixin


class FakeTab:
    """Minimal stand-in for OpenTab: _reindex_tabs only reads source_view."""

    def __init__(self, name, source_view):
        self.remote_path = name
        self.source_view = source_view


class Harness:
    """A bare object wired with the real EditorMixin tab-reorder machinery and
    a real Gtk.Notebook, so a genuine reorder drives the production code path."""

    _reindex_tabs = EditorMixin._reindex_tabs
    _setup_tab_reordering = EditorMixin._setup_tab_reordering

    def __init__(self):
        self.notebook = Gtk.Notebook()
        self.tabs = {}

    def _debug(self, *_args, **_kwargs):
        pass

    def add(self, name):
        scroll = Gtk.ScrolledWindow()
        view = Gtk.TextView()
        scroll.add(view)
        page_num = self.notebook.append_page(scroll, Gtk.Label(label=name))
        self.notebook.set_tab_reorderable(scroll, True)
        self.tabs[page_num] = FakeTab(name, view)
        return scroll


def _current_names(h):
    return {idx: tab.remote_path for idx, tab in h.tabs.items()}


def test_tabs_reindexed_after_reorder():
    h = Harness()
    h._setup_tab_reordering()
    a = h.add('a')
    h.add('b')
    assert _current_names(h) == {0: 'a', 1: 'b'}

    # Drag tab 'a' to the end — GTK renumbers pages; self.tabs must follow.
    h.notebook.reorder_child(a, 1)

    assert h.notebook.get_nth_page(0) is not a
    assert h.notebook.get_nth_page(1) is a
    assert _current_names(h) == {0: 'b', 1: 'a'}, (
        f"self.tabs went stale after reorder: {_current_names(h)}"
    )


if __name__ == '__main__':
    test_tabs_reindexed_after_reorder()
    print("OK")
