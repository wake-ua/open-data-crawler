import json
import os
from concurrent.futures import ThreadPoolExecutor

from opendatacrawler.setup_logger import log_manager
from .tabular import get_mime_and_ext


logger = log_manager.log

PERMANENT_UNAVAILABLE_TAGS = {
    "resource_removed",
    "unresolvable_domain",
    "method_not_allowed",
    "missing_resource",
    "forbidden_resource",
    "ssl_error",
    "invalid_request",
    "invalid_download_url",
    "invalid_redirect_location",
}


def package_requires_retry(failed, pending):
    return bool(failed or pending)


def recover_resume(save_path, accepted_types=None, num_resources=None, max_workers=None):
    packages_status = {}
    total_failed = []
    total_successful = []
    total_unavailable_permanent = []
    incomplete_packages = set()
    save_path = os.path.abspath(save_path)
    cwd = os.getcwd()
    relative_save_path = os.path.relpath(save_path, cwd)
    existing_paths = set()
    meta_files = []

    for entry in os.scandir(save_path):
        if not entry.is_file():
            continue
        if entry.name.startswith("meta_") and entry.name.endswith(".json"):
            meta_files.append(entry.name)
            continue

        existing_paths.add(entry.path)
        existing_paths.add(os.path.join(relative_save_path, entry.name))

    total_meta_files = len(meta_files)
    workers = max_workers if max_workers is not None else min(16, max(4, (os.cpu_count() or 1) * 2))
    workers = max(1, workers)
    use_threads = workers > 1 and total_meta_files > 1000

    if use_threads:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="resume") as executor:
            for identifier, entry in executor.map(lambda fname: load_resume_entry(save_path, fname), meta_files):
                if entry is None:
                    incomplete_packages.add(identifier)
                    packages_status[identifier] = {"failed_resources": [], "successful_resources": [], "unavailable_permanent": [], "pending_resources": []}
                    continue

                failed, success, unavailable_permanent, pending = compute_package_resume_status(entry, accepted_types, num_resources, existing_paths, save_path)

                if package_requires_retry(failed, pending):
                    incomplete_packages.add(identifier)

                total_failed.extend(failed)
                total_successful.extend(success)
                total_unavailable_permanent.extend(unavailable_permanent)

                packages_status[identifier] = {
                    "failed_resources": failed,
                    "successful_resources": success,
                    "unavailable_permanent": unavailable_permanent,
                    "pending_resources": pending,
                }
    else:
        for fname in meta_files:
            identifier, entry = load_resume_entry(save_path, fname)
            if entry is None:
                incomplete_packages.add(identifier)
                packages_status[identifier] = {"failed_resources": [], "successful_resources": [], "unavailable_permanent": [], "pending_resources": []}
                continue

            failed, success, unavailable_permanent, pending = compute_package_resume_status(entry, accepted_types, num_resources, existing_paths, save_path)

            if package_requires_retry(failed, pending):
                incomplete_packages.add(identifier)

            total_failed.extend(failed)
            total_successful.extend(success)
            total_unavailable_permanent.extend(unavailable_permanent)

            packages_status[identifier] = {
                "failed_resources": failed,
                "successful_resources": success,
                "unavailable_permanent": unavailable_permanent,
                "pending_resources": pending,
            }

    return packages_status, total_successful, total_failed, total_unavailable_permanent, incomplete_packages


def load_resume_entry(save_path, fname):
    meta_path = os.path.join(save_path, fname)
    identifier = fname.replace("meta_", "").replace(".json", "")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        identifier = meta.get("identifier") or identifier
        return identifier, build_resume_entry(meta)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger("ERROR", f"Could not decode {meta_path}; deleting damaged metadata so it can be rebuilt", e)
        try:
            os.remove(meta_path)
            logger("DEL", f"Deleted damaged metadata file '{meta_path}'")
        except Exception as delete_error:
            logger("ERROR", f"Could not delete damaged metadata file '{meta_path}'", delete_error)
        return identifier, None
    except Exception as e:
        logger("ERROR", f"Could not read {meta_path}; keeping metadata file and marking package as pending for safety", e)
        return identifier, None


