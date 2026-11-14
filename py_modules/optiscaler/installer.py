"""Install, detect and remove OptiScaler inside a game directory.

Mirrors what the official setup_linux.sh does — copy the release next to the
game executable and rename OptiScaler.dll to a proxy filename the game already
loads — but records a manifest and stashes every displaced file in a visible
backup folder, so an uninstall can put the game back exactly as it was.
"""

import json
import os
import shutil
import stat
import threading
import time
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from pathlib import PurePosixPath

from . import constants, fsr4build, live, schema
from .inifile import IniFile
from .constants import (
    OPTIPATCHER_NAME,
    BACKUP_DIR,
    FFX_UPSCALER_DLL,
    FSR4_MIN_SDK_VERSION,
    DEFAULT_PROXY,
    FSR4_SOURCE_HINTS,
    FSR4_SUPPORT_FILES,
    INI_NAME,
    LAUNCH_RECORD_NAME,
    LOG_NAME,
    MANIFEST_NAME,
    OPTISCALER_VERSION,
    PAYLOAD_DIRS,
    PAYLOAD_FILES,
    PROXY_FILENAMES,
    REFRAMEWORK_DLL,
    RUNTIME_ARTIFACTS,
)

# Suffix used by v0.1 of this plugin, still restored on uninstall.
LEGACY_BACKUP_SUFFIX = ".decky-optiscaler.bak"
DLL_MARKER = b"OptiScaler"
MIN_OPTISCALER_SIZE = 4 * 1024 * 1024
RESTORE_DIR = ".decky_optiscaler_restore"
TRANSACTION_PREFIX = ".decky-optiscaler-tx-"
TRANSACTION_PREPARING_PREFIX = ".decky-optiscaler-tx-preparing-"
TRANSACTION_RETIRING_PREFIX = ".decky-optiscaler-tx-cleanup-"
TRANSACTION_COPY_PREFIX = ".decky-optiscaler-copy-"
RESTOREPOINT_STAGING_PREFIX = ".decky-restorepoint-"
TRANSACTION_RECORD = "journal.json"
TRANSACTION_VERSION = 1

_TRANSACTIONS = threading.local()


def _allowed_file_names():
    nested = _payload_nested_names()
    return (set(PROXY_FILENAMES) | set(PAYLOAD_FILES) | set(FSR4_SUPPORT_FILES)
            | set(RUNTIME_ARTIFACTS) | {INI_NAME, LOG_NAME, REFRAMEWORK_DLL,
                                       "remove_optiscaler.sh",
                                       live.CMD_FILE, live.STATUS_FILE, live.LOG_FILE,
                                       f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}",
                                       f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}"}
            | set(nested))


def _allowed_dir_names():
    return set(PAYLOAD_DIRS) | {"DlssOverrides", live.PLUGIN_SUBDIR}


def _payload_nested_names(payload_root=None):
    from . import payload as runtime_payload

    inventory = runtime_payload.required_inventory()
    return sorted(name for name in inventory["files"] if "/" in name)


