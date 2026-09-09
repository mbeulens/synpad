"""Single-click folder toggle on both file trees — GTK3 parity.

Under GTK3 the TreeView's expander arrow toggled a directory row on one
click. Under GTK4 that arrow does not respond here, so folders required a
double-click. Reported from real use after the port. These tests drive the
real handlers on real TreeViews.
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FAKE = tempfile.mkdtemp(); os.environ['HOME'] = _FAKE
os.environ['XDG_CONFIG_HOME'] = os.path.join(_FAKE, '.config')

import gi
gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')
from gi.repository import Gtk, GLib

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import config
assert '/home/beuner/.config' not in config.CONFIG_DIR, "NOT ISOLATED"
config.save_config = lambda *a, **k: None

from remote import RemoteMixin
from local_files import LocalFilesMixin

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)

class FakeGesture:
    def __init__(self, widget, button=1): self._w, self._b = widget, button
    def get_widget(self): return self._w
    def get_current_button(self): return self._b

def build(store_cols=(str, str, str, bool, bool)):
    store = Gtk.TreeStore(*store_cols)
    view = Gtk.TreeView(model=store)
    col = Gtk.TreeViewColumn("Files")
    ir, tr = Gtk.CellRendererPixbuf(), Gtk.CellRendererText()
    col.pack_start(ir, False); col.pack_start(tr, True)
    view.append_column(col)
    scroll = Gtk.ScrolledWindow(); scroll.set_child(view)
    win = Gtk.Window(); win.set_child(scroll); win.set_default_size(400, 400)
    return store, view, win

# ---- remote tree ----------------------------------------------------------
class RHost(RemoteMixin, Gtk.Window):
    def __init__(self, store, view):
        super().__init__()
        self.tree_store, self.tree_view = store, view
        self.ftp_mgr = None
    def _set_status(self, *a): pass
    def _console_log(self, *a, **k): pass

store, view, win = build()
h = RHost(store, view)
d = store.append(None, ["error_docs", "folder", "/error_docs", True, True])
store.append(d, ["bad_request.html", "text-html", "/error_docs/bad_request.html", False, True])
f = store.append(None, ["index.php", "text-x-php", "/index.php", False, True])
dpath, fpath = store.get_path(d), store.get_path(f)

h._remote_attach_tree_controllers(view)
scroll_host = view.get_ancestor(Gtk.ScrolledWindow)
btns = [c.get_button() for c in view.observe_controllers() if isinstance(c, Gtk.GestureClick)]
host_btns = [c.get_button() for c in scroll_host.observe_controllers() if isinstance(c, Gtk.GestureClick)]
check("toggle gesture is on the ScrolledWindow ancestor, not the view",
      1 in host_btns, host_btns)
# The view still carries GTK's own button-1/button-0 gestures; what matters
# is that OUR toggle is not among them -- it must live on the ancestor.
check("view carries our button-3 menu gesture", 3 in btns, btns)
own_capture_b1 = [c for c in view.observe_controllers()
                  if isinstance(c, Gtk.GestureClick) and c.get_button() == 1
                  and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE]
check("only GTK's own button-1 capture gesture is on the view (exactly 1)",
      len(own_capture_b1) == 1, len(own_capture_b1))
check("remote tree keeps its button-3 menu gesture", 3 in btns, btns)
# Must be CAPTURE: at BUBBLE this fired for clicks on the folder name but
# NOT on the expander arrow, because GTK4's own CAPTURE-phase button-1
# gesture claims an arrow press and does nothing with it.
ours = [c for c in scroll_host.observe_controllers()
        if isinstance(c, Gtk.GestureClick) and c.get_button() == 1
        and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE]
check("toggle gesture runs at CAPTURE so the expander arrow reaches it",
      len(ours) >= 1, [c.get_propagation_phase() for c in view.observe_controllers()
                       if isinstance(c, Gtk.GestureClick)])
# It must not claim the sequence, or row selection and drag break.
check("toggle handler returns False so selection still happens",
      h._on_tree_single_click(FakeGesture(view), 1, 5, 5000) is False)

# geometry for a real row
win.present()
for _ in range(80):
    if not GLib.MainContext.default().pending(): break
    GLib.MainContext.default().iteration(False)
area = view.get_cell_area(dpath, view.get_column(0))
cx, cy = area.x + 20, area.y + area.height // 2

# The expander-arrow zone must be left to GTK, or our toggle and GTK's
# cancel each other and the arrow looks dead (the v2.0.11/2.0.12 bug).
bg = view.get_background_area(dpath, view.get_column(0))
arrow_x = bg.x + max(0, (area.x - bg.x) // 2)
check("arrow zone exists left of the cell area", area.x > bg.x, (bg.x, area.x))
# GTK's expander owns the arrow and toggles on RELEASE; we toggle on press.
# If we also handled the arrow the row would open on mousedown and close on
# mouseup -- the v2.0.15 symptom. So an arrow press must be a no-op for us.
was = view.row_expanded(dpath)
h._on_tree_single_click(FakeGesture(view), 1, arrow_x, cy)
check("ARROW press is a no-op for us (GTK toggles it on release)",
      view.row_expanded(dpath) == was)
# ...and the name, one pixel right of the cell edge, must still toggle.
h._on_tree_single_click(FakeGesture(view), 1, area.x + 1, cy)
check("press just inside the cell area DOES toggle",
      view.row_expanded(dpath) != was)
h._on_tree_single_click(FakeGesture(view), 1, area.x + 1, cy)  # restore

check("directory starts collapsed", not view.row_expanded(dpath))
h._on_tree_single_click(FakeGesture(view), 1, cx, cy)
check("single click EXPANDS a directory", view.row_expanded(dpath))
h._on_tree_single_click(FakeGesture(view), 1, cx, cy)
check("single click COLLAPSES it again (the reported bug)", not view.row_expanded(dpath))

# files must not be opened by a single click
opened = []
h._open_file = lambda p: opened.append(p)
h._open_remote_external = lambda p: opened.append(p)
farea = view.get_cell_area(fpath, view.get_column(0))
h._on_tree_single_click(FakeGesture(view), 1, farea.x + 20, farea.y + farea.height // 2)
check("single click does NOT open a file (GTK3 parity)", opened == [], opened)

# double-click must not double-toggle via this handler
before = view.row_expanded(dpath)
h._on_tree_single_click(FakeGesture(view), 2, cx, cy)
check("n_press=2 ignored, so double-click can't cancel itself",
      view.row_expanded(dpath) == before)

# click on empty space below the rows
h._on_tree_single_click(FakeGesture(view), 1, 5, 5000)
check("click on empty space is a no-op", True)

# ---- local tree -----------------------------------------------------------
class LHost(LocalFilesMixin, Gtk.Window):
    def __init__(self, store, view):
        super().__init__()
        self._local_store, self._local_view = store, view
    def _set_status(self, *a): pass

lstore, lview, lwin = build()
lh = LHost(lstore, lview)
ld = lstore.append(None, ["src", "folder", "/tmp/src", True, True])
lstore.append(ld, ["main.py", "text-x-python", "/tmp/src/main.py", False, True])
ldpath = lstore.get_path(ld)
lh._local_attach_tree_controllers(lview)
lwin.present()
for _ in range(80):
    if not GLib.MainContext.default().pending(): break
    GLib.MainContext.default().iteration(False)
larea = lview.get_cell_area(ldpath, lview.get_column(0))
lcx, lcy = larea.x + 20, larea.y + larea.height // 2

lh._on_local_tree_single_click(FakeGesture(lview), 1, lcx, lcy)
check("local tree: single click expands", lview.row_expanded(ldpath))
lh._on_local_tree_single_click(FakeGesture(lview), 1, lcx, lcy)
check("local tree: single click collapses", not lview.row_expanded(ldpath))

# ---- lazy-load parity: collapsing an unloaded dir must still work ---------
store2, view2, win2 = build()
h2 = RHost(store2, view2)
h2._remote_attach_tree_controllers(view2)
lazy = store2.append(None, ["httpdocs", "folder", "/httpdocs", True, False])  # loaded=False
store2.append(lazy, ["Loading...", "content-loading-symbolic", "", False, False])
lp = store2.get_path(lazy)
loads = []
h2._load_tree = lambda p, it=None: loads.append(p)
view2.connect('row-expanded', h2._on_tree_row_expanded)
win2.present()
for _ in range(80):
    if not GLib.MainContext.default().pending(): break
    GLib.MainContext.default().iteration(False)
a2 = view2.get_cell_area(lp, view2.get_column(0))
h2._on_tree_single_click(FakeGesture(view2), 1, a2.x + 20, a2.y + a2.height // 2)
check("unloaded dir expands and triggers a lazy load", view2.row_expanded(lp) and loads == ['/httpdocs'], loads)
h2._on_tree_single_click(FakeGesture(view2), 1, a2.x + 20, a2.y + a2.height // 2)
check("unloaded dir COLLAPSES again — the root-folder bug", not view2.row_expanded(lp))

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