def compute_package_resume_status(entry, accepted_types=None, num_resources=None, existing_paths=None, save_path=None):
    failed = []
    success = []
    unavailable_permanent = []
    pending = []
    considered_resources = []

    for file_name, resource in entry.get("resources", {}).items():
        ext = file_name.split(".")[-1].lower() if "." in file_name else None
        inferred_ext = ext
        if not inferred_ext:
            _, inferred_ext = get_mime_and_ext(resource.get("mediaType"))

        was_attempted = bool(resource.get("fileCrawled")) or bool(resource.get("fileInfoKeys"))
        if accepted_types:
            if inferred_ext in accepted_types:
                considered_resources.append(file_name)
                continue
            if was_attempted and inferred_ext:
                continue

        considered_resources.append(file_name)

    if considered_resources and num_resources:
        pending_failed = []
        pending_unavailable = []
        pending_unattempted = []
        for file_name in considered_resources:
            resource = entry.get("resources", {}).get(file_name, {})
            if is_completed(entry, file_name, complete=False, unavailable_permanent=True):
                pending_unavailable.append(file_name)
                continue

            if is_completed(entry, file_name) and has_materialized_file(resource, existing_paths, save_path):
                success.append(file_name)
                if len(success) >= num_resources:
                    return [], success, [], []
                continue

            was_attempted = bool(resource.get("fileCrawled")) or bool(resource.get("fileInfoKeys"))
            if was_attempted:
                pending_failed.append(file_name)
            else:
                pending_unattempted.append(file_name)

        if len(success) < num_resources:
            failed = pending_failed
            unavailable_permanent = pending_unavailable
            pending = pending_unattempted
        return failed, success, unavailable_permanent, pending

    for file_name in considered_resources:
        resource = entry.get("resources", {}).get(file_name, {})
        if is_completed(entry, file_name, complete=False, unavailable_permanent=True):
            unavailable_permanent.append(file_name)
        elif is_completed(entry, file_name) and has_materialized_file(resource, existing_paths, save_path):
            success.append(file_name)
        else:
            was_attempted = bool(resource.get("fileCrawled")) or bool(resource.get("fileInfoKeys"))
            if was_attempted:
                failed.append(file_name)
            else:
                pending.append(file_name)

    return failed, success, unavailable_permanent, pending


def build_resume_entry(package, meta_file_name=None, meta_stat=None):
    resources = {}
    resources_info = package.get("crawlerInfo", {}).get("resourcesInfo", {})
    for file_name, resource in package.get("resources", {}).items():
        info = resources_info.get(file_name, {})
        file_status = info.get("fileStatus", {})
        file_info = info.get("fileInfo", {})
        resources[file_name] = {
            "mediaType": resource.get("mediaType"),
            "path": resource.get("path"),
            "fileCompleted": bool(file_status.get("fileCompleted")),
            "fileCrawled": bool(file_status.get("fileCrawled")),
            "fileInfoKeys": list(file_info.keys()),
        }

    return {"identifier": package.get("identifier"), "resources": resources}


def has_materialized_file(resource, existing_paths=None, save_path=None):
    path = resource.get("path")
    if not path:
        return False

    abs_path = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    if existing_paths is not None and (path in existing_paths or abs_path in existing_paths):
        return True
    if save_path and not os.path.isabs(path):
        alt_path = os.path.join(save_path, os.path.basename(path))
        if existing_paths is not None and alt_path in existing_paths:
            return True
        if os.path.exists(alt_path):
            return True
    return os.path.exists(abs_path)


def is_completed(package, file_name, complete=True, unavailable=False, unavailable_permanent=False):
    if file_name:
        try:
            if "crawlerInfo" in package:
                info = package["crawlerInfo"]["resourcesInfo"].get(file_name, {})
                file_status = info.get("fileStatus", {})
                file_info = info.get("fileInfo", {})
                file_completed = file_status.get("fileCompleted")
                file_info_keys = file_info.keys()
            else:
                info = package.get("resources", {}).get(file_name, {})
                file_completed = info.get("fileCompleted")
                file_info_keys = info.get("fileInfoKeys", [])

            if complete and file_completed:
                return True

            if unavailable and "resource_temporarily_unavailable" in file_info_keys:
                return True

            if unavailable_permanent and any(tag in file_info_keys for tag in PERMANENT_UNAVAILABLE_TAGS):
                return True

            return False
        except Exception as e:
            logger("WARNING", f"Missing or invalid status for '{file_name}': {e}")
            return False

    try:
        return bool(package["crawlerInfo"]["packageStatus"].get("packageCompleted", False))
    except Exception as e:
        logger("WARNING", f"Missing or invalid status for '{package.get('fileName')}': {e}")
        return False
