"""Tests for SFTPManager liveness/timeout behaviour.

These cover the half-open-connection hang: a save wedged forever on a remote
that silently went away. The fix gives the SFTP channel a finite operation
timeout and makes is_alive() do a real round-trip with a short probe timeout,
instead of trusting transport.is_active() (which stays True on a half-open
socket because paramiko's keepalive never disconnects on missed replies).

Runnable directly (python3 tests/test_connection_liveness.py) or via pytest.
No network or real paramiko transport required — the SFTP client is faked.
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connection
from connection import SFTPManager, SFTP_OP_TIMEOUT, SFTP_PROBE_TIMEOUT


class FakeChannel:
    def __init__(self, timeout=SFTP_OP_TIMEOUT):
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value


class FakeSFTP:
    """Stand-in for paramiko.SFTPClient. stat() either records the channel
    timeout in force at call time and returns, or raises a configured error."""

    def __init__(self, channel, stat_error=None):
        self._channel = channel
        self._stat_error = stat_error
        self.stat_calls = 0
        self.timeout_during_stat = None

    def get_channel(self):
        return self._channel

    def stat(self, path):
        self.stat_calls += 1
        self.timeout_during_stat = self._channel.gettimeout()
        if self._stat_error is not None:
            raise self._stat_error
        return object()  # a truthy SFTPAttributes stand-in


class FakeTransport:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


def _make_manager(stat_error=None, transport_active=True):
    mgr = SFTPManager()
    chan = FakeChannel()
    mgr.sftp = FakeSFTP(chan, stat_error=stat_error)
    mgr.transport = FakeTransport(active=transport_active)
    mgr.connected = True
    mgr.home_dir = '/home/user'
    return mgr, chan


def test_is_alive_true_when_stat_succeeds():
    mgr, _chan = _make_manager()
    assert mgr.is_alive() is True
    assert mgr.connected is True
    assert mgr.sftp.stat_calls == 1  # actually probed, not just is_active()


def test_is_alive_false_on_socket_timeout():
    # The half-open-connection case: transport still "active", but the
    # round-trip never completes. Must be reported dead.
    mgr, _chan = _make_manager(stat_error=socket.timeout())
    assert mgr.is_alive() is False
    assert mgr.connected is False


def test_is_alive_false_on_eof():
    mgr, _chan = _make_manager(stat_error=EOFError())
    assert mgr.is_alive() is False
    assert mgr.connected is False


def test_is_alive_uses_short_probe_timeout_then_restores():
    mgr, chan = _make_manager()
    assert mgr.is_alive() is True
    # The probe ran at the short timeout...
    assert mgr.sftp.timeout_during_stat == SFTP_PROBE_TIMEOUT
    # ...and restored the channel to its normal operation timeout afterwards.
    assert chan.gettimeout() == SFTP_OP_TIMEOUT


def test_is_alive_false_when_transport_inactive():
    mgr, _chan = _make_manager(transport_active=False)
    assert mgr.is_alive() is False
    assert mgr.connected is False
    assert mgr.sftp.stat_calls == 0  # short-circuits before the round-trip


def test_is_alive_false_when_not_connected():
    mgr, _chan = _make_manager()
    mgr.connected = False
    assert mgr.is_alive() is False


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e!r}")
        except Exception as e:
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(_run())
