import json
import os

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
}


def recover_resume(save_path, accepted_types=None, num_resources=None):
    packages_status = {}
    total_failed = []
    total_successful = []
    total_unavailable_permanent = []
    failed_packages = set()

    for fname in os.listdir(save_path):
        if not fname.startswith("meta_"):
            continue

        meta_path = os.path.join(save_path, fname)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            identifier = meta.get("identifier")
            failed = []
            success = []
            unavailable_permanent = []

            considered_resources = []
            for file_name, resource in meta.get("resources", {}).items():
                ext = file_name.split(".")[-1].lower() if "." in file_name else None
                inferred_ext = ext
                if not inferred_ext:
                    _, inferred_ext = get_mime_and_ext(resource.get("mediaType"))

                if accepted_types and (not inferred_ext or inferred_ext not in accepted_types):
                    continue

                considered_resources.append(file_name)

            if considered_resources and num_resources:
                considered_resources = considered_resources[:num_resources]

            for file_name in considered_resources:
                
                if is_completed(meta, file_name):
                    if is_completed(meta, file_name, complete=False, unavailable_permanent=True):
                        unavailable_permanent.append(file_name)
                    else:
                        success.append(file_name)
                else:
                    failed.append(file_name)

            if failed:
                failed_packages.add(identifier)

            total_failed.extend(failed)
            total_successful.extend(success)
            total_unavailable_permanent.extend(unavailable_permanent)

            packages_status[identifier] = {
                "failed_resources": failed,
                "successful_resources": success,
                "unavailable_permanent": unavailable_permanent,
            }
        except Exception as e:
            logger("ERROR", f"Could not read {meta_path}", e)

            identifier = fname.replace("meta_", "").replace(".json", "")
            failed_packages.add(identifier)

            packages_status[identifier] = {
                "failed_resources": [],
                "successful_resources": [],
                "unavailable_permanent": [],
            }

    return packages_status, total_successful, total_failed, total_unavailable_permanent, failed_packages


def is_completed(package, file_name, complete=True, unavailable=False, unavailable_permanent=False):
    if file_name:
        try:
            info = package["crawlerInfo"]["resourcesInfo"].get(file_name, {})
            file_status = info.get("fileStatus", {})
            file_info = info.get("fileInfo", {})

            if complete and file_status.get("fileCompleted"):
                return True

            if unavailable and "resource_temporarily_unavailable" in file_info:
                return True

            if unavailable_permanent and any(tag in file_info for tag in PERMANENT_UNAVAILABLE_TAGS):
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