def _relative_name(name):
    """Normalize a manifest or transaction path without accepting traversal."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError(f"invalid managed path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"invalid managed path: {name!r}")
    normalized = path.as_posix()
    if normalized != name:
        raise ValueError(f"invalid managed path: {name!r}")
    return normalized


def _validate_owned_name(name, directory=False):
    normalized = _relative_name(name)
    allowed = _allowed_dir_names() if directory else _allowed_file_names()
    allowed_casefold = {item.casefold() for item in allowed}
    if normalized not in allowed and normalized.casefold() not in allowed_casefold:
        raise ValueError(f"unsupported managed {'directory' if directory else 'file'}: {name}")
    return normalized


def _legacy_backup_target(name):
    """Return the supported root-level destination for a v0.1 .bak file."""
    if not isinstance(name, str) or "/" in name or "\\" in name:
        return None
    suffix = LEGACY_BACKUP_SUFFIX
    if not name.casefold().endswith(suffix.casefold()):
        return None
    destination = name[:-len(suffix)]
    try:
        return _validate_owned_name(destination)
    except ValueError:
        return None


def _safe_path(target, name):
    """Return a target-contained path, refusing symlinked parent components."""
    normalized = _relative_name(name)
    root = Path(target).resolve()
    path = root
    for part in PurePosixPath(normalized).parts[:-1]:
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"managed path crosses a symlink: {normalized}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"managed path parent is not a directory: {normalized}")
    result = root.joinpath(*PurePosixPath(normalized).parts)
    try:
        result.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"managed path leaves install target: {normalized}") from exc
    return result


def _install_relative_path(target_dir, destination):
    """Map a lexical destination onto the resolved install root without hiding links."""
    target_lexical = Path(os.path.abspath(os.fspath(target_dir)))
    target_resolved = target_lexical.resolve()
    destination = Path(destination)
    if ".." in destination.parts:
        raise ValueError(f"destination contains parent traversal: {destination}")
    if destination.is_absolute():
        destination_lexical = Path(os.path.abspath(os.fspath(destination)))
        try:
            relative = destination_lexical.relative_to(target_lexical)
        except ValueError:
            # Some callers already hold a path returned by _safe_path, which
            # uses the resolved spelling of a symlinked root (e.g. /private/var).
            try:
                relative = destination_lexical.relative_to(target_resolved)
            except ValueError as exc:
                raise ValueError(f"destination leaves install target: {destination}") from exc
    else:
        relative = destination
    return target_resolved, _relative_name(relative.as_posix())


def _remove_path(path):
    """Remove a path itself without following a final symlink."""
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink()
    else:
        shutil.rmtree(path)
    return True


def _existing_mode(path):
    try:
        return Path(path).lstat().st_mode
    except FileNotFoundError:
        return None


def _require_path_type(path, directory=False, allow_missing=True):
    mode = _existing_mode(path)
    if mode is None:
        if allow_missing:
            return False
        raise FileNotFoundError(path)
    expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not expected:
        kind = "directory" if directory else "regular file"
        raise ValueError(f"expected {kind} at {path}")
    return True


def _assert_plain_tree(root):
    root = Path(root)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError(f"expected directory at {root}")
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"directory tree contains an unsafe object: {path}")


def _merge_directory_tree(source, destination, target_dir=None, replace_files=None):
    """Restore a backed-up directory while preserving unrelated new files."""
    source, destination = Path(source), Path(destination)
    game_target = Path(target_dir).resolve() if target_dir is not None else None
    replace_files = set(replace_files or ())

    def make_merge_directory_writable(path):
        mode = _existing_mode(path)
        if mode is not None and stat.S_ISDIR(mode):
            permissions = stat.S_IMODE(mode)
            writable = permissions | stat.S_IWUSR | stat.S_IXUSR
            if writable != permissions:
                os.chmod(path, writable)

    _assert_plain_tree(source)
    _require_path_type(destination, directory=True)
    if _path_exists(destination):
        _assert_plain_tree(destination)
        make_merge_directory_writable(destination)
    destination.mkdir(parents=True, exist_ok=True)
    directory_pairs = []
    for base, dirs, files in os.walk(source, followlinks=False):
        relative = Path(base).relative_to(source)
        target_base = destination / relative
        directory_pairs.append((Path(base), target_base))
        make_merge_directory_writable(target_base)
        for name in dirs:
            item = target_base / name
            mode = _existing_mode(item)
            if mode is not None and not stat.S_ISDIR(mode):
                raise ValueError(f"directory backup collides with a non-directory: {item}")
            item.mkdir(exist_ok=True)
            make_merge_directory_writable(item)
        for name in files:
            original = Path(base) / name
            item = target_base / name
            mode = _existing_mode(item)
            if mode is not None and not stat.S_ISREG(mode):
                raise ValueError(f"file backup collides with a non-file: {item}")
            relative_file = (relative / name).as_posix()
            if mode is not None and relative_file not in replace_files:
                continue
            fd, temporary = tempfile.mkstemp(prefix=f".{item.name}.decky-restore-",
                                             dir=item.parent)
            os.close(fd)
            try:
                shutil.copy2(original, temporary)
                if game_target is not None:
                    require_stopped_target(game_target)
                os.replace(temporary, item)
            finally:
                Path(temporary).unlink(missing_ok=True)
    for source_dir, dest_dir in sorted(
            directory_pairs,
            key=lambda pair: len(pair[0].relative_to(source).parts), reverse=True):
        if game_target is not None:
            require_stopped_target(game_target)
        if dest_dir.exists():
            shutil.copystat(source_dir, dest_dir, follow_symlinks=False)


def _copy_path(source, destination):
    """Copy a regular file/tree or preserve a symlink as a symlink."""
    source, destination = Path(source), Path(destination)
    mode = source.lstat().st_mode
    if stat.S_ISLNK(mode):
        os.symlink(os.readlink(source), destination)
    elif stat.S_ISDIR(mode):
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    elif stat.S_ISREG(mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise ValueError(f"unsupported filesystem object in install target: {source}")


def _atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".decky-optiscaler-manifest-",
                                     dir=path.parent)
    os.close(fd)
    try:
        Path(temporary).write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _looks_like_optiscaler(path):
    """True if a proxy-named file is really an OptiScaler build."""
    try:
        if path.stat().st_size < MIN_OPTISCALER_SIZE:
            return False
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    return False
                if DLL_MARKER in chunk:
                    return True
    except OSError:
        return False


def _path_exists(path):
    try:
        Path(path).lstat()
        return True
    except FileNotFoundError:
        return False


def read_manifest(target_dir):
    path = _safe_path(target_dir, MANIFEST_NAME)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError("OptiScaler manifest is not a regular file")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"OptiScaler manifest is invalid: {exc}") from exc
    return validate_manifest(manifest)


def validate_manifest(manifest):
    """Reject malformed or unsafe ownership records before they drive mutations."""
    if not isinstance(manifest, dict) or manifest.get("plugin") != "decky-optiscaler":
        raise ValueError("OptiScaler manifest has an unexpected plugin identifier")

    filename = manifest.get("filename")
    if filename is not None and filename not in PROXY_FILENAMES:
        raise ValueError(f"OptiScaler manifest has an unsupported proxy filename: {filename!r}")
    if "backup_dir" in manifest and manifest["backup_dir"] != BACKUP_DIR:
        raise ValueError("OptiScaler manifest has an unsupported backup directory")

    for field, directory in (("files", False), ("dirs", True)):
        items = manifest.get(field, [])
        if not isinstance(items, list):
            raise ValueError(f"OptiScaler manifest {field} must be a list")
        seen = set()
        for item in items:
            name = _validate_owned_name(item, directory)
            identity = name.casefold()
            if identity in seen:
                raise ValueError(f"OptiScaler manifest repeats {field} entry: {name}")
            seen.add(identity)

    backups = manifest.get("backups", {})
    if not isinstance(backups, dict):
        raise ValueError("OptiScaler manifest backups must be an object")
    seen_backups = set()
    for name, stored_name in backups.items():
        normalized = _relative_name(name)
        if normalized.casefold() not in {
                item.casefold() for item in (_allowed_file_names() | _allowed_dir_names())}:
            raise ValueError(f"OptiScaler manifest has an unsupported backup path: {name}")
        if normalized.casefold() in seen_backups:
            raise ValueError(f"OptiScaler manifest repeats backup path: {name}")
        seen_backups.add(normalized.casefold())
        if not isinstance(stored_name, str):
            raise ValueError(f"OptiScaler manifest backup record is invalid: {name}")
        stored = _relative_name(stored_name)
        if stored not in (normalized, PurePosixPath(normalized).name):
            raise ValueError(f"OptiScaler manifest backup path does not match: {name}")
    runtime_owned = manifest.get("runtime_artifacts_owned", [])
    if not isinstance(runtime_owned, list):
        raise ValueError("OptiScaler manifest runtime_artifacts_owned must be a list")
    for name in runtime_owned:
        if name not in RUNTIME_ARTIFACTS:
            raise ValueError(f"OptiScaler manifest has an unsupported runtime artifact: {name}")
    created_dirs = manifest.get("created_directories", [])
    if not isinstance(created_dirs, list):
        raise ValueError("OptiScaler manifest created_directories must be a list")
    for name in created_dirs:
        if name not in set(PAYLOAD_DIRS) | {live.PLUGIN_SUBDIR}:
            raise ValueError(f"OptiScaler manifest has an unsupported created directory: {name}")
    original_dirs = manifest.get("original_directories", {})
    if not isinstance(original_dirs, dict):
        raise ValueError("OptiScaler manifest original_directories must be an object")
    for name, record in original_dirs.items():
        if name not in PAYLOAD_DIRS:
            raise ValueError(f"OptiScaler manifest has an unsupported original directory: {name}")
        if not isinstance(record, dict) or set(record) != {"exists", "mode"} \
                or not isinstance(record.get("exists"), bool):
            raise ValueError(f"OptiScaler manifest original directory record is invalid: {name}")
        mode = record.get("mode")
        if record["exists"]:
            if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
                raise ValueError(f"OptiScaler manifest original directory mode is invalid: {name}")
        elif mode is not None:
            raise ValueError(f"OptiScaler manifest absent directory has a mode: {name}")
    return manifest


def backup_dir(target_dir):
    return Path(target_dir) / BACKUP_DIR


def _transaction_paths(target, extra_names=(), include_known=True):
    """Known writable assets plus paths listed by the current safe manifest."""
    names = {MANIFEST_NAME, INI_NAME}
    if include_known:
        names.update({LAUNCH_RECORD_NAME, BACKUP_DIR, LOG_NAME,
                      "remove_optiscaler.sh", *PROXY_FILENAMES, *PAYLOAD_FILES,
                      *PAYLOAD_DIRS, *FSR4_SUPPORT_FILES, *RUNTIME_ARTIFACTS,
                      "DlssOverrides", f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}",
                      f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}",
                      live.CMD_FILE, live.STATUS_FILE, live.LOG_FILE})
    manifest = read_manifest(target)
    if manifest and include_known:
        names.update(manifest.get("files", []))
        names.update(manifest.get("dirs", []))
    if manifest and include_known:
        names.update(manifest.get("backups", {}).keys())
    if isinstance(extra_names, str):
        extra_names = (extra_names,)
    for name in extra_names or ():
        names.add(_relative_name(name))

    # Record any parent directory a nested file write may create. This also
    # preserves unrelated ASIs if the user already has a plugins/ folder.
    for name in list(names):
        parts = PurePosixPath(_relative_name(name)).parts
        for index in range(1, len(parts)):
            names.add(PurePosixPath(*parts[:index]).as_posix())

    normalized = sorted({_relative_name(name) for name in names})
    # Snapshot an owning directory once rather than duplicating each child.
    roots = set(normalized)
    for name in normalized:
        parts = PurePosixPath(name).parts
        if any(PurePosixPath(*parts[:i]).as_posix() in roots
               for i in range(1, len(parts))):
            roots.discard(name)
    return sorted(roots)


def _snapshot_into(target, journal, names, records):
    entries = journal / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    already = {record["name"] for record in records}
    for name in names:
        name = _relative_name(name)
        if name in already:
            continue
        if any(name.startswith(existing + "/") for existing in already):
            continue
        source = _safe_path(target, name)
        record = {"name": name, "exists": False, "stored": None}
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError:
            records.append(record)
            already.add(name)
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"managed transaction path is a symlink: {source}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"managed transaction path is a special file: {source}")
        if name == BACKUP_DIR and not stat.S_ISDIR(mode):
            raise ValueError(f"backup path is not a directory: {source}")
        record["exists"] = True
        record["stored"] = f"{len(records):06d}"
        _copy_path(source, entries / record["stored"])
        records.append(record)
        already.add(name)


def _transaction_record(state, target, status="active"):
    return {
        "version": TRANSACTION_VERSION,
        "target": str(target),
        "status": status,
        "require_stopped": bool(state.get("require_stopped")),
        "records": state["records"],
    }


def _persist_transaction(state, target, status="active"):
    _atomic_json(state["journal"] / TRANSACTION_RECORD,
                 _transaction_record(state, target, status))


def _restore_snapshot(target, journal, records, require_stopped):
    if require_stopped:
        require_stopped_target(target)
    staging_root = journal / "restore-staging"
    staging_root.mkdir(exist_ok=True)
    # Rebuild directories from the immutable journal before replacing target
    # paths. A retry after interruption uses the same source bytes and mapping.
    ordered = sorted(records, key=lambda item: len(PurePosixPath(item["name"]).parts))
    for index, record in enumerate(ordered):
        if require_stopped:
            require_stopped_target(target)
        destination = _safe_path(target, record["name"])
        if not record["exists"]:
            _remove_path(destination)
            continue
        saved = journal / "entries" / record["stored"]
        saved_mode = saved.lstat().st_mode
        destination.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISDIR(saved_mode):
            staged = staging_root / f"{index:06d}"
            if _path_exists(staged):
                _remove_path(staged)
            _copy_path(saved, staged)
            if require_stopped:
                require_stopped_target(target)
            _remove_path(destination)
            os.replace(staged, destination)
        else:
            while True:
                staged = destination.with_name(f"{TRANSACTION_COPY_PREFIX}{uuid.uuid4().hex}")
                relative_stage = staged.relative_to(target).as_posix()
                _safe_path(target, relative_stage)
                if not _path_exists(staged):
                    break
            _snapshot_into(target, journal, (relative_stage,), records)
            _persist_transaction({"journal": journal, "records": records,
                                  "require_stopped": require_stopped}, target)
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            try:
                _copy_path(saved, staged)
                if require_stopped:
                    require_stopped_target(target)
                _remove_path(destination)
                if require_stopped:
                    require_stopped_target(target)
                os.replace(staged, destination)
            finally:
                staged.unlink(missing_ok=True)


def _validate_transaction_records(target, journal, records):
    if not isinstance(records, list):
        raise ValueError("transaction journal records are invalid")
    seen = set()
    entries = Path(journal) / "entries"
    if entries.is_symlink() or not entries.is_dir():
        raise ValueError("transaction journal entries are missing or unsafe")
    allowed = (_allowed_file_names() | _allowed_dir_names() | {
        MANIFEST_NAME, LAUNCH_RECORD_NAME, BACKUP_DIR, RESTORE_DIR,
    })
    allowed_casefold = {name.casefold() for name in allowed}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("transaction journal contains an invalid path record")
        name = _relative_name(item["name"])
        leaf = PurePosixPath(name).name
        copy_stage = leaf.casefold().startswith(TRANSACTION_COPY_PREFIX.casefold())
        restorepoint_stage = leaf.casefold().startswith(RESTOREPOINT_STAGING_PREFIX.casefold())
        if (name.casefold() not in allowed_casefold
                and _legacy_backup_target(name) is None
                and not copy_stage and not restorepoint_stage):
            raise ValueError(f"transaction journal contains an unsupported path: {name}")
        if restorepoint_stage and "/" in name:
            raise ValueError("restorepoint staging must be a root-level path")
        if name.casefold() in seen:
            raise ValueError(f"transaction journal repeats a path: {name}")
        seen.add(name.casefold())
        if not isinstance(item.get("exists"), bool):
            raise ValueError(f"transaction journal has an invalid existence record: {name}")
        if (copy_stage or restorepoint_stage) and item["exists"]:
            raise ValueError(f"transaction staging path was not absent at start: {name}")
        if not item["exists"]:
            if item.get("stored") is not None:
                raise ValueError(f"transaction journal has invalid absent record: {name}")
            continue
        stored = item.get("stored")
        if not isinstance(stored, str) or not stored.isdigit():
            raise ValueError(f"transaction journal has invalid content mapping: {name}")
        saved = entries / stored
        if saved.is_symlink() or not (saved.is_file() or saved.is_dir()):
            raise ValueError(f"transaction journal content is missing or unsafe: {name}")
        _safe_path(target, name)


def recover_target_transactions(target_dir):
    """Retry any interrupted target operation before allowing a new mutation."""
    target = Path(target_dir).resolve()
    active = getattr(_TRANSACTIONS, "targets", {})
    if str(target) in active or not target.is_dir():
        return []

    recovered = []
    for path in sorted(target.iterdir()):
        if path.name.startswith(TRANSACTION_RETIRING_PREFIX):
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"unsafe retired transaction cleanup path: {path}")
            shutil.rmtree(path)
            recovered.append({"path": path.name, "action": "cleaned"})
            continue
        if path.name.startswith(TRANSACTION_PREPARING_PREFIX):
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"unsafe incomplete transaction staging path: {path}")
            shutil.rmtree(path)
            continue
        if not path.name.startswith(TRANSACTION_PREFIX):
            continue
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"unsafe transaction journal path: {path}")
        record_path = path / TRANSACTION_RECORD
        if record_path.is_symlink() or not record_path.is_file():
            # New journals are atomically published only after their complete
            # snapshot and mapping exist. An older or damaged journal is not
            # safe to guess at, so block mutations and keep its bytes in place.
            raise RuntimeError(f"transaction journal is incomplete or unreadable: {path}")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"transaction journal is invalid: {path}: {exc}") from exc
        if (not isinstance(record, dict) or record.get("version") != TRANSACTION_VERSION
                or record.get("target") != str(target)
                or record.get("status") not in ("active", "committed")
                or not isinstance(record.get("require_stopped"), bool)
                or not isinstance(record.get("records"), list)):
            raise RuntimeError(f"transaction journal has an unsupported format: {path}")
        if record["status"] == "committed":
            _retire_transaction(path, target)
            recovered.append({"path": path.name, "action": "cleaned"})
            continue
        try:
            _validate_transaction_records(target, path, record["records"])
        except ValueError as exc:
            raise RuntimeError(f"transaction journal has unsafe mappings: {path}: {exc}") from exc
        _restore_snapshot(target, path, record["records"], record["require_stopped"])
        _persist_transaction({"journal": path, "records": record["records"],
                              "require_stopped": record["require_stopped"]},
                             target, status="committed")
        _retire_transaction(path, target)
        recovered.append({"path": path.name, "action": "rolled_back"})

    # Restorepoint staging lives next to the game's committed snapshot. Older
    # interrupted versions did not journal it; it cannot be a usable snapshot.
    for path in sorted(target.iterdir()):
        if path.name.startswith(RESTOREPOINT_STAGING_PREFIX):
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"unsafe restore point staging path: {path}")
            shutil.rmtree(path)
    return recovered


def _retire_transaction(journal, target):
    """Rename a finished journal before cleanup so partial deletion is retryable."""
    journal, target = Path(journal), Path(target)
    retiring = target / f"{TRANSACTION_RETIRING_PREFIX}{uuid.uuid4().hex}"
    os.replace(journal, retiring)
    shutil.rmtree(retiring)


@contextmanager
def target_transaction(target_dir, extra_names=(), require_stopped=False, include_known=True):
    """Journal known target assets and restore them if a mutation raises.

    Nested calls on the same thread share the outer journal. Callers that may
    change runtime binaries set ``require_stopped=True`` so rollback receives
    the same process guard as the original write.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    active = getattr(_TRANSACTIONS, "targets", None)
    if active is None:
        active = _TRANSACTIONS.targets = {}
    key = str(target)
    state = active.get(key)
    if state:
        names = _transaction_paths(target, extra_names, include_known)
        if require_stopped:
            require_stopped_target(target)
        _snapshot_into(target, state["journal"], names, state["records"])
        state["require_stopped"] = state["require_stopped"] or require_stopped
        _persist_transaction(state, target)
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
        return

    recover_target_transactions(target)
    names = _transaction_paths(target, extra_names, include_known)
    if require_stopped:
        require_stopped_target(target)
    preparing = Path(tempfile.mkdtemp(prefix=TRANSACTION_PREPARING_PREFIX, dir=target))
    journal = target / f"{TRANSACTION_PREFIX}{uuid.uuid4().hex}"
    state = {"journal": preparing, "records": [], "depth": 1,
             "require_stopped": bool(require_stopped)}
    # Snapshot failures occur before the body can mutate anything. Remove only
    # the preparation directory and do not run rollback against a partial map.
    try:
        _snapshot_into(target, preparing, names, state["records"])
        _persist_transaction(state, target)
        os.replace(preparing, journal)
        state["journal"] = journal
    except BaseException:
        if preparing.exists():
            shutil.rmtree(preparing)
        raise
    active[key] = state
    try:
        yield
    except BaseException as operation_error:
        try:
            _restore_snapshot(target, journal, state["records"], state["require_stopped"])
        except Exception as rollback_error:
            raise RuntimeError(
                f"operation failed and rollback could not finish; recovery journal remains at {journal}: "
                f"{rollback_error}"
            ) from operation_error
        try:
            _persist_transaction(state, target, status="committed")
            _retire_transaction(journal, target)
        except OSError as cleanup_error:
            raise RuntimeError(
                f"operation rolled back but transaction journal cleanup failed at {journal}: {cleanup_error}"
            ) from operation_error
        raise
    else:
        try:
            _persist_transaction(state, target, status="committed")
            _retire_transaction(journal, target)
        except OSError as cleanup_error:
            raise RuntimeError(
                f"operation committed but transaction journal cleanup failed at {journal}: {cleanup_error}"
            ) from cleanup_error
    finally:
        active.pop(key, None)


