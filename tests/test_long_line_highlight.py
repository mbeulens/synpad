"""Tests for the long-line syntax-highlighting guard.

The bug: opening a file that contains a very long line (minified JS/CSS, or a
single-line JSON blob such as a Postman collection export) froze SynPad for
around a minute. GtkSourceView 3.x highlights and lays out text per line at
roughly O(n^2) in line length, and _create_editor_tab enabled highlighting
unconditionally. Measured on a 453 KB Postman collection whose longest line is
349,924 chars:

    highlight on   59.5 s   blocked in the GTK main loop
    highlight off   0.23 s

Same 455 KB of JSON chopped into 200-char lines highlights in 0.56 s, so the
cost is driven by line length, not file size. Cost vs. longest line:

    5k 0.06s | 10k 0.11s | 20k 0.76s | 40k 2.0s | 80k 6.5s | 160k 22s | 350k 86s

The fix skips highlighting for buffers whose longest line exceeds
MAX_HIGHLIGHT_LINE_LEN, keeping open times in the sub-second range.

Runnable directly (python3 tests/test_long_line_highlight.py) or via pytest.
The pure helpers need no display; the buffer test uses GtkSource headlessly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import GtkSource

from config import MAX_HIGHLIGHT_LINE_LEN
from editor import max_line_length, syntax_highlight_allowed, apply_syntax_highlighting


def test_max_line_length_reports_longest_line():
    assert max_line_length('') == 0
    assert max_line_length('abc') == 3
    assert max_line_length('a\nbbbb\ncc') == 4
    # Trailing newline must not be counted as a line of its own length.
    assert max_line_length('short\n' + 'x' * 500) == 500


def test_normal_source_is_highlighted():
    content = '\n'.join(['def f():', '    return 1'] * 500)
    assert syntax_highlight_allowed(content) is True


def test_big_file_of_short_lines_is_still_highlighted():
    """Size alone must not disable highlighting — only line length costs us."""
    content = '\n'.join('{"key": "value", "n": %d},' % i for i in range(20000))
    assert len(content) > 400_000
    assert syntax_highlight_allowed(content) is True


def test_single_long_line_disables_highlighting():
    content = '{"description": "%s"}' % ('x' * 349_924)
    assert syntax_highlight_allowed(content) is False


def test_threshold_boundary():
    assert syntax_highlight_allowed('a' * MAX_HIGHLIGHT_LINE_LEN) is True
    assert syntax_highlight_allowed('a' * (MAX_HIGHLIGHT_LINE_LEN + 1)) is False


def test_long_line_buried_in_an_otherwise_normal_file_disables_highlighting():
    """The Postman-collection shape: mostly short lines, one enormous one."""
    lines = ['{'] + ['  "a": 1,'] * 2000
    lines.insert(1146, '  "blob": "%s"' % ('y' * 200_000))
    assert syntax_highlight_allowed('\n'.join(lines)) is False


def test_apply_syntax_highlighting_turns_the_buffer_flag_off():
    lang = GtkSource.LanguageManager.get_default().get_language('json')
    assert lang is not None, 'json language spec missing — cannot test'

    buf = GtkSource.Buffer()
    enabled = apply_syntax_highlighting(buf, lang, '{"a": "%s"}' % ('x' * 200_000))
    assert enabled is False
    assert buf.get_highlight_syntax() is False
    # The language is still attached, so re-enabling later needs no relookup.
    assert buf.get_language() is lang


def test_apply_syntax_highlighting_leaves_normal_files_highlighted():
    lang = GtkSource.LanguageManager.get_default().get_language('json')
    buf = GtkSource.Buffer()
    enabled = apply_syntax_highlighting(buf, lang, '{"a": 1}')
    assert enabled is True
    assert buf.get_highlight_syntax() is True


def test_apply_syntax_highlighting_with_no_language():
    """Unknown file types have no language; highlighting stays off, no crash."""
    buf = GtkSource.Buffer()
    assert apply_syntax_highlighting(buf, None, 'plain text') is False
    assert buf.get_highlight_syntax() is False


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except Exception as exc:
                failures += 1
                print(f'FAIL {name}: {exc.__class__.__name__}: {exc}')
    total = sum(1 for n in globals() if n.startswith('test_'))
    print(f'\n{total - failures}/{total} passed')
    sys.exit(1 if failures else 0)
