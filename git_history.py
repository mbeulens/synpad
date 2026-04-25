"""Git history viewer — runs `git log` against a local or SFTP repo and
renders the result in the Console pane's "Git History" tab."""

import os
import shlex
import subprocess
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib

GIT_LOG_FORMAT = '%h%x09%ad%x09%an%x09%s'
GIT_LOG_COUNT = 50
GIT_TIMEOUT = 15


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
            GLib.idle_add(self._git_render, repo_dir, branch, res.stdout)
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
            log_text = self._git_exec_remote(
                transport,
                f'cd {quoted} && git log '
                f'--pretty=format:{GIT_LOG_FORMAT} '
                f'--date=short -{GIT_LOG_COUNT} 2>&1',
            )
            GLib.idle_add(self._git_render, repo_path, branch, log_text)
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

    def _git_render(self, repo_path, branch, log_text):
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