def launch_record_path(target_dir):
    return Path(target_dir) / LAUNCH_RECORD_NAME


def read_launch_record(target_dir):
    """The Steam launch options this game had before OptiScaler went in.

    ``recorded`` is what separates "there was nothing there" from "nobody
    looked": an empty string is a real answer and the reason uninstalling can
    offer to clear the launch options outright, while a missing file means the
    install predates this record or Steam would not say, and only the override
    this plugin added can safely be taken back out.
    """
    path = launch_record_path(target_dir)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return {"recorded": False, "value": ""}
    if not stat.S_ISREG(mode):
        raise ValueError("OptiScaler launch-options record is not a regular file")
    try:
        return {"recorded": True, "value": path.read_text(encoding="utf-8")}
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"OptiScaler launch-options record is unreadable: {exc}") from exc


def record_launch_options(target_dir, options, logger=None):
    """Keep the pre-install launch options, once.

    Write-once for the same reason ``_stash`` keeps the oldest backup: the value
    worth putting back is the one from before this plugin first touched the
    game, and a reinstall — or turning the launch-options step off and on again
    — must not overwrite it with the override we ourselves wrote.
    """
    target = Path(target_dir).resolve()
    recover_target_transactions(target)
    path = launch_record_path(target)
    existing = read_launch_record(target)
    if existing["recorded"]:
        return {**existing, "written": False}
    text = options or ""
    fd, temporary = tempfile.mkstemp(prefix=".decky-launch-options-", dir=target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Link publishes the complete file without overwriting a record
            # created concurrently by another Decky action.
            os.link(temporary, path)
            written = True
        except FileExistsError:
            existing = read_launch_record(target)
            if not existing["recorded"]:
                raise ValueError("OptiScaler launch-options record changed while it was being saved")
            return {**existing, "written": False}
    finally:
        Path(temporary).unlink(missing_ok=True)
    if logger:
        logger.info("recorded previous launch options for %s", target_dir)
    return {"recorded": True, "value": text, "written": written}


def forget_launch_options(target_dir):
    """Drop the record. Called by uninstall: it belongs to one install."""
    path = launch_record_path(target_dir)
    record = read_launch_record(target_dir)
    existed = record["recorded"]
    path.unlink(missing_ok=True)
    return existed


def _assert_known_live_asi(path):
    path = Path(path)
    if not _path_exists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("Cannot safely remove the existing live-control ASI: its file type is unknown.")
    expected = getattr(constants, "LEGACY_LIVE_ASI_SHA256", "")
    if not expected or _sha256_file(path) != expected:
        raise ValueError(
            "Cannot safely remove plugins/decky_optiscaler_live.asi because its bytes are unknown."
        )


def detect(target_dir):
    """Report whether (and how) OptiScaler is installed in a directory."""
    target = Path(target_dir)
    result = {
        "path": str(target),
        "installed": False,
        "filename": None,
        "managed": False,
        "version": None,
        "installed_at": None,
        "ini_present": False,
        "ini_path": None,
        "log_present": False,
        "log_path": None,
        "candidates": [],
        "extra_proxies": [],
        "backup_dir": str(backup_dir(target)),
        "backed_up": [],
        "launch_record": {"recorded": False, "value": ""},
        "fsr4": {},
        # REFramework, for the RE Engine games that need it. Reported from the
        # folder rather than from the manifest so a copy the user put there
        # themselves counts — the step is "is REF present", not "did we install
        # REF", and re-installing over somebody's own working REF would be a
        # worse answer than leaving it alone.
        "reframework": {"installed": False, "revision": None, "managed": False},
    }
    if not target.is_dir():
        return result

    result["launch_record"] = read_launch_record(target)

    ini = target / INI_NAME
    result["ini_present"] = ini.is_file()
    result["ini_path"] = str(ini)
    log = target / LOG_NAME
    result["log_present"] = log.is_file()
    result["log_path"] = str(log)
    result["fsr4"] = fsr4_status(target)

    result["reframework"] = {
        "installed": (target / REFRAMEWORK_DLL).is_file(),
        # Which build went in is only knowable from what we recorded: the
        # stamp file is deliberately not installed next to the game.
        "revision": None,
        "managed": False,
    }

    stash = backup_dir(target)
    if stash.is_dir():
        result["backed_up"] = sorted(p.name for p in stash.iterdir())

    present = [name for name in PROXY_FILENAMES if (target / name).is_file()]
    result["candidates"] = present

    manifest = read_manifest(target)
    if manifest and manifest.get("filename"):
        name = manifest["filename"]
        if (target / name).is_file():
            result.update(
                installed=True,
                filename=name,
                managed=True,
                version=manifest.get("optiscaler_version"),
                installed_at=manifest.get("installed_at"),
            )
            result["reframework"]["managed"] = bool(manifest.get("reframework"))
            result["reframework"]["revision"] = manifest.get("reframework_revision")
            result["extra_proxies"] = [p for p in present if p != name
                                       and _looks_like_optiscaler(target / p)]
            return result

    # Installed by hand or by another tool: identify by content.
    for name in present:
        if _looks_like_optiscaler(target / name):
            result.update(installed=True, filename=name, managed=False)
            result["extra_proxies"] = [
                p for p in present if p != name and _looks_like_optiscaler(target / p)
            ]
            break
    return result


def _reject_case_variant_proxies(target):
    """Wine treats DLL names case-insensitively; do not create shadow entries."""
    known = {name.casefold(): name for name in PROXY_FILENAMES}
    for entry in Path(target).iterdir():
        canonical = known.get(entry.name.casefold())
        if canonical and entry.name != canonical:
            raise ValueError(
                f"Cannot safely install beside case-variant proxy {entry.name}; "
                f"rename it to {canonical} or remove it first."
            )


def ffx_upscaler_info(target_dir):
    """The FidelityFX upscaler library actually sitting in the game folder.

    This is the version OptiScaler's overlay prints in its FFX Settings box, and
    it is what decides whether FSR 4 is even on offer - so it belongs in front
    of the user rather than behind a verify step.
    """
    dll = Path(target_dir) / FFX_UPSCALER_DLL
    version = pe_file_version(dll) if dll.is_file() else None
    return {
        "present": dll.is_file(),
        "name": FFX_UPSCALER_DLL,
        "version": ".".join(str(n) for n in version) if version else None,
        # The tuple as well, because callers compare it against the floor FSR 4
        # starts at and against the 4.1.1 the INT8 override needs — and parsing
        # the string back into numbers in each of them is a chance for them to
        # disagree.
        "version_tuple": list(version) if version else None,
        # OptiScaler treats an SDK at or above 4.0.2 as providing FSR 4.
        "fsr4_capable": bool(version and version[:3] >= FSR4_MIN_SDK_VERSION[:3]),
    }


def fsr4_status(target_dir):
    """Which FSR4 support DLLs are present next to the game executable."""
    target = Path(target_dir)
    present = {name: (target / name).is_file() for name in FSR4_SUPPORT_FILES}
    ffx = ffx_upscaler_info(target_dir)
    return {
        "files": present,
        "ready": present.get("amdxcffx64.dll", False),
        "required": FSR4_SUPPORT_FILES,
        "ffx": ffx,
        # Which upscaler build this actually is. Read out of the bytes rather
        # than the manifest, so a game that had a build dropped into it by hand
        # — or by another tool — is reported the same way one installed from
        # here is, and reported as *unknown* rather than as the released one when
        # the hash matches nothing (see fsr4build.identify).
        "build": fsr4build.identify(target / FFX_UPSCALER_DLL),
    }


def find_fsr4_sources(home):
    """Folders on this machine that already hold FSR4 support DLLs."""
    found = []
    for hint in FSR4_SOURCE_HINTS:
        candidate = Path(home) / hint
        if not candidate.is_dir():
            continue
        names = [n for n in FSR4_SUPPORT_FILES if (candidate / n).is_file()]
        if names:
            found.append({"path": str(candidate), "files": names})
    return found


def import_fsr4_files(target_dir, source_dir, logger=None):
    """Copy user-supplied FSR4 DLLs in and register them for later removal."""
    target = Path(target_dir).resolve()
    source = Path(source_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    if not source.is_dir():
        raise NotADirectoryError(f"source folder does not exist: {source}")
    recover_target_transactions(target)

    names = [n for n in FSR4_SUPPORT_FILES if (source / n).is_file()]
    if not names:
        raise FileNotFoundError(
            f"no FSR4 support files ({', '.join(FSR4_SUPPORT_FILES)}) in {source}"
        )

    require_stopped_target(target)
    with target_transaction(target, require_stopped=True):
        manifest = read_manifest(target)
        if not manifest:
            raise ValueError("Install OptiScaler with this plugin before importing support files.")
        _backup_items(target, manifest)
        backups = dict(manifest.get("backups") or {})
        files = list(manifest.get("files") or [])

        for name in names:
            require_stopped_target(target)
            destination = _safe_path(target, name)
            if _path_exists(destination) and name not in backups and name not in files:
                backups[name] = _stash(target, destination, logger)
            atomic_copy(source / name, destination, target_dir=target)
            if name not in files:
                files.append(name)
            if logger:
                logger.info("installed FSR4 support file %s", name)

        manifest["files"] = files
        manifest["backups"] = backups
        _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)

    return {"path": str(target), "imported": names, "source": str(source)}


def require_stopped_target(target_dir):
    """Reject runtime mutation while a game has the target mapped or attached."""
    target = Path(target_dir).resolve()
    status = live.status(target)
    if status.get("attached") or status.get("legacy_running"):
        raise RuntimeError("Close the game before changing files in this game directory.")

    prefix = str(target) + "/"
    translations = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"),
                    ("\\134", "\\"))
    for maps in Path("/proc").glob("[0-9]*/maps"):
        try:
            lines = maps.read_text(errors="replace").splitlines()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) != 6:
                continue
            mapped = fields[5]
            for encoded, decoded in translations:
                mapped = mapped.replace(encoded, decoded)
            if mapped.endswith(" (deleted)"):
                mapped = mapped[:-10]
            if mapped == str(target) or mapped.startswith(prefix):
                raise RuntimeError("Close the game before changing files in this game directory.")
    return True


