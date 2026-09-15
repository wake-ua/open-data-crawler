import json
import os
import shutil
import tempfile
import time
import re
import fcntl
from contextlib import contextmanager

from opendatacrawler.setup_logger import log_manager

logger = log_manager.log

ATOMIC_TEMP_PREFIX = f".odc_tmp_{os.getpid()}_"
TABULAR_TEMP_PREFIX = f"odc_tabular_{os.getpid()}_"

def get_disk_usage(path):
    target = path
    target = os.path.abspath(target)
    while not os.path.isdir(target):
        target = os.path.dirname(target)
    return shutil.disk_usage(target)

def has_enough_disk_space(path, required_bytes=0, min_free_bytes=0, min_free_percent=0.0):
    usage = get_disk_usage(path)
    required_free = max(int(required_bytes or 0) + int(min_free_bytes or 0), 0)

    if usage.free < required_free:
        return False, usage

    if min_free_percent and usage.total > 0:
        free_percent = (usage.free / usage.total) * 100
        if free_percent < float(min_free_percent):
            return False, usage

    return True, usage

def create_folder(path):
    if os.path.exists(path):
        logger("...", f"The directory '{path}' already exists, skipping creation...")
        return os.path.isdir(path)

    try:
        os.makedirs(path, exist_ok=True)
        logger("OK", f"Successfully created the directory '{path}'")
        return True
    except OSError as e:
        logger("ERROR", f"Failed to create the directory '{path}'", e)
        return False

def delete_tempfiles(file_paths, keep_path=None):
    for path in file_paths:
        if path != keep_path and path and os.path.exists(path):
            os.remove(path)

def cleanup_path_tempfiles(root_path, older_than_seconds=5 * 60):
    if not root_path or not os.path.exists(root_path):
        return 0

    removed = 0
    now = time.time()
    for current_root, _, files in os.walk(root_path):
        for name in files:
            if not orphan_owned_temp(name):
                continue
            path = os.path.join(current_root, name)
            try:
                if older_than_seconds and now - os.path.getmtime(path) < older_than_seconds:
                    continue
                os.remove(path)
                removed += 1
            except Exception:
                pass
    return removed

def cleanup_system_tempfiles(older_than_seconds=24 * 3600):
    temp_dir = tempfile.gettempdir()
    now = time.time()
    removed = 0

    try:
        for name in os.listdir(temp_dir):
            if not orphan_owned_temp(name):
                continue

            path = os.path.join(temp_dir, name)
            try:
                if not os.path.isfile(path):
                    continue
                if older_than_seconds and now - os.path.getmtime(path) < older_than_seconds:
                    continue
                os.remove(path)
                removed += 1
            except Exception:
                pass
    except Exception:
        return 0

    return removed

def atomic_dump_json(path, data, **json_kwargs):
    temp_path = None
    try:
        directory = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=directory, prefix=ATOMIC_TEMP_PREFIX) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, **json_kwargs)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        os.replace(temp_path, path)
        return True
    except Exception as e:
        logger("ERROR", f"Failed to atomically write JSON file '{path}'", e)
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        return False

def atomic_write_bytes(path, data):
    temp_path = None
    try:
        directory = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=directory, prefix=ATOMIC_TEMP_PREFIX) as temp_file:
            temp_path = temp_file.name
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name

        os.replace(temp_path, path)
        return True
    except Exception as e:
        logger("ERROR", f"Failed to atomically write file '{path}'", e)
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        return False

def is_empty_file(path):
    try:
        return os.path.getsize(path) == 0
    except Exception as e:
        logger("ERROR", f"Could not check file size for '{path}'", e)
        return True

def orphan_owned_temp(name):
    match = re.match(r"(?:\.odc_tmp_|odc_tabular_)([0-9]+)_", name)
    if not match:
        return False
    try:
        os.kill(int(match.group(1)), 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        pass
    return False

@contextmanager
def file_lock(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    remove_lock_file = False
    with open(path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            remove_lock_file = True
        except BlockingIOError as error:
            raise RuntimeError(f"Another crawl is using {path}") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    if remove_lock_file:
        try:
            os.remove(path)
            os.rmdir(os.path.dirname(os.path.abspath(path)))
        except (FileNotFoundError, OSError):
            pass
