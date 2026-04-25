"""Git history viewer — runs `git log` against a local or SFTP repo and
renders the result in the Console pane's "Git History" tab."""

import os
import re
import shlex
import subprocess
import threading
import webbrowser

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

GIT_LOG_FORMAT = '%h%x09%ad%x09%an%x09%s'
GIT_LOG_COUNT = 50
GIT_TIMEOUT = 15

_HASH_RE = re.compile(r'^[0-9a-f]{4,40}$')


class GitHistoryMixin:
    """Adds 'Show git history' behavior. Right-click handlers in
    local_files.py and remote.py invoke `_git_show_history_local`
    or `_git_show_history_sftp`."""

    # -- Entry points (called from tree right-click menus) -------------------

    def _git_show_history_local(self, dot_git_path):
        repo_dir = os.path.dirname(dot_git_path) or '/'
        self._git_show_pane(f"Loading git history of {repo_dir}...")
        threading.Thread(
            target=self._git_run_local, args=(repo_dir,), daemon=True
        ).start()

    def _git_show_history_sftp(self, dot_git_remote_path):
        repo_path = os.path.dirname(dot_git_remote_path) or '/'
        self._git_show_pane(f"Loading git history of {repo_path} (remote)...")
        threading.Thread(
            target=self._git_run_sftp, args=(repo_path,), daemon=True
        ).start()

    # -- Workers -------------------------------------------------------------

    def _git_run_local(self, repo_dir):
        try:
            branch = subprocess.run(
                ['git', '-C', repo_dir, 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=GIT_TIMEOUT,
            ).stdout.strip() or '(detached)'
            remote = subprocess.run(
                ['git', '-C', repo_dir, 'remote', 'get-url', 'origin'],
                capture_output=True, text=True, timeout=GIT_TIMEOUT,
            ).stdout.strip()
            res = subprocess.run(
                ['git', '-C', repo_dir, 'log',
                 f'--pretty=format:{GIT_LOG_FORMAT}',
                 '--date=short', f'-{GIT_LOG_COUNT}'],
                capture_output=True, text=True, timeout=GIT_TIMEOUT,
            )
            if res.returncode != 0:
                err = res.stderr.strip() or 'git log failed'
                GLib.idle_add(self._git_render_error, repo_dir, err)
                return
            GLib.idle_add(self._git_render, repo_dir, branch, remote, res.stdout)
        except FileNotFoundError:
            GLib.idle_add(self._git_render_error, repo_dir,
                          "git is not installed on this machine")
        except subprocess.TimeoutExpired:
            GLib.idle_add(self._git_render_error, repo_dir,
                          "git history timed out")
        except Exception as e:
            GLib.idle_add(self._git_render_error, repo_dir,
                          f"{type(e).__name__}: {e}")

    def _git_run_sftp(self, repo_path):
        try:
            transport = self.ftp_mgr.transport
            quoted = shlex.quote(repo_path)
            branch = self._git_exec_remote(
                transport,
                f'cd {quoted} && git rev-parse --abbrev-ref HEAD 2>&1',
            ).strip() or '(detached)'
            remote = self._git_exec_remote(
                transport,
                f'cd {quoted} && git remote get-url origin 2>/dev/null',
            ).strip()
            log_text = self._git_exec_remote(
                transport,
                f'cd {quoted} && git log '
                f'--pretty=format:{GIT_LOG_FORMAT} '
                f'--date=short -{GIT_LOG_COUNT} 2>&1',
            )
            GLib.idle_add(self._git_render, repo_path, branch, remote, log_text)
        except Exception as e:
            GLib.idle_add(self._git_render_error, repo_path,
                          f"{type(e).__name__}: {e}")

    def _git_exec_remote(self, transport, cmd):
        chan = transport.open_session()
        chan.settimeout(GIT_TIMEOUT)
        chan.exec_command(cmd)
        chunks = []
        while True:
            data = chan.recv(8192)
            if not data:
                break
            chunks.append(data)
        chan.close()
        return b''.join(chunks).decode('utf-8', errors='replace')

    # -- Rendering -----------------------------------------------------------

    def _git_show_pane(self, placeholder):
        if not self._console_visible:
            self._on_toggle_console()
        self._console_notebook.set_current_page(1)
        buf = self._git_history_buffer
        buf.set_text('')
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(end, placeholder + '\n', 'timestamp')

    def _git_render(self, repo_path, branch, remote_url, log_text):
        # Cache state for click-to-browser
        self._git_history_state = {
            'repo_path': repo_path,
            'remote_url': remote_url or '',
        }
        buf = self._git_history_buffer
        buf.set_text('')
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(
            end, f"Branch: {branch}    Repo: {repo_path}\n", 'git_header')
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(
            end, f"Last {GIT_LOG_COUNT} commits\n\n", 'git_header')
        log_text = log_text.strip()
        if not log_text:
            end = buf.get_end_iter()
            buf.insert(end, "(no commits)\n")
            return False
        for line in log_text.split('\n'):
            parts = line.split('\t', 3)
            if len(parts) != 4:
                end = buf.get_end_iter()
                buf.insert(end, line + '\n')
                continue
            h, date, author, subject = parts
            end = buf.get_end_iter()
            buf.insert_with_tags_by_name(end, h, 'git_hash')
            end = buf.get_end_iter()
            buf.insert(end, '  ')
            end = buf.get_end_iter()
            buf.insert_with_tags_by_name(end, date, 'git_date')
            end = buf.get_end_iter()
            buf.insert(end, '  ')
            end = buf.get_end_iter()
            buf.insert_with_tags_by_name(end, author, 'git_author')
            end = buf.get_end_iter()
            buf.insert(end, '  ' + subject + '\n')
        return False

    def _git_render_error(self, repo_path, err):
        buf = self._git_history_buffer
        buf.set_text('')
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(
            end, f"Repo: {repo_path}\n\n", 'git_header')
        end = buf.get_end_iter()
        buf.insert_with_tags_by_name(
            end, f"Git history failed:\n{err}\n", 'error')
        return False

    # -- Click on a commit line opens it in the browser --------------------

    def _git_attach_click_handler(self, view):
        """Wire click-to-browser plus hover highlight on the Git History view."""
        view.connect('button-press-event', self._git_on_history_click)
        view.connect('motion-notify-event', self._git_on_history_motion)
        view.connect('leave-notify-event', self._git_on_history_leave)

    def _git_line_hash(self, line_no):
        """Return the commit hash on `line_no` if any, else None."""
        if line_no < 0:
            return None
        buf = self._git_history_buffer
        line_start = buf.get_iter_at_line(line_no)
        line_end = line_start.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()
        text = buf.get_text(line_start, line_end, False)
        parts = text.split()
        if parts and _HASH_RE.match(parts[0]):
            return parts[0]
        return None

    def _git_set_text_cursor(self, view, name):
        win = view.get_window(Gtk.TextWindowType.TEXT)
        if win is None:
            return
        cursor = Gdk.Cursor.new_from_name(view.get_display(), name)
        win.set_cursor(cursor)

    def _git_clear_hover(self):
        state = getattr(self, '_git_history_state', None) or {}
        prev = state.get('hover_line', -1)
        if prev < 0:
            return
        buf = self._git_history_buffer
        ps = buf.get_iter_at_line(prev)
        pe = ps.copy()
        if not pe.ends_line():
            pe.forward_to_line_end()
        buf.remove_tag_by_name('git_hover', ps, pe)
        state['hover_line'] = -1

    def _git_on_history_motion(self, view, event):
        x, y = view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        ok, it = view.get_iter_at_location(x, y)
        line_no = it.get_line() if ok else -1
        h = self._git_line_hash(line_no) if line_no >= 0 else None

        state = getattr(self, '_git_history_state', None)
        if state is None:
            state = {}
            self._git_history_state = state
        prev_line = state.get('hover_line', -1)
        new_line = line_no if h else -1

        if new_line != prev_line:
            buf = self._git_history_buffer
            if prev_line >= 0:
                ps = buf.get_iter_at_line(prev_line)
                pe = ps.copy()
                if not pe.ends_line():
                    pe.forward_to_line_end()
                buf.remove_tag_by_name('git_hover', ps, pe)
            if new_line >= 0:
                ns = buf.get_iter_at_line(new_line)
                ne = ns.copy()
                if not ne.ends_line():
                    ne.forward_to_line_end()
                buf.apply_tag_by_name('git_hover', ns, ne)
            state['hover_line'] = new_line
            self._git_set_text_cursor(view, 'pointer' if h else 'text')
        return False

    def _git_on_history_leave(self, view, _event):
        self._git_clear_hover()
        self._git_set_text_cursor(view, 'text')
        return False

    def _git_on_history_click(self, view, event):
        if event.button != 1 or event.type != event.type.BUTTON_PRESS:
            return False
        x, y = view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        ok, it = view.get_iter_at_location(x, y)
        if not ok:
            return False
        h = self._git_line_hash(it.get_line())
        if not h:
            return False
        state = getattr(self, '_git_history_state', None) or {}
        remote_url = state.get('remote_url', '')
        if not remote_url:
            self._set_status("No remote configured for this repo")
            return False
        base, kind = _parse_remote_url(remote_url)
        if base is None:
            self._set_status(f"Cannot parse remote URL: {remote_url}")
            return False
        url = _commit_url(base, kind, h)
        webbrowser.open(url)
        self._set_status(f"Opening {url}")
        return True


def _parse_remote_url(url):
    """Return (https_base, kind) for a git remote URL, or (None, None)."""
    url = url.strip()
    if url.endswith('.git'):
        url = url[:-4]
    https_base = None
    # SSH form: git@host:user/repo  (also git@host:port/user/repo for some)
    m = re.match(r'^[\w.-]+@([^:]+):(.+)$', url)
    if m:
        host, path = m.groups()
        https_base = f'https://{host}/{path}'
    elif url.startswith(('http://', 'https://')):
        https_base = url
    elif url.startswith('ssh://'):
        # ssh://git@host[:port]/path
        m = re.match(r'^ssh://[\w.-]+@([^/:]+)(?::\d+)?/(.+)$', url)
        if m:
            host, path = m.groups()
            https_base = f'https://{host}/{path}'
    if https_base is None:
        return None, None
    if 'github.com' in https_base:
        kind = 'github'
    elif 'gitlab' in https_base:
        kind = 'gitlab'
    elif 'bitbucket.org' in https_base:
        kind = 'bitbucket'
    else:
        kind = 'unknown'
    return https_base, kind


def _commit_url(base, kind, sha):
    if kind == 'gitlab':
        return f'{base}/-/commit/{sha}'
    if kind == 'bitbucket':
        return f'{base}/commits/{sha}'
    # GitHub, Gitea, Forgejo, Gogs, and most other frontends use /commit/<sha>
    return f'{base}/commit/{sha}'