def require_stopped_install(target_dir):
    """Require a managed install and reject DLL replacement while it is in use."""
    target = Path(target_dir).resolve()
    require_stopped_target(target)
    manifest = read_manifest(target)
    if not manifest:
        raise ValueError("Install OptiScaler with this plugin first.")
    return manifest


def atomic_copy(source, destination, target_dir=None):
    """Stage beside the destination so replacement is atomic on its filesystem."""
    source = Path(source)
    destination = Path(destination)
    if target_dir is None:
        if destination.parent.name == live.PLUGIN_SUBDIR and destination.name in (
                live.ASI_NAME, OPTIPATCHER_NAME):
            target_dir = destination.parent.parent
        elif destination.name in (set(PROXY_FILENAMES) | set(PAYLOAD_FILES)
                                  | set(FSR4_SUPPORT_FILES) | {REFRAMEWORK_DLL, INI_NAME,
                                                                LOG_NAME}):
            target_dir = destination.parent
    if target_dir is not None:
        target, relative = _install_relative_path(target_dir, destination)
        destination = _safe_path(target, relative)
        active = getattr(_TRANSACTIONS, "targets", {})
        if str(target) not in active:
            with target_transaction(target, extra_names=(relative,), require_stopped=True):
                return atomic_copy(source, destination, target_dir=target)
        require_stopped_target(target)
    try:
        mode = destination.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise ValueError(f"refusing to replace non-regular file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if target_dir is not None:
        target = Path(target_dir).resolve()
        state = getattr(_TRANSACTIONS, "targets", {}).get(str(target))
        if state is None:
            raise RuntimeError("target copy is not protected by an active transaction")
        while True:
            staged = destination.with_name(f"{TRANSACTION_COPY_PREFIX}{uuid.uuid4().hex}")
            relative_stage = staged.relative_to(target).as_posix()
            _safe_path(target, relative_stage)
            if not _path_exists(staged):
                break
        _snapshot_into(target, state["journal"], (relative_stage,), state["records"])
        _persist_transaction(state, target)
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        name = str(staged)
    else:
        fd, name = tempfile.mkstemp(prefix=".decky-optiscaler-", dir=destination.parent)
        os.close(fd)
    try:
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"copy source is not a regular file: {source}")
        shutil.copy2(source, name)
        if target_dir is not None:
            require_stopped_target(target_dir)
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def replace_directory_from(source, destination, target_dir):
    """Atomically publish an exact, prevalidated directory from a target transaction."""
    source = Path(source)
    target, relative = _install_relative_path(target_dir, destination)
    destination = _safe_path(target, relative)
    active = getattr(_TRANSACTIONS, "targets", {})
    if str(target) not in active:
        with target_transaction(target, extra_names=(relative,), require_stopped=True):
            return replace_directory_from(source, destination, target)
    _assert_plain_tree(source)
    mode = _existing_mode(destination)
    if mode is not None and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise ValueError(f"directory destination has an unsafe filesystem type: {destination}")
    if mode is not None and stat.S_ISDIR(mode):
        _assert_plain_tree(destination)

    journal = active[str(target)]["journal"]
    stage_parent = journal / "directory-staging"
    stage_parent.mkdir(exist_ok=True)
    stage = stage_parent / f"new-{uuid.uuid4().hex}"
    old = stage_parent / f"old-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage, symlinks=False, copy_function=shutil.copy2)
        require_stopped_target(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            os.replace(destination, old)
        try:
            os.replace(stage, destination)
        except Exception:
            if mode is not None and _path_exists(old):
                os.replace(old, destination)
            raise
        if _path_exists(old):
            _remove_path(old)
    finally:
        if _path_exists(stage):
            _remove_path(stage)
        if _path_exists(old) and not _path_exists(destination):
            os.replace(old, destination)


def runtime_current(target_dir):
    """Whether the installed proxy bytes match this plugin's pinned runtime."""
    manifest = read_manifest(target_dir)
    filename = manifest.get("filename") if manifest else None
    if filename not in PROXY_FILENAMES:
        filename = detect(target_dir).get("filename")
    if not filename:
        return False
    path = _safe_path(target_dir, filename)
    if path.is_symlink() or not path.is_file():
        return False
    expected = getattr(constants, "RUNTIME_DLL_SHA256", "")
    if not expected:
        return False
    import hashlib

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return False
    return digest.hexdigest() == expected


def _replace_fsr4(target, source, recorded, enable_int8=False):
    target = Path(target).resolve()
    recover_target_transactions(target)
    require_stopped_install(target)
    if not runtime_current(target):
        raise ValueError("Upgrade OptiScaler to the supported runtime before changing FSR 4 settings.")
    with target_transaction(target, require_stopped=True):
        manifest = read_manifest(target)
        _backup_items(target, manifest)
        if not manifest or FFX_UPSCALER_DLL not in manifest.get("files", []):
            raise ValueError("Reinstall OptiScaler before changing its upscaler build.")
        atomic_copy(source, _safe_path(target, FFX_UPSCALER_DLL), target_dir=target)
        if enable_int8:
            ini_path = _safe_path(target, INI_NAME)
            if not ini_path.is_file():
                raise FileNotFoundError("OptiScaler.ini not found")
            with tempfile.TemporaryDirectory(prefix=".decky-fsr4-") as staging:
                edited_ini = Path(staging) / INI_NAME
                shutil.copy2(ini_path, edited_ini)
                ini = IniFile(edited_ini)
                changes = {
                    "Upscalers": {"Dx12Upscaler": "fsr31", "Dx11Upscaler": "fsr31_12",
                                  "VulkanUpscaler": "fsr31_12"},
                    "FSR": {"Fsr4Update": "auto", "Fsr4ForceEnableInt8": "1",
                            "UpscalerIndex": "0", "Fsr4Preset": "auto"},
                }
                for section, values in changes.items():
                    for key, value in values.items():
                        meta = schema.option(section, key)
                        if meta is None or not schema.valid(meta, value):
                            raise ValueError(f"Invalid FSR 4 preset setting: {section}.{key}={value}")
                ini.update(changes)
                ini.save()
                atomic_copy(edited_ini, ini_path, target_dir=target)
        manifest["fsr4_build"] = recorded
        _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)


def install_fsr4_build(target_dir, dll_path, build, logger=None, enable_int8=False):
    """Install a verified community DLL, optionally enabling the Deck preset."""
    if fsr4build.digest(dll_path) != build["file_sha256"]:
        raise ValueError("FSR 4 DLL checksum mismatch")
    recorded = {"id": build["id"], "label": build["label"],
                "file": FFX_UPSCALER_DLL, "sha256": build["file_sha256"],
                "reaches_fsr4_by": build["reaches_fsr4_by"]}
    _replace_fsr4(target_dir, dll_path, recorded, enable_int8)
    return {"path": str(target_dir), "file": FFX_UPSCALER_DLL, "build": recorded}


def restore_fsr4_build(target_dir, payload_root, logger=None):
    """Restore the bundled DLL; upscaler and frame-generation settings stay intact."""
    source = Path(payload_root) / FFX_UPSCALER_DLL
    if fsr4build.digest(source) != fsr4build.BUNDLED_BUILD["file_sha256"]:
        raise ValueError("Bundled FSR 4 DLL checksum mismatch")
    _replace_fsr4(target_dir, source, {})
    return {"path": str(target_dir), "restored": FFX_UPSCALER_DLL,
            "build": fsr4build.identify(Path(target_dir) / FFX_UPSCALER_DLL)}


