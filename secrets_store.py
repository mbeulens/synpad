"""Encrypted credential storage backed by the OS secret service
(GNOME Keyring / KWallet via the freedesktop Secret Service API).

Falls back gracefully when secretstorage isn't installed or DBus isn't
available — callers see None / False return values and revert to plaintext
storage in config.json. The fallback path keeps the app working in
headless / sshed sessions where no keyring daemon is reachable.
"""

import sys

try:
    import secretstorage
    HAS_SECRETSTORAGE = True
except ImportError:
    HAS_SECRETSTORAGE = False

ATTR_GUID = 'synpad_profile_guid'
ATTR_APP = 'application'
APP_VALUE = 'synpad'

_connection = None
_collection = None
_init_failed = False


def _ensure_collection():
    """Lazily connect to the secret service and unlock the default collection.
    Returns the collection or None if unavailable."""
    global _connection, _collection, _init_failed
    if _init_failed:
        return None
    if _collection is not None:
        return _collection
    if not HAS_SECRETSTORAGE:
        _init_failed = True
        sys.stderr.write(
            "synpad: python3-secretstorage not installed; "
            "falling back to plaintext password storage\n")
        return None
    try:
        _connection = secretstorage.dbus_init()
        _collection = secretstorage.get_default_collection(_connection)
        if _collection.is_locked():
            _collection.unlock()
        return _collection
    except Exception as e:
        _init_failed = True
        sys.stderr.write(
            f"synpad: secret service unavailable ({type(e).__name__}: {e}); "
            "falling back to plaintext password storage\n")
        return None


def is_available() -> bool:
    return _ensure_collection() is not None


def get_password(guid: str):
    """Return the stored password for `guid`, or None."""
    coll = _ensure_collection()
    if coll is None or not guid:
        return None
    try:
        items = list(coll.search_items({ATTR_GUID: guid, ATTR_APP: APP_VALUE}))
        if not items:
            return None
        return items[0].get_secret().decode('utf-8', errors='replace')
    except Exception:
        return None


def set_password(guid: str, password: str) -> bool:
    """Store `password` under `guid`. Returns True on success."""
    coll = _ensure_collection()
    if coll is None or not guid:
        return False
    try:
        for item in coll.search_items({ATTR_GUID: guid, ATTR_APP: APP_VALUE}):
            item.delete()
        coll.create_item(
            label=f'SynPad: {guid}',
            attributes={ATTR_GUID: guid, ATTR_APP: APP_VALUE},
            secret=password.encode('utf-8'),
            replace=True,
        )
        return True
    except Exception:
        return False


def delete_password(guid: str) -> None:
    coll = _ensure_collection()
    if coll is None or not guid:
        return
    try:
        for item in coll.search_items({ATTR_GUID: guid, ATTR_APP: APP_VALUE}):
            item.delete()
    except Exception:
        pass
