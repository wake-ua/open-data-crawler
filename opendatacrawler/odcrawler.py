import json
import os
import shutil
import errno
import requests
import humanize
import hashlib
import gc
import random
import threading
from datetime import datetime, timedelta
import time
import traceback
from frictionless import describe, Dialect
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from opendatacrawler import utils
from opendatacrawler.portals import CkanCrawler, DataEuropaEuCrawler, DatosGobEsCrawler, DatosMadridEsCrawler, GbifCrawler, ZenodoCrawler
from urllib.parse import urlparse

from opendatacrawler.setup_logger import log_manager
logger = log_manager.log


class LowDiskSpaceError(RuntimeError):
    pass


class OpenDataCrawler():
    def __init__(self, domain, path=None, data_types=None, categories=None, partial=False, avoid_data=None, max_sec=None, reqs_per_sec=None, max_threads=None, max_resource_threads=None, num_resources=None, countries=None, ignore_hosts=None, save_raw_data=False, extract_schema=True):
        self.domain = utils.normalize_domain(domain).rstrip("/")
        self.dms = None
        self.dms_instance = None
        self.max_sec = max_sec
        self.config_reqs_per_sec = reqs_per_sec
        self.max_threads = max_threads
        self.max_resource_threads = max_resource_threads
        self.serial_metadata_phase = True
        self.save_raw_data = save_raw_data
        self.extract_schema = extract_schema

        base_path = path or os.path.join(os.getcwd(), "data")
        utils.create_folder(base_path)

        self.clean_domain = utils.clean_url(self.domain)
        self.base_domain_path = os.path.join(base_path, self.clean_domain)
        self.save_path = self.base_domain_path

        self.data_types = data_types
        self.categories = categories
        self.partial = partial
        self.avoid_data = avoid_data
        self.num_resources = num_resources

        self.countries = countries
        self.current_country = None
        self.ignore_hosts = {self.normalize_host(value) for value in (ignore_hosts or []) if self.normalize_host(value)}

        self.user_agent = None

        self.domain_netloc = urlparse(self.domain).netloc
        self.ckan_action_requires_post = False
        self.host_timeout_threshold = utils.get_config_option("defaults", "host_timeout_threshold", cast=int, fallback=3)
        self.host_cooldown_seconds = utils.get_config_option("defaults", "host_cooldown_seconds", cast=int, fallback=60)
        self.max_file_size_mb = utils.get_config_option("defaults", "max_file_size_mb", cast=int, fallback=0)
        self.min_free_disk_mb = utils.get_config_option("defaults", "min_free_disk_mb", cast=int, fallback=512)
        self.min_free_disk_percent = utils.get_config_option("defaults", "min_free_disk_percent", cast=float, fallback=2.0)
        self.host_cooldowns = {}
        self.host_cooldown_lock = threading.Lock()
        logger("...", f"Detecting DMS for domain '{self.get_print_domain()}'...", level="print")
        self.init_rate_limit(reqs_per_sec=self.config_reqs_per_sec)
        self.detect_dms()
        if self.ignore_hosts:
            logger("INFO", f"Ignoring hosts for this run: {', '.join(sorted(self.ignore_hosts))}", level="print")

    # ==============================
    
    def detect_dms(self):
        dms_endpoints = {
            "dataEuropaEu": "/api/hub/repo/catalogues",
            #"Socrata": "/api/catalog/v1",
            "datosGobEs": "/apidata/catalog/dataset?_sort=title&_pageSize=1",
            "CKAN": "/api/3/action/package_list",
            #"WorldBank": "/ddhxext/DatasetList",
            #"EuroStat": "/estat-navtree-portlet-prod/BulkDownloadListing?sort=1&dir=metadata",
            "Zenodo": "/oai2d?verb=Identify",
            "GBIF" : "/v1/dataset/search?limit=1&offset=0",
            "OpenDataSoft": "/api/v2/catalog",
            "INE": "/wstempus/js/ES/OPERACIONES_DISPONIBLES",
            "datosMadridEs": "/portal/site/egob"
        }

        base_url = self.domain.rstrip("/")
        headers = {
            "Accept": "application/json",
        }

        for dms_name, endpoint in dms_endpoints.items():
            full_url = utils.fix_url(base_url + endpoint)
            logger("...", f"Checking DMS '{dms_name}' at '{full_url}'...", level="print")

            try:
                response, self.user_agent = self.make_request(full_url, self.user_agent, headers=headers, max_sec=120)
                if not response:
                    logger("NET", f"No response from endpoint '{full_url}' while checking DMS '{dms_name}'")
                    continue

                if response.status_code != 200:
                    logger("NET", f"Non-successful response (HTTP {response.status_code}) from '{full_url}' while checking DMS '{dms_name}'")
                    continue
                
                response.raise_for_status()
                if dms_name == "datosMadridEs" or "text/html" not in response.headers.get("Content-Type", "").lower():
                    self.dms = dms_name
                    logger("OK", f"DMS detected: '{dms_name}'", level="print")

                    if not utils.create_folder(self.save_path):
                        logger("ERROR", f"Can't create folder '{self.save_path}'")
                    break
                
            except requests.RequestException as e:
                logger("NET", f"Failed to reach '{full_url}'", [e, traceback.format_exc()])
                continue

        if self.dms == "CKAN":
            self.ckan_action_requires_post = self.detect_ckan_requires_post()

        dms_classes = {
            "CKAN": CkanCrawler,
            #"Socrata": SocrataCrawler,
            #"WorldBank": WorldBankCrawler,
            #"EuroStat": EurostatCrawler,
            "datosGobEs": DatosGobEsCrawler,
            "Zenodo": ZenodoCrawler,
            "GBIF" : GbifCrawler,
            #"OpenDataSoft": OpenDataSoftCrawler,
            #"INE": INECrawler,
            "dataEuropaEu": DataEuropaEuCrawler,
            "datosMadridEs": DatosMadridEsCrawler
        }

        if self.dms:
            cls = dms_classes.get(self.dms)
            if cls:
                try:
                    self.dms_instance = cls(self)
                except Exception:
                    logger("ERROR", f"Error instantiating DMS class for '{self.dms}'", f"\n{traceback.format_exc()}")
        else:
            logger("ERROR", f"No accessible or supported DMS detected at '{self.get_print_domain()}'", level="print")

    # ==============================
    def detect_ckan_requires_post(self) -> bool:
        probe_url = utils.fix_url(f"{self.domain}/api/3/action/package_show")
        try:
            r, self.user_agent = self.make_request(probe_url, self.user_agent, headers={"Accept": "application/json"}, params={"id": "non-existent"}, max_sec=30)
            if r is None:
                return False
            txt = (r.text or "")
            return ("Please use POST method" in txt) or ("Invalid request" in txt)
        except Exception:
            return False

    def get_ckan_api_key(self):
        if self.dms != "CKAN":
            return None

        if self.dms_instance and hasattr(self.dms_instance, "token"):
            return self.dms_instance.token

        return None
        
    def set_country_context(self, country):
        previous_save_path = self.save_path
        self.current_country = country

        if country:
            self.save_path = os.path.join(self.base_domain_path, country)
        else:
            self.save_path = self.base_domain_path

        if self.save_path != previous_save_path or not os.path.exists(self.save_path):
            utils.create_folder(self.save_path)

        removed_parts = utils.cleanup_path_tempfiles(self.save_path)
        removed_tempfiles = utils.cleanup_system_tempfiles()
        if removed_parts or removed_tempfiles:
            logger("DEL", f"Cleaned {removed_parts} orphan partial files and {removed_tempfiles} orphan temporary files before continuing", level="print")

    def get_print_domain(self):
        if self.current_country:
            return f"{self.domain} | {utils.get_country_label(self.current_country)} [{self.current_country}]"
        return self.domain

    # ==============================

    def reset_domain(self, reset_domain, has_data, has_logs):
        if reset_domain and (has_data or has_logs):
            try:
                log_manager.move_to_domain(self.clean_domain, move_file=False)

                if has_data:
                    if os.path.exists(self.save_path):
                        shutil.rmtree(self.save_path)
                        logger("DEL", f"Deleted data folder: {self.save_path}", level="print")

                if has_logs:
                    domain_logs_path = os.path.join(os.getcwd(), "logs", self.clean_domain)
                    if os.path.exists(domain_logs_path):
                        shutil.rmtree(domain_logs_path)
                        logger("DEL", f"Deleted log folder: {domain_logs_path}", level="print")

                logger("OK", f"All data and logs for '{self.domain}' have been removed", level="print")
                utils.create_folder(self.save_path)
            except Exception:
                logger("ERROR", f"Failed to delete data/logs for '{self.domain}'", f"\n{traceback.format_exc()}", level="print")

        log_manager.move_to_domain(self.clean_domain, move_file=True)
        log_manager.clean_unused_logs()

    def force_replace_package(self, dataset_ids):
        if not dataset_ids:
            return

        for pkg_id in dataset_ids:
            meta_name = f"meta_{utils.generate_short_filename(f'{self.domain}_{pkg_id}')}.json"
            meta_path = os.path.join(self.save_path, meta_name)

            if not os.path.exists(meta_path):
                logger("SKIP", f"No metadata found for package with ID '{pkg_id}', skipping replacement", level="print")
                continue

            logger("...", f"Forcing replacement of package with ID '{pkg_id}' (removing related files and metadata)...", level="print")

            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                logger("ERROR", f"Failed to read metadata for package with ID '{pkg_id}'", e)
                continue

            removed_resources = 0
            for file_name, resource in meta.get("resources", {}).items():
                if not resource.get("path"):
                    continue

                res_path = os.path.join(self.save_path, file_name)
                if os.path.exists(res_path):
                    try:
                        os.remove(res_path)
                        removed_resources += 1
                        logger("DEL", f"Deleted resource '{res_path}' from package '{pkg_id}'", level="print")
                    except Exception as e:
                        logger("ERROR", f"Failed to delete resource '{res_path}' from package '{pkg_id}'", e)

            try:
                os.remove(meta_path)
                logger("DEL", f"Deleted metadata file '{meta_path}' for package '{pkg_id}'", level="print")
            except Exception as e:
                logger("ERROR", f"Failed to delete metadata for package '{pkg_id}' ('{meta_path}')", e, level="print")

            logger("OK", f"Package '{pkg_id}' fully cleared ({removed_resources} resources removed)", level="print")

    def log_run_summary(self, downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_permanent_before, unavailable_permanent_after, resume_data):
        new_downloads = len(downloaded_after_res) - len(downloaded_before_res)
        new_failures = max(0, len(failed_after_res) - len(failed_before_res))
        recovered = len(set(failed_before_res) - set(failed_after_res))
        new_unavailable = max(0, len(unavailable_permanent_after) - len(unavailable_permanent_before))
        total_success = len(downloaded_after_res)
        total_failed = len(failed_after_res)
        total_unavailable = len(unavailable_permanent_after)
        logger("OK", f"{new_downloads} new resources downloaded in this run ({new_failures} new failures, {recovered} recovered, {new_unavailable} marked as permanently unavailable): {total_success} successful, {total_failed} failed, {total_unavailable} permanently unavailable in total across {len(resume_data)} packages", level="print")
        for host_info in self.get_problematic_hosts():
            logger("WARNING", f"Host '{host_info['host']}' appears to be down or unstable in this run ({host_info['temporary_failures']} temporary failures, {host_info['cooldown_hits']} cooldown skips, last reason: {host_info['last_reason']}). Try again later or use '--ignore-hosts {host_info['host']}' to skip it.", level="print")

    def run_threaded_function(self, items, func, max_workers=1, thread_name_prefix=None, use_tqdm=False, tqdm_initial=0, tqdm_desc="", tqdm_colour=None, store_results=True):
        results = [] if store_results else None
        if max_workers == 1:
            iterable = items
            if use_tqdm:
                iterable = tqdm(items, total=len(items) + tqdm_initial, initial=tqdm_initial, desc=tqdm_desc, colour=tqdm_colour)
            for item in iterable:
                result = func(item)
                if store_results:
                    results.append(result)
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"{thread_name_prefix}") as executor:
                futures = [executor.submit(func, item) for item in items]
                try:
                    if use_tqdm:
                        futures_iter = tqdm(as_completed(futures), total=len(futures) + tqdm_initial, initial=tqdm_initial, desc=tqdm_desc, colour=tqdm_colour)
                    else:
                        futures_iter = as_completed(futures)
                    for future in futures_iter:
                        result = future.result()
                        if store_results:
                            results.append(result)
                except KeyboardInterrupt:
                    for f in futures:
                        f.cancel()
                    executor.shutdown(wait=False)
                    raise
        return results if store_results else None

    def process_packages_batch(self, packages, phase, tqdm_initial, tqdm_desc, tqdm_colour, downloaded_before_res, failed_before_res, unavailable_permanent_before_res, max_workers=None):
        try:
            if phase == "metadata":
                func = lambda pkg_id: self.process_package_metadata(pkg_id)
            else:
                func = lambda pkg_id: self.process_package(pkg_id)

            workers = max_workers if max_workers is not None else self.max_threads

            self.run_threaded_function(items=packages, func=func, max_workers=workers, thread_name_prefix="t", use_tqdm=True, tqdm_desc=tqdm_desc, tqdm_colour=tqdm_colour, tqdm_initial=tqdm_initial, store_results=False)

        except KeyboardInterrupt:
            logger(None, "=" * 80, level="print")

            if self.max_threads == 1:
                logger("WARNING", "Interrupt received, terminating execution", level="print")
            else:
                logger("WARNING", "Interrupt received, terminating all threads immediately", level="print")

            logger(None, "=" * 80, level="print")

            resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(save_path=self.save_path, accepted_types=self.data_types, num_resources=self.num_resources)

            self.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_permanent_before_res, unavailable_permanent_after_res, resume_data)
            os._exit(1)
        except LowDiskSpaceError as e:
            logger(None, "=" * 80, level="print")
            logger("ERROR", "Stopping crawler due to low disk space", e, level="print")
            logger(None, "=" * 80, level="print")

            resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(save_path=self.save_path, accepted_types=self.data_types, num_resources=self.num_resources)

            self.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_permanent_before_res, unavailable_permanent_after_res, resume_data)
            os._exit(1)

    # ==============================

    def init_rate_limit(self, reqs_per_sec=None, safety_factor=0.85):
        if not reqs_per_sec:
            self.rate_limit = None
            return

        self.rate_limit = reqs_per_sec * safety_factor
        self.max_pending_reqs = 1
        self.pending_reqs = self.max_pending_reqs

        now = time.time()
        self.last_update = now

        self.req_total = 0
        self.lock = threading.Lock()

        logger("INFO", f"Rate limiting active for domain '{self.get_print_domain()}' ({reqs_per_sec:.3f} req/s ~ {(reqs_per_sec * 3600):.0f} req/h, of which {self.rate_limit:.3f} req/s ~ {(self.rate_limit * 3600):.0f} req/h effective)", level="print")

    def check_rate_limit(self):
        if not self.rate_limit:
            return

        with self.lock:
            now = time.time()
            elapsed = now - self.last_update

            self.pending_reqs = min(self.max_pending_reqs, self.pending_reqs + (elapsed * self.rate_limit))
            self.last_update = now

            if self.pending_reqs < 1:
                wait_time = (1 - self.pending_reqs) / self.rate_limit
                wait_time += random.uniform(0, 0.2 / self.rate_limit)
                time.sleep(wait_time)
                now = time.time()
                self.pending_reqs = 1
                self.last_update = now

            self.pending_reqs -= 1
            self.req_total += 1

    def should_rate_limit_url(self, url):
        if not self.rate_limit or not url:
            return False

        try:
            return urlparse(utils.fix_url(url)).netloc == self.domain_netloc
        except Exception:
            return False

    def normalize_host(self, value):
        if not value:
            return None
        try:
            fixed = utils.fix_url(str(value).strip())
            host = urlparse(fixed).netloc.lower()
            return host or None
        except Exception:
            return None

    def is_host_ignored(self, url):
        if not url or not self.ignore_hosts:
            return None
        host = self.normalize_host(url)
        return host if host in self.ignore_hosts else None

    def check_host_cooldown(self, url):
        if not url or self.host_timeout_threshold <= 0 or self.host_cooldown_seconds <= 0:
            return None

        host = urlparse(utils.fix_url(url)).netloc
        if not host:
            return None

        with self.host_cooldown_lock:
            state = self.host_cooldowns.get(host)
            if not state:
                return None

            cooldown_until = state.get("cooldown_until", 0)
            if cooldown_until and cooldown_until > time.time():
                state["cooldown_hits"] = state.get("cooldown_hits", 0) + 1
                wait_seconds = max(1, int(cooldown_until - time.time()))
                return host, wait_seconds

            if cooldown_until:
                state["cooldown_until"] = 0

        return None

    def register_host_result(self, url, error_tag=None, error=None):
        if not url or self.host_timeout_threshold <= 0 or self.host_cooldown_seconds <= 0:
            return

        host = urlparse(utils.fix_url(url)).netloc
        if not host:
            return

        with self.host_cooldown_lock:
            state = self.host_cooldowns.setdefault(host, {"timeouts": 0, "cooldown_until": 0, "temporary_failures": 0, "cooldown_hits": 0, "last_reason": None, "problem_reported": False})

            if error_tag == "resource_temporarily_unavailable":
                backoff_hint = utils.get_host_backoff_hint(error) if error is not None else {"penalty": 1, "cooldown_scale": 1, "reason": "generic"}
                state["timeouts"] += backoff_hint.get("penalty", 1)
                state["temporary_failures"] = state.get("temporary_failures", 0) + 1
                state["last_reason"] = backoff_hint.get("reason", "generic")
                if not state.get("problem_reported") and state["temporary_failures"] >= self.host_timeout_threshold:
                    state["problem_reported"] = True
                    logger("WARNING", f"Host '{host}' is showing repeated temporary failures in this run ({state['temporary_failures']} so far, last reason: {state['last_reason']}). It may be down or unstable. Try again later or use '--ignore-hosts {host}' to skip it.", level="print")
                if state["timeouts"] >= self.host_timeout_threshold:
                    cooldown_seconds = max(1, int(self.host_cooldown_seconds * backoff_hint.get("cooldown_scale", 1)))
                    state["cooldown_until"] = time.time() + cooldown_seconds
                    state["timeouts"] = 0
                    logger("WARNING", f"Host '{host}' entered cooldown for {cooldown_seconds}s after repeated temporary access failures ({backoff_hint.get('reason', 'generic')})")
            else:
                state["timeouts"] = 0
                state["cooldown_until"] = 0

    def get_problematic_hosts(self):
        problematic = []
        with self.host_cooldown_lock:
            for host, state in self.host_cooldowns.items():
                temporary_failures = int(state.get("temporary_failures", 0) or 0)
                cooldown_hits = int(state.get("cooldown_hits", 0) or 0)
                if temporary_failures < self.host_timeout_threshold and cooldown_hits < 3:
                    continue
                problematic.append({
                    "host": host,
                    "temporary_failures": temporary_failures,
                    "cooldown_hits": cooldown_hits,
                    "last_reason": state.get("last_reason", "generic"),
                })
        return sorted(problematic, key=lambda item: (item["temporary_failures"], item["cooldown_hits"]), reverse=True)

    def ensure_disk_headroom(self, path, required_bytes=0, context=None, log_indent=4):
        min_free_bytes = max(int(self.min_free_disk_mb or 0), 0) * 1024 * 1024
        ok, usage = utils.has_enough_disk_space(path, required_bytes=required_bytes, min_free_bytes=min_free_bytes, min_free_percent=self.min_free_disk_percent)
        if ok:
            return True

        free_percent = (usage.free / usage.total * 100) if usage.total else 0
        required_headroom = max(int(required_bytes or 0) + min_free_bytes, 0)
        message = "Insufficient free disk space"
        if context:
            message += f" while {context}"
        message += f" (free={humanize.naturalsize(usage.free, binary=True)}, required_headroom>={humanize.naturalsize(required_headroom, binary=True)}, free_percent={free_percent:.2f}%)"
        logger("WARNING", message, indent=log_indent)
        raise LowDiskSpaceError(message)

    def make_action_request(self, action: str, *, params: dict | None = None, headers: dict | None = None, return_tag: bool = False, current_agent: str | None = None, max_sec: int | None = None):
        url = utils.fix_url(f"{self.domain}/api/3/action/{action}")

        hdrs = dict(headers) if headers else {}
        hdrs.setdefault("Accept", "application/json")
        hdrs.setdefault("Connection", "keep-alive")

        ckan_api_key = self.get_ckan_api_key()
        if ckan_api_key and urlparse(url).netloc == self.domain_netloc:
            hdrs.setdefault("X-CKAN-API-Key", ckan_api_key)
            hdrs.setdefault("Authorization", ckan_api_key)

        agent = current_agent if current_agent is not None else self.user_agent
        timeout = max_sec if max_sec is not None else self.max_sec
        payload = params or {}

        if self.ckan_action_requires_post:
            if self.rate_limit:
                return utils.make_request_post(url, agent, headers=hdrs, json_body=payload, max_sec=timeout, return_tag=return_tag, rate_controller=self)
            return utils.make_request_post(url, agent, headers=hdrs, json_body=payload, max_sec=timeout, return_tag=return_tag)

        if self.rate_limit:
            return utils.make_request(url, agent, headers=hdrs, params=payload, max_sec=timeout, return_tag=return_tag, rate_controller=self)
        return utils.make_request(url, agent, headers=hdrs, params=payload, max_sec=timeout, return_tag=return_tag)

    def make_request(self, url, current_agent, headers=None, **kwargs):
        headers = dict(headers) if headers else {}

        ckan_api_key = self.get_ckan_api_key()
        if ckan_api_key and urlparse(url).netloc == self.domain_netloc:
            headers.setdefault("Authorization", ckan_api_key)
            headers.setdefault("X-CKAN-API-Key", ckan_api_key)

        if self.should_rate_limit_url(url):
            return utils.make_request(url, current_agent, headers=headers, **kwargs, rate_controller=self)

        return utils.make_request(url, current_agent, headers=headers, **kwargs)
        
    # ==============================

    def save_dataset(self, url, file_name, chunk_size=64*1024, log_indent=4):
        logger("...", f"Attempting to download resource '{file_name}' from '{url}'...", indent=log_indent)

        headers = {
            "Accept": "*/*", 
            "Connection": "keep-alive"
        }

        response, self.user_agent, error_tag, e = self.make_request(url, self.user_agent, headers=headers, max_sec=self.max_sec, stream=True, return_tag=True)
        if error_tag:
            return None, error_tag, e

        path = os.path.join(self.save_path, file_name)
        temp_path = f"{path}.part"
        try:
            content_length = response.headers.get("Content-Length")
            required_bytes = int(content_length) if content_length and str(content_length).isdigit() else 0

            if self.max_file_size_mb and required_bytes:
                max_allowed_bytes = int(self.max_file_size_mb) * 1024 * 1024
                if required_bytes > max_allowed_bytes:
                    logger(
                        "WARNING",
                        f"Skipping resource '{file_name}' because server-declared size {humanize.naturalsize(required_bytes, binary=True)} exceeds configured limit of {humanize.naturalsize(max_allowed_bytes, binary=True)}",
                        indent=log_indent,
                    )
                    return None, "resource_too_large", None

            self.ensure_disk_headroom(temp_path, required_bytes=required_bytes, context=f"downloading resource '{file_name}'", log_indent=log_indent)

            total_bytes = 0
            line_limit = 50
            lines_downloaded = 0
            next_space_check = 64 * 1024 * 1024

            response.raw.decode_content = True
            try:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as cleanup_error:
                        logger("ERROR", f"Failed to delete stale partial file '{temp_path}' before download", cleanup_error, indent=log_indent)

                with open(temp_path, "wb") as outfile:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue

                        outfile.write(chunk)
                        total_bytes += len(chunk)

                        if total_bytes >= next_space_check:
                            self.ensure_disk_headroom(temp_path, context=f"continuing download of resource '{file_name}'", log_indent=log_indent)
                            next_space_check += 64 * 1024 * 1024

                        if self.partial:
                            lines_downloaded += chunk.count(b"\n")
                            if lines_downloaded >= line_limit:
                                logger("WARNING", f"Partial content downloaded (~{line_limit} rows) for '{file_name}'", indent=log_indent)
                                break
            except LowDiskSpaceError:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger("DEL", f"Deleted partial file '{temp_path}' after disk-space failure", indent=log_indent)
                    except Exception as cleanup_error:
                        logger("ERROR", f"Failed to delete partial file '{temp_path}'", cleanup_error, indent=log_indent)
                raise
            except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logger("DEL", f"Deleted partial file '{temp_path}' after interrupted download", indent=log_indent)
                    except Exception as cleanup_error:
                        logger("ERROR", f"Failed to delete partial file '{temp_path}'", cleanup_error, indent=log_indent)
                logger("WARNING", f"Chunked connection error while saving '{file_name}'", e, indent=log_indent)
                return None, "resource_temporarily_unavailable", e
            except OSError as e:
                if getattr(e, "errno", None) == errno.ENOSPC:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                            logger("DEL", f"Deleted partial file '{temp_path}' after disk-space failure", indent=log_indent)
                        except Exception as cleanup_error:
                            logger("ERROR", f"Failed to delete partial file '{temp_path}'", cleanup_error, indent=log_indent)
                    logger("WARNING", f"Insufficient disk space while saving '{file_name}'", e, indent=log_indent)
                    raise LowDiskSpaceError(f"Insufficient disk space while saving '{file_name}'") from e
                raise

            if not self.partial and total_bytes == 0:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as cleanup_error:
                        logger("ERROR", f"Failed to delete empty file '{temp_path}'", cleanup_error, indent=log_indent)
                logger("WARNING", f"No data downloaded for resource '{file_name}'", indent=log_indent)
                return None, "no_data", None

            if not os.path.exists(temp_path):
                logger("WARNING", f"Partial file '{temp_path}' disappeared before finalizing download of '{file_name}'", indent=log_indent)
                return None, "resource_temporarily_unavailable", FileNotFoundError(temp_path)

            os.replace(temp_path, path)

            return path, None, None

        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception as cleanup_error:
                    logger("ERROR", f"Failed to delete incomplete file '{temp_path}' after unexpected error", cleanup_error, indent=log_indent)
            logger("ERROR", f"Unexpected error saving dataset '{file_name}'", [e, traceback.format_exc()], indent=log_indent)
            return None, None, None
        finally:
            if response:
                response.close()

    def save_metadata(self, package, log_indent=2):
        try:
            file_name = package.get("fileName")

            meta_path = os.path.join(self.save_path, file_name)
            logger("SAVE", f"Saving metadata to '{meta_path}'...", indent=log_indent)

            if "crawlerInfo" in package:
                package["crawlerInfo"] = package.pop("crawlerInfo")

            all_resources_complete = True
            for _, res_info in package["crawlerInfo"]["resourcesInfo"].items():
                if not res_info.get("fileStatus", {}).get("fileCompleted"):
                    all_resources_complete = False
                    break

            if all_resources_complete and not package["crawlerInfo"]["packageStatus"].get("packageCompleted"):
                package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

            sanitized_package = utils.sanitize_json_keys(package)
            serialized_package = json.dumps(sanitized_package, ensure_ascii=False, indent=log_indent, sort_keys=True)
            new_hash = hashlib.sha1(serialized_package.encode("utf-8")).hexdigest()
            required_bytes = len(serialized_package.encode("utf-8"))

            existing_hash = None
            if os.path.exists(meta_path):
                sha1 = hashlib.sha1()
                with open(meta_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        sha1.update(chunk)
                existing_hash = sha1.hexdigest()

            if existing_hash == new_hash:
                logger("SKIP", f"Metadata file '{meta_path}' already exists and is up-to-date", indent=log_indent)
                return

            self.ensure_disk_headroom(meta_path, required_bytes=required_bytes, context=f"saving metadata '{file_name}'", log_indent=log_indent)

            if utils.atomic_dump_json(meta_path, sanitized_package, ensure_ascii=False, indent=log_indent, sort_keys=True):
                logger("OK", f"Metadata saved successfully to '{meta_path}'", indent=log_indent)
        
        except LowDiskSpaceError:
            raise
        except Exception as e:
            logger("ERROR", f"Failed to save metadata file '{meta_path}'", [e, traceback.format_exc()], indent=log_indent)

    def handle_parse_resource(self, resource_meta, base_name, metadata_file_name, reparse_data=None, log_indent=4):
        resource_crawler_info = utils.init_metadata(package=False)

        if reparse_data:
            logger("...", f"Re-parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=log_indent)
            resource = resource_meta
            meta_media_type = reparse_data.get("metaMediaType")
        else:
            logger("...", f"Parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=log_indent)
            resource, meta_media_type = self.dms_instance.parse_resource(resource_meta, base_name)

        if reparse_data:
            meta_media_type = reparse_data.get("metaMediaType") or meta_media_type

        if not resource or not resource.get("downloadURL"):
            logger("ERROR", f"Missing or invalid download URL for resource '{base_name}'", indent=log_indent)
            return resource, resource_crawler_info

        response, self.user_agent, error_tag, e = self.make_request(resource["downloadURL"], self.user_agent, return_tag=True, max_sec=self.max_sec, stream=True)
        if not response:
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    resource_crawler_info["fileInfo"].update(utils.add_tag_explanations(error_tag))
                    logger("WARNING", f"Non-retryable error accessing resource '{base_name}' for parsing", e, indent=log_indent)
                    resource_crawler_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                else:
                    resource_crawler_info["fileInfo"].update(utils.add_tag_explanations(error_tag, {"<metaMediaType>": meta_media_type}))
                    logger("WARNING", f"Retryable error accessing resource '{base_name}' for parsing", e, indent=log_indent)
            else:
                logger("ERROR", f"Error accessing resource '{base_name}'", e, indent=log_indent)

            return resource, resource_crawler_info

        try:
            media_type, file_name, tag_val = utils.resolve_mediatype_conflict(meta_media_type, response, resource["downloadURL"], base_name)

            resource["mediaType"] = media_type
            resource["fileName"] = file_name
            if tag_val:
                logger("WARNING", f"Detected a media type mismatch for '{resource['downloadURL']}'", indent=log_indent)
                resource_crawler_info["fileMetadataChanges"].update(utils.add_tag_explanations("mimetype_mismatch", tag_val))

            if reparse_data:
                logger("OK", f"Successfully re-parsed resource '{resource['fileName']}' from package '{metadata_file_name}'", indent=log_indent)
            else:
                logger("OK", f"Successfully parsed resource '{resource['fileName']}' from package '{metadata_file_name}'", indent=log_indent)

            return resource, resource_crawler_info
        finally:
            response.close()
        
    def retry_temporarily_unavailable_resources(self, package, log_indent=2):
        missing_resources = [
            file_name for file_name, _ in package["crawlerInfo"]["resourcesInfo"].items()
            if utils.is_completed(package, file_name, complete=False, unavailable=True)
        ]

        def reparse_resource(old_file_name):
            resource_meta = package["resources"].get(old_file_name)
            info = package["crawlerInfo"]["resourcesInfo"].get(old_file_name, {})

            tag_info = info.get("fileInfo", {}).get("resource_temporarily_unavailable", {})
            tag_values = tag_info.get("values", {})

            if resource_meta and tag_values:
                new_resource, new_info = self.handle_parse_resource(resource_meta, old_file_name, package["fileName"], reparse_data=tag_values)
                if new_resource:
                    return old_file_name, new_resource, new_info
            return old_file_name, None, None

        if missing_resources:
            logger("...", f"Re-parsing {len(missing_resources)} temporarily unavailable resources...", indent=log_indent)
            reparsed_resources = self.run_threaded_function(
                items=missing_resources,
                func=reparse_resource,
                max_workers=self.max_resource_threads,
                thread_name_prefix="r",
            )

            for old_file_name, new_resource, new_info in reparsed_resources:
                if not new_resource:
                    continue

                new_file_name = new_resource["fileName"]
                if new_file_name != old_file_name:
                    package["resources"].pop(old_file_name, None)
                    package["crawlerInfo"]["resourcesInfo"].pop(old_file_name, None)

                package["resources"][new_file_name] = new_resource
                package["crawlerInfo"]["resourcesInfo"][new_file_name] = new_info

        return package

    def init_and_parse_resources(self, metadata, distributions, log_indent=2):
        logger("WORK", f"Processing {len(distributions)} resources from package '{metadata['identifier']}' ('{metadata['fileName']}')...", indent=log_indent)

        metadata["resources"] = {}
        metadata["crawlerInfo"]["resourcesInfo"] = {}

        for idx, resource in enumerate(distributions):
            base_name = utils.generate_short_filename(f"{metadata['fileName']}_{idx}")
            metadata["resources"][base_name] = resource
            metadata["crawlerInfo"]["resourcesInfo"][base_name] = utils.init_metadata(package=False, crawled=False)

        if self.num_resources:
            logger("INFO", f"Delaying full resource parsing for package '{metadata['identifier']}' because num_resources={self.num_resources}", indent=log_indent)
            return

        def parse_resource_func(item):
            idx, resource = item
            base_name = utils.generate_short_filename(f"{metadata['fileName']}_{idx}")
            parsed_resource, parsed_info = self.handle_parse_resource(resource, base_name, metadata["fileName"])
            return base_name, resource, parsed_resource, parsed_info

        logger("...", f"Parsing {len(distributions)} resources from package '{metadata['fileName']}'...", indent=log_indent)
        parsed_resources = self.run_threaded_function(
            items=list(enumerate(distributions)), func=parse_resource_func,
            max_workers=self.max_resource_threads, thread_name_prefix="r"
        )

        for base_name, resource, parsed_resource, parsed_info in parsed_resources:
            if not parsed_resource:
                continue

            new_file_name = parsed_resource["fileName"]
            if new_file_name != base_name:
                metadata["resources"].pop(base_name, None)
                metadata["crawlerInfo"]["resourcesInfo"].pop(base_name, None)

            metadata["resources"][new_file_name] = parsed_resource
            metadata["crawlerInfo"]["resourcesInfo"][new_file_name] = parsed_info

    def upsert_package_resource(self, package, old_file_name, resource, resource_info):
        new_file_name = resource.get("fileName") or old_file_name

        if new_file_name != old_file_name:
            package["resources"].pop(old_file_name, None)
            package["crawlerInfo"]["resourcesInfo"].pop(old_file_name, None)

        package["resources"][new_file_name] = resource
        package["crawlerInfo"]["resourcesInfo"][new_file_name] = resource_info
        return new_file_name

    def infer_resource_extension(self, file_name, media_type):
        ext = file_name.split(".")[-1].lower() if file_name and "." in file_name else None
        if ext:
            return ext

        _, inferred_ext = utils.get_mime_and_ext(media_type)
        return inferred_ext

    def resource_matches_filters(self, file_name, resource, log_indent=1):
        if self.avoid_data:
            return False

        media_type = resource.get("mediaType")
        if not media_type:
            logger("INFO", f"Resource '{file_name}' has no validated media type yet, skipping for now...", indent=log_indent)
            return False

        inferred_ext = self.infer_resource_extension(file_name, media_type)
        if self.data_types and (not inferred_ext or inferred_ext not in self.data_types):
            logger(
                "SKIP",
                f"Skipping resource '{file_name}' (media type '{media_type}' with inferred extension '{inferred_ext or 'unknown'}' not in accepted types)",
                indent=log_indent
            )
            return False

        return True

    def resource_has_downloaded_file(self, resource):
        path = resource.get("path")
        if not path:
            return False

        abs_path = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
        return os.path.exists(abs_path)

    def prepare_resource_for_processing(self, package, resource_file_name, metadata_file_name):
        resource = package["resources"].get(resource_file_name)
        resource_info = package["crawlerInfo"]["resourcesInfo"].get(resource_file_name, utils.init_metadata(package=False, crawled=False))

        if not resource:
            return resource_file_name, None, resource_info

        tag_info = resource_info.get("fileInfo", {}).get("resource_temporarily_unavailable", {})
        tag_values = tag_info.get("values", {})

        if tag_values:
            parsed_resource, parsed_info = self.handle_parse_resource(resource, resource_file_name, metadata_file_name, reparse_data=tag_values)
            if parsed_resource:
                resource = parsed_resource
                resource_info = parsed_info

            resource_file_name = self.upsert_package_resource(package, resource_file_name, resource, resource_info)
            return resource_file_name, resource, resource_info

        if not resource_info.get("fileStatus", {}).get("fileCrawled"):
            parsed_resource, parsed_info = self.handle_parse_resource(resource, resource_file_name, metadata_file_name)
            if parsed_resource:
                resource = parsed_resource
                resource_info = parsed_info

            resource_file_name = self.upsert_package_resource(package, resource_file_name, resource, resource_info)

        return resource_file_name, resource, resource_info


    def process_package_metadata(self, pkg_id, log_indent=1):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.domain}_{pkg_id}')}.json"
        metadata_path = os.path.join(self.save_path, metadata_file_name)

        try:
            logger("WORK", f"Processing metadata for package '{pkg_id}' ('{metadata_file_name}')...", indent=log_indent-1)

            if os.path.exists(metadata_path):
                logger("SKIP", f"Metadata already exists for '{metadata_file_name}', skipping metadata phase", indent=log_indent)
                return

            package = self.get_package(pkg_id, metadata_file_name)
            if not package:
                return

            if not package.get("resources"):
                logger("WARNING", f"No distributions found in package '{pkg_id}'", indent=log_indent)
                package["crawlerInfo"]["packageMetadataChanges"].update(utils.add_tag_explanations("missing_distributions"))
                package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                self.save_metadata(package)
                return

            if self.categories:
                mapped_theme = utils.extract_mapped_field(
                    package.get("theme"),
                    utils.DATOSGOBESCRAWLER_THEME_MAP
                )
                if not (mapped_theme and any(cat in mapped_theme for cat in self.categories)):
                    logger(
                        "SKIP",
                        f"Package '{pkg_id}' does not match categories, skipping",
                        indent=log_indent
                    )
                    self.save_metadata(package)
                    return

            self.save_metadata(package)

        except LowDiskSpaceError:
            raise
        except Exception as e:
            logger(
                "ERROR",
                f"Error processing metadata for package '{pkg_id}' ('{metadata_file_name}')",
                [e, traceback.format_exc()],
                indent=log_indent
            )

    def process_package(self, pkg_id, log_indent=1):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.domain}_{pkg_id}')}.json"
        try:
            metadata_path = os.path.join(self.save_path, metadata_file_name)
            logger("WORK", f"Processing package '{pkg_id}' ('{metadata_file_name}')...", indent=log_indent-1)

            if not os.path.exists(metadata_path):
                logger("SKIP", f"Metadata file '{metadata_file_name}' not found, skipping resource phase", indent=log_indent)
                return

            with open(metadata_path, "r", encoding="utf-8") as f:
                package = json.load(f)

            if not self.num_resources:
                package = self.retry_temporarily_unavailable_resources(package)

            if not package:
                return

            if not package.get("resources"):
                logger("WARNING", f"No distributions found in package metadata '{pkg_id}' ('{metadata_path}')", indent=log_indent)
                package["crawlerInfo"]["packageMetadataChanges"].update(utils.add_tag_explanations("missing_distributions"))
                package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                self.save_metadata(package)
                return

            if self.categories:
                mapped_theme = utils.extract_mapped_field(package.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)
                if not (mapped_theme and any(cat in mapped_theme for cat in self.categories)):
                    logger("SKIP", f"Package '{pkg_id}' ('{metadata_path}') does not match specified categories ({', '.join(self.categories)}), skipping all resources", indent=log_indent)
                    self.save_metadata(package)
                    return

            if self.num_resources:
                completed_valid_resources = 0
                total_resources = len(package.get("resources", {}))

                if self.num_resources == 1:
                    logger("INFO", f"Processing resources until finding the first valid resource from package '{pkg_id}' (out of {total_resources} total available)", indent=log_indent)
                else:
                    logger("INFO", f"Processing resources until finding {self.num_resources} valid resources from package '{pkg_id}' (out of {total_resources} total available)", indent=log_indent)

                ordered_resource_names = list(package["resources"].keys())
                for resource_file_name in ordered_resource_names:
                    resource = package["resources"].get(resource_file_name)

                    if not resource:
                        continue

                    if self.resource_has_downloaded_file(resource) and self.resource_matches_filters(resource_file_name, resource, log_indent):
                        completed_valid_resources += 1
                        if completed_valid_resources >= self.num_resources:
                            break
                        continue

                    resource_file_name, resource, resource_info = self.prepare_resource_for_processing(package, resource_file_name, metadata_file_name)
                    if not resource:
                        continue

                    if not self.resource_matches_filters(resource_file_name, resource, log_indent):
                        continue

                    _, updated_resource, updated_info = self.process_resource(resource, package, metadata_path)
                    resource_file_name = self.upsert_package_resource(package, resource_file_name, updated_resource, updated_info)

                    if self.resource_has_downloaded_file(updated_resource):
                        completed_valid_resources += 1
                        if completed_valid_resources >= self.num_resources:
                            break

                self.save_metadata(package)
                return

            resources_to_process = []
            for file_name in list(package["resources"].keys()):
                file_name, resource, resource_info = self.prepare_resource_for_processing(package, file_name, metadata_file_name)
                if not resource:
                    continue

                if utils.is_completed(package, file_name, unavailable=True):
                    continue

                if not self.resource_matches_filters(file_name, resource, log_indent):
                    continue

                resources_to_process.append((file_name, resource))

            resource_results = self.run_threaded_function(
                items=resources_to_process, func=lambda item: self.process_resource(item[1], package, metadata_path),
                max_workers=self.max_resource_threads, thread_name_prefix="r"
            )

            for resource_file_name, updated_resource, resource_info in resource_results:
                if resource_file_name not in package["resources"]:
                    continue

                self.upsert_package_resource(package, resource_file_name, updated_resource, resource_info)

            self.save_metadata(package)

        except LowDiskSpaceError:
            raise
        except Exception as e:
            logger("ERROR", f"Error processing package '{pkg_id}' ('{metadata_file_name}')", [e, traceback.format_exc()], indent=log_indent)

    def process_resource(self, resource, package, metadata_path, log_indent=3):
        resource = dict(resource)
        resource_file_name = resource.get("fileName")
        download_url = resource.get("downloadURL")
        media_type = resource.get("mediaType")
        resource_info = dict(package["crawlerInfo"]["resourcesInfo"].get(resource_file_name, {}))
        resource_info["fileMetadataChanges"] = dict(resource_info.get("fileMetadataChanges", {}))
        resource_info["binaryFileChanges"] = dict(resource_info.get("binaryFileChanges", {}))
        resource_info["fileInfo"] = dict(resource_info.get("fileInfo", {}))
        resource_info["fileStatus"] = dict(resource_info.get("fileStatus", {}))

        logger("...", f"Saving and processing resource '{resource_file_name}' from package '{metadata_path}'...", indent=log_indent)
        path, tag, e = self.save_dataset(download_url, resource_file_name)
        if path or tag:
            if path:
                resource["path"] = os.path.relpath(path, start=os.getcwd())
                resource_info["fileStatus"]["fileDownloaded"] = datetime.now().isoformat()
                logger("SAVE", f"Resource '{resource_file_name}' from package '{metadata_path}' downloaded successfully", indent=log_indent)

                process_result = self.process_dataset_file(resource_info, resource_file_name, path, resource)
                if process_result is True:
                    logger("OK", f"Resource '{path}' successfully processed from package '{metadata_path}'", indent=log_indent)
                elif process_result == "completed_without_file":
                    logger("WARNING", f"Downloaded content for resource '{resource_file_name}' from package '{metadata_path}' was not a valid dataset file and was discarded", indent=log_indent)

            if tag:
                if tag == "resource_temporarily_unavailable":
                    logger("WARNING", f"Retryable error downloading resource '{resource_file_name}'", e, indent=log_indent)
                    resource_info["fileInfo"].update(utils.add_tag_explanations(tag, {"<metaMediaType>": media_type}))
                else:
                    resource_info["fileInfo"].update(utils.add_tag_explanations(tag))
                    if tag != "no_data":
                        logger("WARNING", f"Non-retryable error downloading resource '{resource_file_name}'", e, indent=log_indent)

                resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

        return resource_file_name, resource, resource_info

    def process_dataset_file(self, resource_info, dataset_file_name, dataset_path, resource, log_indent=4):
        if not os.path.exists(dataset_path):
            logger("ERROR", f"Dataset file '{dataset_path}' does not exist", indent=log_indent)
            return False

        dataset_size = os.path.getsize(dataset_path)
        processing_headroom = max(dataset_size * 2, 64 * 1024 * 1024)
        self.ensure_disk_headroom(dataset_path, required_bytes=processing_headroom, context=f"processing dataset '{dataset_file_name}'", log_indent=log_indent)

        raw_signature = utils.detect_raw_signature(dataset_path)
        if raw_signature:
            logger("INFO", f"Detected raw signature '{raw_signature}' for file '{dataset_path}'", indent=log_indent)
        if raw_signature == "text/html":
            old_file_name = resource.get("fileName") or dataset_file_name
            old_media_type = resource.get("mediaType")
            base_name = old_file_name.rsplit(".", 1)[0] if "." in old_file_name else old_file_name
            new_file_name = f"{base_name}.html" if base_name else old_file_name

            logger("WARNING", f"File '{dataset_path}' is actually an HTML page, not the expected downloadable dataset", indent=log_indent)
            resource["mediaType"] = raw_signature
            resource["fileName"] = new_file_name
            resource.pop("path", None)
            resource.pop("encoding", None)
            resource.pop("delimiter", None)
            resource.pop("schema", None)
            resource.pop("size", None)
            resource_info["fileInfo"].update(utils.add_tag_explanations("html_page_downloaded"))
            resource_info["fileMetadataChanges"].update(utils.add_tag_explanations("downloaded_html_page", {
                "<mediaType_old>": old_media_type,
                "<fileName_old>": old_file_name,
                "<mediaType_new>": raw_signature,
                "<fileName_new>": new_file_name,
            }))
            resource_info["fileStatus"].pop("fileDownloaded", None)
            if os.path.exists(dataset_path):
                try:
                    os.remove(dataset_path)
                    logger("DEL", f"Deleted discarded HTML content '{dataset_path}'", indent=log_indent)
                except Exception as e:
                    logger("ERROR", f"Failed to delete discarded HTML content '{dataset_path}'", e, indent=log_indent)
            resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
            return "completed_without_file"

        if utils.MIME_TYPE_MAP.get(resource.get("mediaType"), {}).get("compressible", False):
            temp_path, tag = utils.check_file_empty_or_strip(dataset_path)

            if temp_path:
                shutil.move(temp_path, dataset_path)
                logger("FIX", f"File '{dataset_path}' content stripped and overwritten", indent=log_indent)
                resource_info["binaryFileChanges"].update(utils.add_tag_explanations("stripped_data"))

            if tag:
                logger("WARNING", f"File '{dataset_path}' has no data or no valid content", indent=log_indent)
                resource_info["fileInfo"].update(utils.add_tag_explanations(tag))
                return False

            try:
                encoding, temp_path, raw_flags = utils.detect_best_encoding(dataset_path)
                if encoding:
                    resource["encoding"] = encoding
                else:
                    logger("ERROR", f"No matching encoding found for: {dataset_path}", indent=log_indent)
                    return False

                if temp_path:
                    shutil.move(temp_path, dataset_path)
                    logger("FIX", f"Overwrote cleaned content into '{dataset_path}'", indent=log_indent)
                if raw_flags:
                    resource_info["binaryFileChanges"].update(utils.add_tag_explanations(raw_flags, raw_flags))
            except Exception as e:
                logger("ERROR", f"Error while detecting encoding for: {dataset_path}", [e, traceback.format_exc()], indent=log_indent)
                return False

            raw_signature = utils.detect_raw_signature(dataset_path)
            if raw_signature:
                logger("INFO", f"Detected raw signature '{raw_signature}' for normalized file '{dataset_path}'", indent=log_indent)
            if raw_signature == "text/html":
                old_file_name = resource.get("fileName") or dataset_file_name
                old_media_type = resource.get("mediaType")
                base_name = old_file_name.rsplit(".", 1)[0] if "." in old_file_name else old_file_name
                new_file_name = f"{base_name}.html" if base_name else old_file_name

                logger("WARNING", f"Normalized file '{dataset_path}' is actually an HTML page, not the expected downloadable dataset", indent=log_indent)
                resource["mediaType"] = raw_signature
                resource["fileName"] = new_file_name
                resource.pop("path", None)
                resource.pop("encoding", None)
                resource.pop("delimiter", None)
                resource.pop("schema", None)
                resource.pop("size", None)
                resource_info["fileInfo"].update(utils.add_tag_explanations("html_page_downloaded"))
                resource_info["fileMetadataChanges"].update(utils.add_tag_explanations("downloaded_html_page", {
                    "<mediaType_old>": old_media_type,
                    "<fileName_old>": old_file_name,
                    "<mediaType_new>": raw_signature,
                    "<fileName_new>": new_file_name,
                }))
                resource_info["fileStatus"].pop("fileDownloaded", None)
                if os.path.exists(dataset_path):
                    try:
                        os.remove(dataset_path)
                        logger("DEL", f"Deleted discarded HTML content '{dataset_path}'", indent=log_indent)
                    except Exception as e:
                        logger("ERROR", f"Failed to delete discarded HTML content '{dataset_path}'", e, indent=log_indent)
                resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                return "completed_without_file"

        if os.path.getsize(dataset_path) > 0 and dataset_path.endswith((".csv", ".tsv")):
            try:
                max_fix_attempts = 2
                delimiter_fix = None
                temp_path = None
                reconstructed_rows = 0
                outer_quotes_removed = False
                inner_quotes_fixed = 0
                for attempt in range(max_fix_attempts):
                    temp_path, reconstructed_rows, outer_quotes_removed, inner_quotes_fixed, delimiter_fix = utils.fix_tabular_data(dataset_path, resource["encoding"])

                    if not temp_path:
                        break

                    shutil.move(temp_path, dataset_path)
                    if attempt == 0:
                        logger("FIX", f"Cleaned and standardized the tabular file '{dataset_path}'", indent=log_indent)
                        resource_info["binaryFileChanges"].update(utils.add_tag_explanations("standardized_field_quotes"))
                    else:
                        logger("FIX", f"Reprocessing tabular file '{dataset_path}' after previous structural fixes", indent=log_indent)

                    if reconstructed_rows:
                        logger("FIX", f"Reconstructed {reconstructed_rows} multiline rows in tabular file '{dataset_path}' by merging quoted fields split across rows", indent=log_indent)
                        resource_info["binaryFileChanges"].update(utils.add_tag_explanations("reconstructed_rows", {"<reconstructed_rows>": reconstructed_rows}))
                    if outer_quotes_removed:
                        logger("FIX", f"Removed unnecessary outer quotes wrapping entire rows in tabular file '{dataset_path}'", indent=log_indent)
                        resource_info["binaryFileChanges"].update(utils.add_tag_explanations("stripped_outer_quotes"))
                    if inner_quotes_fixed:
                        logger("FIX", f"Fixed {inner_quotes_fixed} rows with malformed inner quotes in tabular file '{dataset_path}'", indent=log_indent + 2)
                        resource_info["binaryFileChanges"].update(utils.add_tag_explanations("fixed_inner_quotes", {"<fixed_inner_quotes>": inner_quotes_fixed}))
                    
                    if delimiter_fix is None and (reconstructed_rows or outer_quotes_removed or inner_quotes_fixed):
                        logger("FIX", f"Retrying delimiter detection on normalized file '{dataset_path}'", indent=log_indent + 2)
                        continue
                    else:
                        break

            except Exception as e:
                logger("ERROR", f"Failed to clean tabular file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                return False

            try:
                delimiter, start_row, delimiter_stats = utils.detect_delimiter_consistent(
                    dataset_path,
                    resource["encoding"],
                    return_stats=True,
                )
                if delimiter_stats.get("nonempty_lines", 0) <= 1:
                    logger("WARNING", f"Tabular file '{dataset_path}' appears to contain only one line", indent=4)
                    resource_info["binaryFileChanges"].update(utils.add_tag_explanations("one_line"))

                if delimiter:
                    if delimiter_fix != delimiter:
                        logger("ERROR", f"Delimiter mismatch in '{dataset_path}', expected '{delimiter_fix}', detected '{delimiter} in tabular file '{dataset_path}'", indent=log_indent)

                    resource["delimiter"] = delimiter

                    if start_row:
                        resource_info["fileInfo"].update(utils.add_tag_explanations("skip_rows", {"<skipped_rows>": start_row}))
                        dialect = Dialect.from_descriptor({"delimiter": delimiter, "comment_rows": [start_row]})
                    else:
                        dialect = Dialect.from_descriptor({"delimiter": delimiter})

                    if self.extract_schema:
                        try:
                            resource_metadata = describe(dataset_path, encoding=resource["encoding"], dialect=dialect).to_dict()
                            resource["schema"] = resource_metadata.get("schema")
                            logger("OK", f"Schema extracted from '{dataset_path}' (encoding: '{resource['encoding']}', delimiter: '{delimiter}', start_row: {start_row})", indent=4)
                        except Exception as e:
                            logger("ERROR", f"Failed to extract schema from file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                            return False
                else:
                    resource_info["fileInfo"].update(utils.add_tag_explanations("no_delimiter_detected"))
                    logger("WARNING", f"File '{dataset_path}' appears to not contain a delimiter, likely not a structured/tabular file", indent=log_indent)
                    resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))
                    resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                    return True
            except Exception as e:
                logger("ERROR", f"Failed to detect delimiter for file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                return False

        resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))
        resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
        return True

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages

    def get_package(self, pkg_id, metadata_file_name):
        package = self.dms_instance.get_package(pkg_id, metadata_file_name)
        return package