def add_files(target_dir, sources, logger=None, reframework_revision=None):
    """Copy extra files into an existing install and register them for removal.

    The same contract as everything else this plugin puts in a game folder:
    anything displaced is stashed, everything added is written into the
    manifest, and uninstall reverses both. Used to add REFramework to a game
    that is already set up, or to retry a download that failed the first time.

    A game with no manifest — OptiScaler installed by hand, or by another tool —
    is refused because there is no durable ownership record for removal.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    recover_target_transactions(target)

    require_stopped_target(target)
    with target_transaction(target, require_stopped=True):
        manifest = read_manifest(target)
        if not manifest:
            raise ValueError("Install OptiScaler with this plugin before adding managed files.")
        _backup_items(target, manifest)
        backups = dict(manifest.get("backups") or {})
        files = list(manifest.get("files") or [])
        added = []

        for source in sources:
            source = Path(source)
            if not source.is_file():
                raise FileNotFoundError(f"missing {source}")
            name = _validate_owned_name(source.name)
            destination = _safe_path(target, name)
            require_stopped_target(target)
            if _path_exists(destination) and name not in backups and name not in files:
                backups[name] = _stash(target, destination, logger)
            atomic_copy(source, destination, target_dir=target)
            if name not in files:
                files.append(name)
            added.append(name)
            if logger:
                logger.info("installed %s", name)

        manifest["files"] = files
        manifest["backups"] = backups
        if REFRAMEWORK_DLL in added:
            manifest["reframework"] = True
            manifest["reframework_revision"] = reframework_revision
        _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)

    return {"ok": True, "path": str(target), "files": added, "tracked": True}


def install_plugin_file(target_dir, source, relative="plugins/OptiPatcher.asi", logger=None):
    """Install and register the optional ASI without touching other plugin files."""
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    relative = _validate_owned_name(relative)
    if relative != f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}":
        raise ValueError(f"unsupported optional plugin path: {relative}")
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError(f"missing {source}")

    recover_target_transactions(target)
    if _preflight_live_control(target):
        raise ValueError("Remove the legacy Decky live ASI before enabling another ASI plugin.")
    require_stopped_target(target)
    with target_transaction(target, require_stopped=True):
        manifest = read_manifest(target)
        if not manifest:
            raise ValueError("Install OptiScaler with this plugin before adding an owned ASI.")
        _backup_items(target, manifest)
        files = list(manifest.get("files") or [])
        backups = dict(manifest.get("backups") or {})
        created_directories = list(manifest.get("created_directories") or [])
        destination = _safe_path(target, relative)
        plugins_dir = destination.parent
        plugins_existed = _path_exists(plugins_dir)
        require_stopped_target(target)
        if _path_exists(destination) and relative not in backups and relative not in files:
            backups[relative] = _stash(target, destination, logger)
        atomic_copy(source, destination, target_dir=target)
        if relative not in files:
            files.append(relative)
        if not plugins_existed and live.PLUGIN_SUBDIR not in created_directories:
            created_directories.append(live.PLUGIN_SUBDIR)
        manifest["files"] = files
        manifest["backups"] = backups
        manifest["created_directories"] = created_directories
        manifest["optipatcher"] = True
        _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)
        if logger:
            logger.info("installed optional plugin %s", relative)
    return {"ok": True, "path": str(_safe_path(target, relative)), "files": [relative],
            "tracked": True}


def remove_files(target_dir, names, logger=None):
    """Take specific files back out, restoring anything they displaced.

    The counterpart to `add_files`, and the reason it exists is Monster Hunter
    Wilds: REFramework is a 23 MB third-party DLL that hooks the engine, and
    when a game stops booting after an install there is no way to find out
    whether that was OptiScaler or REF unless one of them can be taken out on
    its own. Bundling them into a single all-or-nothing install left the user
    with a game that would not start and no way to bisect it.
    """
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"path does not exist: {target}")

    recover_target_transactions(target)
    require_stopped_target(target)
    with target_transaction(target, require_stopped=True):
        manifest = read_manifest(target) or {}
        _backup_items(target, manifest)
        files = list(manifest.get("files") or [])
        backups = dict(manifest.get("backups") or {})
        created_directories = list(manifest.get("created_directories") or [])
        removed = []
        restored = []

        for name in names:
            name = _validate_owned_name(name)
            if name not in files and name not in backups:
                continue
            path = _safe_path(target, name)
            require_stopped_target(target)
            if name in backups:
                stashed = _safe_path(target, f"{BACKUP_DIR}/{name}")
                if not _path_exists(stashed):
                    raise FileNotFoundError(f"recorded original backup is missing: {stashed}")
                _require_path_type(stashed, directory=name in _allowed_dir_names(), allow_missing=False)
            if _path_exists(path):
                _require_path_type(path, directory=False, allow_missing=False)
                path.unlink()
                removed.append(name)
            if name in files:
                files.remove(name)
            # Whatever this displaced goes back, exactly as a full uninstall does.
            if name in backups:
                stashed = _safe_path(target, f"{BACKUP_DIR}/{name}")
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_copy(stashed, path, target_dir=target)
                stashed.unlink()
                restored.append(name)
                if logger:
                    logger.info("restored original %s", name)
                backups.pop(name, None)

        if manifest:
            manifest["files"] = files
            manifest["backups"] = backups
            manifest["created_directories"] = created_directories
            if REFRAMEWORK_DLL in removed:
                manifest["reframework"] = False
                manifest["reframework_revision"] = None
            if f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}" in removed:
                manifest["optipatcher"] = False
            _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)

        plugin_dir = target / live.PLUGIN_SUBDIR
        if (f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}" in removed
                and live.PLUGIN_SUBDIR in created_directories
                and plugin_dir.is_dir() and not plugin_dir.is_symlink()
                and not any(plugin_dir.iterdir())):
            plugin_dir.rmdir()
            created_directories.remove(live.PLUGIN_SUBDIR)
            if manifest:
                manifest["created_directories"] = created_directories
                _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)

    return {"ok": True, "removed": removed, "restored": restored}


def _stash(target, path, logger=None):
    """Move a displaced path into its original-relative backup location."""
    target = Path(target).resolve()
    path = Path(path)
    relative = path.relative_to(target).as_posix()
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise FileNotFoundError(f"cannot preserve missing original path: {path}")
    if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError(f"cannot preserve unsafe original path: {path}")
    is_directory = stat.S_ISDIR(mode)
    if is_directory:
        _assert_plain_tree(path)
    _validate_owned_name(relative, directory=is_directory)
    stash = _safe_path(target, BACKUP_DIR)
    if _path_exists(stash) and (stash.is_symlink() or not stash.is_dir()):
        raise ValueError(f"backup path is not a regular directory: {stash}")
    stash.mkdir(parents=True, exist_ok=True)
    destination = _safe_path(target, f"{BACKUP_DIR}/{relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _path_exists(destination):
        raise FileExistsError(f"an unclaimed backup already exists: {destination}")
    shutil.move(str(path), str(destination))
    if logger:
        logger.info("backed up %s to %s/", relative, BACKUP_DIR)
    return relative


def _sha256_file(path):
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _casefold_root_child(target, name, description):
    """Resolve a root child while rejecting Linux names that alias under Wine."""
    target = Path(target)
    try:
        matches = [item for item in target.iterdir() if item.name.casefold() == name.casefold()]
    except OSError as exc:
        raise ValueError(f"Cannot safely inspect {description}: {exc}") from exc
    if len(matches) > 1:
        raise ValueError(f"Cannot safely inspect multiple Wine-equivalent {description} paths.")
    if matches and matches[0].name != name:
        raise ValueError(f"Cannot safely inspect noncanonical {description} path: {matches[0].name}.")
    return matches[0] if matches else target / name


def _simple_ini_values(path):
    """Read relevant INI keys using SimpleIni's case-insensitive lookup rules."""
    text = Path(path).read_text(encoding="utf-8-sig")
    values = {}
    guarded = {("plugins", "path"), ("libraries", "optidllpath")}
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("[") and "]" in stripped:
            section = stripped[1:stripped.index("]")].strip().casefold()
            continue
        if section is not None and "=" in stripped:
            key, value = stripped.split("=", 1)
            identity = (section, key.strip().casefold())
            if identity in guarded and identity in values:
                raise ValueError(f"OptiScaler.ini repeats a load-path setting: {identity[0]}.{identity[1]}")
            values[identity] = value.strip()
    return values


def _preflight_live_control(target, ini_path=None):
    """Validate the effective search path and identify a removable old ASI.

    ``ini_path`` can be a caller's staged final INI, allowing config edits to
    receive the same check before they are atomically published.
    """
    target = Path(target).resolve()
    installed_ini = _casefold_root_child(target, INI_NAME, "OptiScaler INI")
    plugin_dir = _casefold_root_child(target, live.PLUGIN_SUBDIR, "plugins directory")
    if _path_exists(installed_ini):
        _require_path_type(installed_ini, directory=False, allow_missing=False)
    if _path_exists(plugin_dir):
        _require_path_type(plugin_dir, directory=True, allow_missing=False)

    effective_ini = Path(ini_path) if ini_path is not None else installed_ini
    if _path_exists(effective_ini):
        _require_path_type(effective_ini, directory=False, allow_missing=False)
    settings = _simple_ini_values(effective_ini) if _path_exists(effective_ini) else {}
    plugin_path = (settings.get(("plugins", "path"), "") or "").strip().strip('"').casefold()
    dll_path = (settings.get(("libraries", "optidllpath"), "") or "").strip().strip('"').casefold()

    if plugin_path not in ("", "auto"):
        raise ValueError(
            "Cannot upgrade OptiScaler while Plugins.Path is custom; restore the default plugin path first."
        )
    if dll_path not in ("", "auto"):
        raise ValueError(
            "Cannot upgrade OptiScaler while Libraries.OptiDllPath is custom; restore it to auto first."
        )
    if getattr(constants, "LIVE_CONTROL_SUPPORTED", False):
        return None

    known_hash = getattr(constants, "LEGACY_LIVE_ASI_SHA256", "")
    if not _path_exists(plugin_dir):
        return False
    if plugin_dir.is_symlink() or not plugin_dir.is_dir():
        raise ValueError("Cannot safely inspect the default OptiScaler plugins directory.")

    found_standard = False
    for entry in plugin_dir.iterdir():
        if entry.suffix.casefold() != ".asi":
            continue
        mode = entry.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"Cannot safely inspect ASI plugin: {entry}")
        try:
            actual_hash = _sha256_file(entry)
        except OSError as exc:
            raise ValueError(f"Cannot inspect ASI plugin {entry}: {exc}") from exc
        if actual_hash != known_hash:
            if entry.name.casefold() == live.ASI_NAME.casefold():
                raise ValueError(
                    f"Cannot upgrade OptiScaler because {entry.name} has unknown bytes."
                )
            continue
        if entry.name != live.ASI_NAME:
            raise ValueError(
                f"Cannot upgrade OptiScaler because a renamed legacy live ASI may load: {entry.name}."
            )
        found_standard = True
    return found_standard


