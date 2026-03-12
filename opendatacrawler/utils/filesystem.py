import json
import os
import tempfile

from opendatacrawler.setup_logger import log_manager


logger = log_manager.log


def create_folder(path):
    if os.path.exists(path):
        logger("...", f"The directory '{path}' already exists, skipping creation...")
        return True

    try:
        os.makedirs(path, exist_ok=False)
        logger("OK", f"Successfully created the directory '{path}'")
        return True
    except OSError as e:
        logger("ERROR", f"Failed to create the directory '{path}'", e)
        return False


def delete_tempfiles(file_paths, keep_path=None):
    for path in file_paths:
        if path != keep_path and path and os.path.exists(path):
            os.remove(path)


def atomic_dump_json(path, data, **json_kwargs):
    temp_path = None
    try:
        directory = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=directory) as temp_file:
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
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=directory) as temp_file:
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