def _remove_disabled_live_control(target, backups, old_files, logger=None):
    if getattr(constants, "LIVE_CONTROL_SUPPORTED", False):
        return []
    removed = []
    for name in (f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}", live.CMD_FILE,
                 live.STATUS_FILE, live.LOG_FILE):
        path = _safe_path(target, name)
        if _path_exists(path):
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"Cannot safely preserve live-control file: {path}")
            if name == f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}":
                _assert_known_live_asi(path)
            require_stopped_target(target)
            if name not in backups and name not in old_files:
                backups[name] = _stash(target, path, logger)
                removed.append(name)
                if logger:
                    logger.info("preserved pre-existing live-control path %s", name)
                continue
            _remove_path(path)
            removed.append(name)
            if logger:
                logger.info("removed disabled legacy live-control file %s", name)
    return removed


def _payload_inventory(payload_root):
    payload = Path(payload_root)
    from . import payload as runtime_payload

    state = runtime_payload._inventory_state(payload)
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict) \
            or not state["files"] or not isinstance(state.get("dirs"), list) or not state["dirs"]:
        raise FileNotFoundError(f"payload is incomplete or failed its pinned inventory: {payload}")
    return state


def _copy_payload_dir(source, destination):
    """Overlay pinned payload files into a safe staged copy, then swap trees."""
    source, destination = Path(source), Path(destination)
    target = destination.parent.resolve()
    active = getattr(_TRANSACTIONS, "targets", {})
    if str(target) not in active:
        with target_transaction(target, extra_names=(destination.name,), require_stopped=True):
            return _copy_payload_dir(source, destination)

    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError(f"payload directory is not a real directory: {source}")
    for base, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"payload directory contains an unsafe object: {path}")

    destination_mode = None
    try:
        destination_mode = destination.lstat().st_mode
    except FileNotFoundError:
        pass
    if destination_mode is not None and not stat.S_ISDIR(destination_mode):
        raise ValueError(f"managed payload directory has an unexpected type: {destination}")
    if destination_mode is not None:
        for base, dirs, files in os.walk(destination, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ValueError(f"managed payload tree contains an unsafe object: {path}")

    journal = active[str(target)]["journal"]
    stage_parent = journal / "payload-staging"
    stage_parent.mkdir(exist_ok=True)
    stage = stage_parent / uuid.uuid4().hex
    stage.mkdir()
    staged_tree = stage / destination.name
    moved_old = None
    try:
        if destination_mode is not None:
            shutil.copytree(destination, staged_tree, symlinks=True, copy_function=shutil.copy2)
            for base, dirs, files in os.walk(staged_tree, followlinks=False):
                for name in dirs + files:
                    path = Path(base) / name
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                        raise ValueError(f"managed payload tree changed during staging: {path}")
        else:
            staged_tree.mkdir()
        for base, dirs, files in os.walk(source, followlinks=False):
            relative = Path(base).relative_to(source)
            staged_base = staged_tree / relative
            for name in dirs:
                staged_dir = staged_base / name
                if staged_dir.exists() and not staged_dir.is_dir():
                    raise ValueError(f"payload directory collides with an existing file: {staged_dir}")
                staged_dir.mkdir(exist_ok=True)
            for name in files:
                src = Path(base) / name
                dst = staged_base / name
                if dst.exists() and dst.is_dir():
                    raise ValueError(f"payload file collides with an existing directory: {dst}")
                temporary = dst.with_name(f".{dst.name}.decky-copy-{uuid.uuid4().hex}")
                shutil.copy2(src, temporary)
                os.replace(temporary, dst)

        require_stopped_target(target)
        if destination_mode is not None:
            moved_old = stage_parent / f"old-{uuid.uuid4().hex}"
            os.replace(destination, moved_old)
        try:
            os.replace(staged_tree, destination)
        except Exception:
            if moved_old is not None and _path_exists(moved_old):
                os.replace(moved_old, destination)
                moved_old = None
            raise
        if moved_old is not None:
            shutil.rmtree(moved_old)
            moved_old = None
    finally:
        if moved_old is not None and _path_exists(moved_old) and not _path_exists(destination):
            os.replace(moved_old, destination)
        if stage.exists():
            shutil.rmtree(stage)


def install(target_dir, payload_root, filename=DEFAULT_PROXY, preserve_ini=True,
            logger=None, live_asi=None, optipatcher=None, reframework_files=None,
            reframework_revision=None):
    """Copy OptiScaler into target_dir under the given proxy filename.

    ``live_asi`` is the path to the compiled live-control plugin; when given it
    is installed alongside OptiScaler so settings can be changed while playing.

    ``optipatcher`` is the path to OptiPatcher.asi. It goes in the same plugin
    folder and lets OptiScaler expose DLSS/DLSS-FG inputs in the games it has
    patterns for, without DXGI spoofing.

    ``reframework_files`` are the already-downloaded REFramework files for the
    RE Engine games that cannot host OptiScaler without it. They go in through
    the same ``place`` as everything else, on purpose: that is what puts them in
    the manifest, stashes anything they displace, and takes them back out on
    uninstall. A mod installed off to one side of that machinery is a mod this
    plugin cannot promise to remove.
    """
    if filename not in PROXY_FILENAMES:
        raise ValueError(f"unsupported filename: {filename}")

    target = Path(target_dir).resolve()
    payload = Path(payload_root)
    if not target.is_dir():
        raise NotADirectoryError(f"install target does not exist: {target}")
    if not os.access(target, os.W_OK):
        raise PermissionError(f"no write permission for {target}")
    recover_target_transactions(target)
    require_stopped_target(target)
    _payload_inventory(payload)
    _reject_case_variant_proxies(target)
    source_dll = payload / "OptiScaler.dll"

    old_manifest = read_manifest(target) or {}
    _backup_items(target, old_manifest)
    live_cleanup = _preflight_live_control(target)
    previous = detect(target)
    selected_build = old_manifest.get("fsr4_build") or {}
    if selected_build:
        build = fsr4build.find(selected_build.get("id"))
        current_ffx = _safe_path(target, FFX_UPSCALER_DLL)
        if not build or not current_ffx.is_file() or fsr4build.digest(
                current_ffx) != build["file_sha256"]:
            raise ValueError("Selected FSR 4 build is missing or changed. Enable it again or restore the bundled build first.")

    # An upgrade may be rolled back to this runtime later. Require the complete
    # service-prepared restore point before touching any owned asset.
    if old_manifest and not runtime_current(target):
        from . import restorepoint

        point = restorepoint.info(target)
        if not point.get("available"):
            raise ValueError("Create a complete restore point before upgrading this managed install.")
        restorepoint.validate_metadata(point.get("metadata"))

    with target_transaction(target, require_stopped=True):
        require_stopped_target(target)
        old_manifest = read_manifest(target) or {}
        old_files = set(old_manifest.get("files") or [])
        old_dirs = set(old_manifest.get("dirs") or [])
        backups = dict(old_manifest.get("backups") or {})
        runtime_artifacts_owned = set(old_manifest.get("runtime_artifacts_owned") or [])
        created_directories = set(old_manifest.get("created_directories") or [])
        original_directories = dict(old_manifest.get("original_directories") or {})
        for name in PAYLOAD_DIRS:
            _casefold_root_child(target, name, "managed payload directory")
            if name not in original_directories:
                if name in backups:
                    original = _safe_path(target, f"{BACKUP_DIR}/{name}")
                    mode = _existing_mode(original)
                    if mode is None or not stat.S_ISDIR(mode):
                        raise ValueError(f"recorded original directory is missing or unsafe: {original}")
                    original_directories[name] = {
                        "exists": True, "mode": stat.S_IMODE(mode),
                    }
                elif name in created_directories or name in old_dirs:
                    original_directories[name] = {"exists": False, "mode": None}
                else:
                    path = _safe_path(target, name)
                    mode = _existing_mode(path)
                    if mode is None:
                        original_directories[name] = {"exists": False, "mode": None}
                    elif stat.S_ISDIR(mode):
                        original_directories[name] = {
                            "exists": True, "mode": stat.S_IMODE(mode),
                        }
                    else:
                        raise ValueError(f"managed payload directory has an unexpected type: {path}")
            if original_directories[name]["exists"]:
                created_directories.discard(name)
            else:
                created_directories.add(name)
        patcher_rel = f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}"
        live_rel = f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}"
        plugins_dir = _safe_path(target, live.PLUGIN_SUBDIR)
        plugins_dir_existed_before = _path_exists(plugins_dir)

        # 0.9.4 did not register OptiPatcher in ``files`` even though it set the
        # flag. Carry forward this known prior ownership record on reinstall.
        if old_manifest.get("optipatcher") and _path_exists(_safe_path(target, patcher_rel)):
            old_files.add(patcher_rel)

        removed_live = _remove_disabled_live_control(target, backups, old_files, logger)
        for name in removed_live:
            old_files.discard(name)

        stale_removed = set()
        # Remove a previous proxy under another filename. Its oldest displaced
        # game DLL stays in BACKUP_DIR; a copy is returned to the old location.
        for stale in previous.get("candidates", []):
            if stale == filename:
                continue
            stale_path = _safe_path(target, stale)
            if not _looks_like_optiscaler(stale_path):
                continue
            require_stopped_target(target)
            was_managed = stale in old_files
            if not was_managed and stale not in backups:
                # A hand-installed OptiScaler is still the user's original
                # runtime. Preserve its exact bytes before removing a duplicate.
                backups[stale] = _stash(target, stale_path, logger)
                stale_removed.add(stale)
                continue
            _remove_path(stale_path)
            stale_removed.add(stale)
            if stale in backups:
                stashed = _safe_path(target, f"{BACKUP_DIR}/{stale}")
                if not _path_exists(stashed):
                    raise FileNotFoundError(f"recorded original proxy backup is missing: {stashed}")
                if not _looks_like_optiscaler(stashed):
                    atomic_copy(stashed, stale_path, target_dir=target)
                    if logger:
                        logger.info("restored native game proxy %s while moving proxy", stale)
                elif logger:
                    logger.info("kept prior OptiScaler proxy %s in backup until Remove", stale)
            elif logger:
                logger.info("removed previous OptiScaler proxy %s", stale)

        created = []
        created_dirs = []

        def place(source, relative_name):
            relative_name = _validate_owned_name(relative_name)
            source = Path(source)
            if not source.is_file() or source.is_symlink():
                raise FileNotFoundError(f"required install source missing: {source}")
            destination = _safe_path(target, relative_name)
            require_stopped_target(target)
            if _path_exists(destination) and not stat.S_ISREG(destination.lstat().st_mode):
                raise ValueError(f"managed file has an unexpected filesystem type: {destination}")
            if (_path_exists(destination) and relative_name not in backups
                    and relative_name not in old_files
                    ):
                backups[relative_name] = _stash(target, destination, logger)
            atomic_copy(source, destination, target_dir=target)
            if relative_name not in created:
                created.append(relative_name)

        place(source_dll, filename)

        ini_path = _safe_path(target, INI_NAME)
        ini_existed = _path_exists(ini_path)
        if ini_existed and INI_NAME not in backups and INI_NAME not in old_files:
            backups[INI_NAME] = _stash(target, ini_path, logger)
            if preserve_ini:
                original_ini = _safe_path(target, f"{BACKUP_DIR}/{INI_NAME}")
                atomic_copy(original_ini, ini_path, target_dir=target)
        if not (preserve_ini and ini_existed):
            atomic_copy(payload / INI_NAME, ini_path, target_dir=target)
        if INI_NAME not in created:
            created.append(INI_NAME)

        for name in PAYLOAD_FILES:
            if name == FFX_UPSCALER_DLL and selected_build:
                if name not in created:
                    created.append(name)
                continue
            place(payload / name, name)

        nested_payload_files = _payload_nested_names(payload)
        for name in PAYLOAD_DIRS:
            source = payload / name
            destination = _safe_path(target, name)
            destination_mode = _existing_mode(destination)
            if destination_mode is not None and not stat.S_ISDIR(destination_mode):
                raise ValueError(f"managed payload directory has an unexpected type: {destination}")
            if destination_mode is not None and name not in backups:
                _assert_plain_tree(destination)
                for relative in nested_payload_files:
                    if not relative.startswith(name + "/"):
                        continue
                    nested_path = _safe_path(target, relative)
                    nested_mode = _existing_mode(nested_path)
                    if nested_mode is None:
                        continue
                    if not stat.S_ISREG(nested_mode):
                        raise ValueError(f"managed payload file has an unexpected type: {nested_path}")
                    if relative not in backups and relative not in old_files and name not in old_dirs:
                        backups[relative] = _stash(target, nested_path, logger)
            require_stopped_target(target)
            _copy_payload_dir(source, destination)
            if name not in created_dirs:
                created_dirs.append(name)

        for name in nested_payload_files:
            if name not in old_files:
                old_files.add(name)

        # Existing output/config files are user data unless an earlier manifest
        # already proved that they are plugin-created. Keep their oldest bytes
        # before the new runtime is allowed to append or overwrite them.
        for name in RUNTIME_ARTIFACTS:
            path = _safe_path(target, name)
            if _path_exists(path):
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError(f"runtime artifact has an unexpected filesystem type: {path}")
                if name not in runtime_artifacts_owned and name not in backups and name not in old_files:
                    backups[name] = _stash(target, path, logger)
                    original = _safe_path(target, f"{BACKUP_DIR}/{name}")
                    atomic_copy(original, path, target_dir=target)
            elif name not in backups and name not in old_files:
                runtime_artifacts_owned.add(name)
            elif name in backups:
                original = _safe_path(target, f"{BACKUP_DIR}/{name}")
                _require_path_type(original, directory=False, allow_missing=False)
                atomic_copy(original, path, target_dir=target)

        # REFramework files, if supplied, are placed and tracked in the same
        # transaction as the OptiScaler runtime.
        ref_result = {"installed": False, "files": [], "error": None,
                      "revision": reframework_revision}
        for source in reframework_files or []:
            source = Path(source)
            if not source.is_file():
                raise FileNotFoundError(f"missing {source}")
            place(source, source.name)
            ref_result["files"].append(source.name)
        ref_result["installed"] = REFRAMEWORK_DLL in ref_result["files"]
        if not ref_result["installed"] and old_manifest.get("reframework"):
            ref_result["installed"] = _path_exists(_safe_path(target, REFRAMEWORK_DLL))
            ref_result["revision"] = old_manifest.get("reframework_revision")

        live_result = {"installed": False, "error": None}
        if not getattr(constants, "LIVE_CONTROL_SUPPORTED", False):
            live_result["error"] = getattr(
                constants, "LIVE_CONTROL_REASON", "Live control is disabled for this runtime."
            )
            if logger:
                logger.info("live control disabled: %s", live_result["error"])
        elif live_asi:
            outcome = live.install_asi(target, live_asi, logger)
            live_result["installed"] = bool(outcome.get("ok"))
            live_result["error"] = outcome.get("error")
            if live_result["installed"]:
                created.append(live_rel)

        # Optional OptiPatcher is installed through the same ownership and
        # backup path as every other managed file. Existing ownership survives
        # an ordinary reinstall when it is not selected again.
        patcher_result = {"installed": False, "error": None}
        if optipatcher:
            patcher_source = Path(optipatcher)
            if not patcher_source.is_file():
                patcher_result["error"] = f"{OPTIPATCHER_NAME} missing at {patcher_source}"
            else:
                place(patcher_source, patcher_rel)
                patcher_result["installed"] = True
        elif old_manifest.get("optipatcher") and _path_exists(_safe_path(target, patcher_rel)):
            patcher_result["installed"] = True

        will_create_plugins = bool(live_result["installed"] or patcher_result["installed"])
        if will_create_plugins:
            if plugins_dir_existed_before:
                mode = plugins_dir.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ValueError(f"plugins path is not a regular directory: {plugins_dir}")
            else:
                created_directories.add(live.PLUGIN_SUBDIR)

        files = [name for name in old_files if name not in stale_removed and name != live_rel]
        for name in created:
            if name not in files:
                files.append(name)
        for name in _payload_nested_names(payload):
            if name not in files:
                files.append(name)
        dirs = list(old_dirs)
        for name in created_dirs:
            if name not in dirs:
                dirs.append(name)
        manifest = {
            **old_manifest,
            "plugin": "decky-optiscaler",
            "live_asi": False,
            "optipatcher": patcher_result["installed"],
            "reframework": ref_result["installed"],
            "reframework_revision": ref_result["revision"],
            "fsr4_build": selected_build,
            "optiscaler_version": OPTISCALER_VERSION,
            "filename": filename,
            "installed_at": time.time(),
            "files": files,
            "dirs": dirs,
            "backups": backups,
            "runtime_artifacts_owned": sorted(runtime_artifacts_owned),
            "created_directories": sorted(created_directories),
            "original_directories": original_directories,
            "backup_dir": BACKUP_DIR,
        }
        validate_manifest(manifest)
        _atomic_json(_safe_path(target, MANIFEST_NAME), manifest)

    return {
        "path": str(target),
        "filename": filename,
        "version": OPTISCALER_VERSION,
        "ini_preserved": bool(preserve_ini and ini_existed),
        "optipatcher": patcher_result,
        "reframework": ref_result,
        "launch_option": launch_option(filename),
        "files": files,
        "dirs": dirs,
        "backups": backups,
        "backup_dir": str(backup_dir(target)),
        "fsr4": fsr4_status(target),
        "live": live_result,
    }


def launch_option(filename):
    """Steam launch options needed for Wine to load the proxy dll."""
    if filename.lower().endswith(".asi"):
        return "%command%"  # .asi is loaded by an ASI loader, no override needed
    stem = filename[:-4] if filename.lower().endswith(".dll") else filename
    return f'WINEDLLOVERRIDES="{stem}=n,b" %command%'


def uninstall(target_dir, remove_ini=True, logger=None):
    """Remove OptiScaler, restoring everything it displaced."""
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"path does not exist: {target}")
    recover_target_transactions(target)
    require_stopped_target(target)
    manifest = read_manifest(target)
    _backup_items(target, manifest)
    legacy_names = [path.name[:-len(LEGACY_BACKUP_SUFFIX)]
                    for path in target.glob("*" + LEGACY_BACKUP_SUFFIX)]
    for name in legacy_names:
        _validate_owned_name(name)

    with target_transaction(target, extra_names=(RESTORE_DIR, *legacy_names),
                            require_stopped=True):
        manifest = read_manifest(target)
        removed = []
        restored = []

        if manifest:
            original_directories = manifest.get("original_directories", {})
            for name in manifest.get("files", []):
                if name == INI_NAME and not remove_ini and INI_NAME not in manifest.get("backups", {}):
                    continue
                path = _safe_path(target, name)
                require_stopped_target(target)
                if _require_path_type(path, directory=False):
                    path.unlink()
                    removed.append(name)
            for name in manifest.get("dirs", []):
                path = _safe_path(target, name)
                require_stopped_target(target)
                original = original_directories.get(name)
                if (original and original.get("exists")) or name in manifest.get("backups", {}):
                    continue
                if _require_path_type(path, directory=True):
                    # Only files listed by ownership are deleted. An empty
                    # managed directory can then be removed; user additions
                    # keep their parent in place.
                    if not any(path.iterdir()):
                        path.rmdir()
                        removed.append(name + "/")
            # Older manifests owned the bundled trees as a whole. Remove only
            # the pinned children from those trees; never recurse over extras.
            nested_paths = _payload_nested_names()
            for directory in PAYLOAD_DIRS:
                if directory not in manifest.get("dirs", []):
                    continue
                for name in nested_paths:
                    if not name.startswith(directory + "/") or name in manifest.get("files", []):
                        continue
                    path = _safe_path(target, name)
                    if _require_path_type(path, directory=False):
                        path.unlink()
                        removed.append(name)
                directory_path = _safe_path(target, directory)
                original = original_directories.get(directory)
                if (not (original and original.get("exists"))
                        and directory not in manifest.get("backups", {})
                        and _require_path_type(directory_path, directory=True)
                        and not any(directory_path.iterdir())):
                    directory_path.rmdir()
                    removed.append(directory + "/")
        else:
            # No ownership manifest means there is no proof over support
            # libraries, INI, logs, ASIs or generic runtime directories. Remove
            # only a core proxy positively identified by its image marker.
            for name in PROXY_FILENAMES:
                path = _safe_path(target, name)
                if _path_exists(path) and path.is_file() and _looks_like_optiscaler(path):
                    require_stopped_target(target)
                    path.unlink()
                    removed.append(name)

        owned_live_asi = bool(manifest and (
            f"{live.PLUGIN_SUBDIR}/{live.ASI_NAME}" in manifest.get("files", [])
            or manifest.get("live_asi")
        ))
        for name in live.remove_asi(
                target, logger, allow_owned_asi=owned_live_asi,
                owned_paths=manifest.get("files", []) if manifest else ()).get("removed", []):
            if name not in removed:
                removed.append(name)

        runtime_owned = set(manifest.get("runtime_artifacts_owned", [])) if manifest else set()
        for name in runtime_owned:
            path = _safe_path(target, name)
            if _require_path_type(path, directory=False):
                require_stopped_target(target)
                path.unlink()
                removed.append(name)

        if manifest and forget_launch_options(target):
            removed.append(LAUNCH_RECORD_NAME)

        # Older manifests claimed this plugin without listing its path.
        if (manifest and manifest.get("optipatcher")
                and f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}" not in manifest.get("files", [])):
            patcher = _safe_path(target, f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}")
            if _require_path_type(patcher, directory=False):
                patcher.unlink()
                removed.append(f"{live.PLUGIN_SUBDIR}/{OPTIPATCHER_NAME}")

        restored.extend(restore_backups(target, logger))

        if manifest:
            for name, record in manifest.get("original_directories", {}).items():
                path = _safe_path(target, name)
                mode = _existing_mode(path)
                if record["exists"]:
                    if mode is None:
                        path.mkdir(parents=True, exist_ok=True)
                        mode = _existing_mode(path)
                    if mode is None or not stat.S_ISDIR(mode):
                        raise ValueError(f"original payload directory cannot be restored: {path}")
                    os.chmod(path, record["mode"])
                elif mode is not None and stat.S_ISDIR(mode) and not any(path.iterdir()):
                    path.rmdir()

        manifest_path = _safe_path(target, MANIFEST_NAME)
        if manifest and _require_path_type(manifest_path, directory=False):
            manifest_path.unlink()
            removed.append(MANIFEST_NAME)

        if manifest:
            from . import restorepoint

            restorepoint.remove(target)

        if manifest:
            for name in manifest.get("created_directories", []):
                original = manifest.get("original_directories", {}).get(name)
                if original and original.get("exists"):
                    continue
                path = _safe_path(target, name)
                if _require_path_type(path, directory=True) and not any(path.iterdir()):
                    path.rmdir()
                    removed.append(name + "/")

    if logger:
        logger.info(
            "uninstalled OptiScaler from %s (%d removed, %d restored)",
            target, len(removed), len(restored),
        )
    return {
        "path": str(target),
        "removed": removed,
        "restored": restored,
        "backup_dir": str(backup_dir(target)),
    }


def restore_backups(target_dir, logger=None):
    """Restore validated originals and fail if any backup cannot be restored."""
    target = Path(target_dir).resolve()
    if not target.is_dir():
        raise NotADirectoryError(f"path does not exist: {target}")
    recover_target_transactions(target)
    require_stopped_target(target)
    manifest = read_manifest(target)
    items = _backup_items(target, manifest)
    legacy_paths = list(target.glob("*" + LEGACY_BACKUP_SUFFIX))
    extra = []
    for path in legacy_paths:
        relative = _legacy_backup_target(path.name)
        if relative is None:
            raise ValueError(f"unsupported legacy backup path: {path.name}")
        _require_path_type(path, directory=False, allow_missing=False)
        extra.extend((path.name, relative))

    restored = []
    with target_transaction(target, extra_names=extra, require_stopped=True):
        for relative, source in items:
            destination = _safe_path(target, relative)
            require_stopped_target(target)
            is_directory = relative in _allowed_dir_names()
            if is_directory:
                _require_path_type(destination, directory=True)
                prefix = relative + "/"
                replace_files = {name[len(prefix):] for name in _allowed_file_names()
                                 if name.startswith(prefix)}
                _merge_directory_tree(source, destination, target_dir=target,
                                      replace_files=replace_files)
            else:
                _require_path_type(destination, directory=False)
                atomic_copy(source, destination, target_dir=target)
            _remove_path(source)
            restored.append(relative)
            if logger:
                logger.info("restored %s from %s/", relative, BACKUP_DIR)

        for path in legacy_paths:
            relative = _legacy_backup_target(path.name)
            if relative is None:
                raise ValueError(f"unsupported legacy backup path: {path.name}")
            destination = _safe_path(target, relative)
            require_stopped_target(target)
            _require_path_type(path, directory=False, allow_missing=False)
            _require_path_type(destination, directory=False)
            atomic_copy(path, destination, target_dir=target)
            path.unlink()
            restored.append(relative)

        stash = _safe_path(target, BACKUP_DIR)
        if _path_exists(stash):
            _require_path_type(stash, directory=True, allow_missing=False)
            shutil.rmtree(stash)
    return restored


def _backup_items(target, manifest=None):
    """Preflight and inventory original backup paths without following links."""
    stash = _safe_path(target, BACKUP_DIR)
    if not _path_exists(stash):
        if manifest and manifest.get("backups"):
            raise FileNotFoundError(f"backup directory is missing: {stash}")
        return []
    if stash.is_symlink() or not stash.is_dir():
        raise ValueError(f"backup path is not a regular directory: {stash}")

    entries = []
    declared = manifest.get("backups", {}) if manifest else {}
    for relative in declared:
        is_directory = relative in _allowed_dir_names()
        source = _safe_path(target, f"{BACKUP_DIR}/{relative}")
        if not _path_exists(source):
            raise FileNotFoundError(f"original backup is missing: {source}")
        if is_directory:
            _assert_plain_tree(source)
        else:
            _require_path_type(source, directory=False, allow_missing=False)
        entries.append((relative, source))

    for item in sorted(stash.iterdir()):
        if item.name == live.PLUGIN_SUBDIR:
            if item.is_symlink() or not item.is_dir():
                raise ValueError(f"invalid nested plugin backup: {item}")
            # The plugins folder is normally only a namespace for individually
            # stashed ASIs. Do not merge that namespace back as a whole tree.
            if any(name == live.PLUGIN_SUBDIR for name, _ in entries):
                continue
            for child in sorted(item.iterdir()):
                relative = f"{item.name}/{child.name}"
                _validate_owned_name(relative)
                _require_path_type(child, directory=False, allow_missing=False)
                if not any(name == relative for name, _ in entries):
                    entries.append((relative, child))
        elif item.name in PAYLOAD_DIRS:
            if item.is_symlink() or not item.is_dir():
                raise ValueError(f"invalid nested payload backup: {item}")
            # A directory containing file-level backups is a namespace, while
            # an explicitly declared directory backup remains a legacy tree.
            if any(name == item.name for name, _ in entries):
                continue
            _assert_plain_tree(item)
            for base, _, files in os.walk(item, followlinks=False):
                for filename in files:
                    child = Path(base) / filename
                    relative = child.relative_to(stash).as_posix()
                    _validate_owned_name(relative)
                    _require_path_type(child, directory=False, allow_missing=False)
                    if not any(name == relative for name, _ in entries):
                        entries.append((relative, child))
        elif item.name in _allowed_dir_names():
            if not any(name == item.name for name, _ in entries):
                _validate_owned_name(item.name, directory=True)
                _assert_plain_tree(item)
                entries.append((item.name, item))
        else:
            _validate_owned_name(item.name)
            _require_path_type(item, directory=False, allow_missing=False)
            if not any(name == item.name for name, _ in entries):
                entries.append((item.name, item))

    plugin_root = stash / live.PLUGIN_SUBDIR
    if _path_exists(plugin_root):
        if plugin_root.is_symlink() or not plugin_root.is_dir():
            raise ValueError(f"invalid nested plugin backup: {plugin_root}")
        if not any(name == live.PLUGIN_SUBDIR for name, _ in entries):
            allowed = {name for name, _ in entries if name.startswith(live.PLUGIN_SUBDIR + "/")}
            for child in plugin_root.iterdir():
                if f"{live.PLUGIN_SUBDIR}/{child.name}" not in allowed:
                    raise ValueError(f"unrecognized backup item: {live.PLUGIN_SUBDIR}/{child.name}")
    return entries


def pe_file_version(path):
    """Read a PE file's VS_FIXEDFILEINFO version, or None.

    Used to report which FidelityFX SDK — and therefore which FSR version — is
    actually sitting in the game folder.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    marker = data.find(b"\xbd\x04\xef\xfe")
    if marker < 0 or marker + 16 > len(data):
        return None
    import struct

    try:
        minor, major, build, patch = struct.unpack("<HHHH", data[marker + 8 : marker + 16])
    except struct.error:
        return None
    return (major, minor, patch, build)


def verify_install(target_dir, payload_root):
    """Check that every bundled file really landed in the game folder.

    Answers the practical question "did the install actually override what the
    game shipped?" by comparing size and content against the payload.
    """
    target = Path(target_dir)
    payload = Path(payload_root)
    from . import payload as runtime_payload

    manifest = read_manifest(target)
    entries = []

    inventory = runtime_payload.required_inventory()
    nested = _payload_nested_names(payload)
    expected = list(PAYLOAD_FILES) + nested
    if manifest and manifest.get("filename"):
        expected.insert(0, manifest["filename"])

    for name in expected:
        source_name = ("OptiScaler.dll" if manifest and name == manifest.get("filename") else name)
        source = payload / source_name
        destination = _safe_path(target, name)
        expected_record = inventory["files"].get(source_name)
        record = {
            "name": name,
            "present": _path_exists(destination),
            "matches_payload": False,
            "size": destination.stat().st_size if destination.is_file() else 0,
            "expected_size": expected_record.get("bytes", 0) if expected_record else 0,
        }
        if record["present"] and expected_record:
            record["matches_payload"] = runtime_payload.matches_file(destination, expected_record)
        if name == FFX_UPSCALER_DLL:
            selected = (manifest or {}).get("fsr4_build") or {}
            build = fsr4build.find(selected.get("id", "bundled"))
            record["matches_payload"] = bool(
                record["present"] and build
                and fsr4build.digest(destination) == build["file_sha256"])
            if build and build.get("file_bytes"):
                record["expected_size"] = build["file_bytes"]
        entries.append(record)

    for name in PAYLOAD_DIRS:
        destination = target / name
        matches = runtime_payload.subtree_matches(
            target, [name], inventory["files"], allow_extra=True
        )
        entries.append({
            "name": name + "/",
            "present": destination.is_dir(),
            "matches_payload": matches,
            "size": 0,
            "expected_size": 0,
        })

    ffx = target / FFX_UPSCALER_DLL
    version = pe_file_version(ffx) if ffx.is_file() else None
    fsr4_capable = bool(version and version[:3] >= FSR4_MIN_SDK_VERSION[:3])

    problems = [e["name"] for e in entries if not e["present"] or not e["matches_payload"]]
    return {
        "path": str(target),
        "files": entries,
        "problems": problems,
        "complete": not problems,
        "ffx_upscaler": {
            "present": ffx.is_file(),
            "version": ".".join(str(n) for n in version) if version else None,
            "fsr4_capable": fsr4_capable,
        },
    }
