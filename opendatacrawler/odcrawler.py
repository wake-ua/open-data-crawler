import json
import os
import shutil
import errno
import hashlib
import requests
import humanize
import gc
import copy
import csv
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from types import SimpleNamespace
import random
import threading
from datetime import datetime, timedelta
import time
import traceback
from frictionless import describe, Dialect
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from tqdm import tqdm
from opendatacrawler import utils
from opendatacrawler.portals import CkanCrawler, DataEuropaEuCrawler, DatosGobEsCrawler, DatosMadridEsCrawler, GbifCrawler, INECrawler, ZenodoCrawler
from urllib.parse import urlparse

from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class LowDiskSpaceError(RuntimeError):
    pass

class OpenDataCrawler():
    LOCAL_RESOURCE_FIELDS = frozenset({"fileName", "path", "encoding", "delimiter", "schema", "size", "sizeBytes", "downloadHeaders", "processingFingerprint"})
    TEMP_UNAVAILABLE_INFO_KEY = utils.metadata_tag_key("resource_temporarily_unavailable")

    def __init__(self, domain, path=None, data_types=None, categories=None, partial=False, partial_dataset_rows=None, avoid_data=None, max_sec=None, reqs_per_sec=None, max_threads=None, max_resource_threads=None, max_packages=None, num_resources=None, countries=None, ignore_hosts=None, save_raw_data=False, extract_schema=True, check_remote_updates=True):
        self.domain = utils.normalize_domain(domain).rstrip("/")
        self.dms = None
        self.dms_instance = None
        self.max_sec = max_sec or 30
        self.config_reqs_per_sec = reqs_per_sec
        self.max_threads = max_threads or 4
        self.max_resource_threads = max_resource_threads or 2
        self.serial_metadata_phase = True
        self.save_raw_data = save_raw_data
        self.extract_schema = extract_schema
        self.check_remote_updates = check_remote_updates
        self.packages_requiring_refresh = set()
        self.failed_packages = set()
        self.download_results = {}
        self.request_deadline_seconds = utils.get_config_option("defaults", "request_deadline_seconds", float, 120)
        self.download_deadline_seconds = utils.get_config_option("defaults", "download_deadline_seconds", float, 600)
        self.http_max_attempts = utils.get_config_option("defaults", "http_max_attempts", int, 3)
        self.max_metadata_bytes = utils.get_config_option("defaults", "max_metadata_mb", int, 64) * 1024 * 1024

        base_path = path or os.path.join(os.getcwd(), "data")
        utils.create_folder(base_path)

        self.clean_domain = utils.clean_url(self.domain)
        self.base_domain_path = os.path.join(base_path, self.clean_domain)
        self.save_path = self.base_domain_path

        self.data_types = data_types
        self.categories = categories
        if partial and partial_dataset_rows is None:
            partial_dataset_rows = 100
        if partial_dataset_rows is not None and partial_dataset_rows <= 0:
            partial_dataset_rows = 100
        self.partial_dataset_rows = partial_dataset_rows
        self.partial_dataset_sample_mode = "first"
        self.partial_dataset_random_seed = 1
        self.avoid_data = avoid_data
        self.num_resources = num_resources
        self.max_packages = max_packages

        self.countries = countries or []
        self.current_country = None
        self.ignore_hosts = {self.normalize_host(value) for value in (ignore_hosts or []) if self.normalize_host(value)}

        self.user_agent = None

        self.domain_netloc = urlparse(self.domain).netloc
        self.ckan_action_requires_post = False
        self.host_timeout_threshold = utils.get_config_option("defaults", "host_timeout_threshold", cast=int, fallback=3)
        self.host_cooldown_seconds = utils.get_config_option("defaults", "host_cooldown_seconds", cast=int, fallback=60)
        self.host_max_concurrent_requests = max(1, utils.get_config_option("defaults", "host_max_concurrent_requests", cast=int, fallback=1))
        self.max_file_size_mb = utils.get_config_option("defaults", "max_file_size_mb", cast=int, fallback=0)
        self.min_free_disk_mb = utils.get_config_option("defaults", "min_free_disk_mb", cast=int, fallback=512)
        self.min_free_disk_percent = utils.get_config_option("defaults", "min_free_disk_percent", cast=float, fallback=2.0)
        self.host_cooldowns = {}
        self.host_cooldown_lock = threading.Lock()
        self.host_request_semaphores = {}
        self.host_request_lock = threading.Lock()
        logger("...", f"Detecting DMS for domain '{self.get_print_domain()}'...", level="print")
        self.init_rate_limit(reqs_per_sec=self.config_reqs_per_sec)
        self.detect_dms()
        if self.ignore_hosts:
            logger("INFO", f"Ignoring hosts for this run: {', '.join(sorted(self.ignore_hosts))}", level="print")

    def detect_dms(self):
        endpoints = {
            "CKAN": "/api/3/action/package_list",
            "dataEuropaEu": "/api/hub/repo/catalogues",
            "datosGobEs": "/apidata/catalog/dataset?_sort=title&_pageSize=1",
            "Zenodo": "/oai2d?verb=Identify",
            "GBIF": "/v1/dataset/search?limit=1&offset=0",
            "INE": "/wstempus/js/ES/OPERACIONES_DISPONIBLES",
        }
        host = urlparse(self.domain).hostname
        hints = {"data.europa.eu": "dataEuropaEu", "datos.gob.es": "datosGobEs",
                 "api.gbif.org": "GBIF", "zenodo.org": "Zenodo", "sandbox.zenodo.org": "Zenodo",
                 "www.ine.es": "INE", "servicios.ine.es": "INE", "ine.es": "INE"}
        order = list(endpoints)
        if host in hints:
            order.remove(hints[host]); order.insert(0, hints[host])
        for name in order:
            url = self.domain + endpoints[name]
            if name == "INE" and host in {"www.ine.es", "servicios.ine.es", "ine.es"}:
                url = "https://servicios.ine.es" + endpoints[name]
            headers = {"Accept": "application/json"}
            if name == "CKAN" and utils.AUTH_TOKENS.get("ckan"):
                headers["Authorization"] = utils.AUTH_TOKENS["ckan"]
            response, self.user_agent = self.make_request(url, self.user_agent, headers=headers)
            if response is None:
                continue
            if name == "CKAN" and (response.status_code == 405 or "Please use POST method" in response.text):
                response.close()
                response, self.user_agent = utils.make_request_post(url, self.user_agent, headers=headers, json_body={}, rate_controller=self)
                self.ckan_action_requires_post = True
            if response is None:
                continue
            try:
                response.raise_for_status()
                if name == "Zenodo":
                    tree = ET.fromstring(response.content)
                    valid = tree.tag == "{http://www.openarchives.org/OAI/2.0/}OAI-PMH" and tree.find("{http://www.openarchives.org/OAI/2.0/}Identify") is not None
                else:
                    data = response.json()
                    valid = self.valid_detection_payload(name, data)
                if valid:
                    self.dms = name
                    break
            except (requests.RequestException, ValueError, ET.ParseError):
                continue
            finally:
                response.close()
        classes = {"CKAN": CkanCrawler, "dataEuropaEu": DataEuropaEuCrawler,
                   "datosGobEs": DatosGobEsCrawler, "Zenodo": ZenodoCrawler,
                   "GBIF": GbifCrawler, "INE": INECrawler, "datosMadridEs": DatosMadridEsCrawler}
        if self.dms:
            utils.create_folder(self.save_path)
            self.dms_instance = classes[self.dms](self)
            if self.dms == "CKAN" and not self.ckan_action_requires_post:
                self.ckan_action_requires_post = self.detect_ckan_requires_post()
            logger("OK", f"DMS detected: '{self.dms}'", level="print")
        else:
            raise utils.CatalogError(f"No supported portal contract found at {self.domain}")

    @staticmethod
    def valid_detection_payload(name, data):
        if name == "INE":
            return isinstance(data, list) and bool(data) and all(isinstance(x, dict) and "Codigo" in x and "Nombre" in x for x in data)
        if name == "dataEuropaEu" and isinstance(data, list):
            return bool(data) and all(isinstance(x, str) and "/catalogue/" in x for x in data)
        if not isinstance(data, dict):
            return False
        if name == "CKAN":
            return data.get("success") is True and isinstance(data.get("result"), list)
        if name == "GBIF":
            return isinstance(data.get("count"), int) and isinstance(data.get("results"), list)
        if name == "datosGobEs":
            return isinstance(data.get("result"), dict) and isinstance(data["result"].get("items"), list)
        if name == "dataEuropaEu":
            return (data.get("success") is True or data.get("status") == "success") and isinstance(data.get("result"), (dict, list))
        return False

    def detect_ckan_requires_post(self):
        response, self.user_agent = self.make_action_request("package_show", params={"id": "non-existent"})
        if response is None:
            return False
        try:
            return response.status_code == 405 or "Please use POST method" in response.text
        finally:
            response.close()

    def get_ckan_api_key(self):
        if self.dms != "CKAN":
            return None

        if self.dms_instance and hasattr(self.dms_instance, "token"):
            return self.dms_instance.token

        return utils.AUTH_TOKENS.get("ckan")
        
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

    def set_packages_requiring_refresh(self, package_ids):
        self.packages_requiring_refresh = {pkg_id for pkg_id in (package_ids or []) if pkg_id}

    def get_packages_requiring_refresh(self):
        return set(self.packages_requiring_refresh)

    def needs_package_refresh(self, pkg_id):
        return pkg_id in self.packages_requiring_refresh

    def clear_package_refresh_requirement(self, pkg_id):
        self.packages_requiring_refresh.discard(pkg_id)

    def preserve_local_resource_fields(self, old_resource, new_resource):
        merged_resource = dict(new_resource or {})
        if not isinstance(old_resource, dict):
            return merged_resource

        for field in self.LOCAL_RESOURCE_FIELDS:
            if field in old_resource:
                merged_resource[field] = old_resource[field]

        return merged_resource

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
            meta_name = self.metadata_file_name(pkg_id)
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
            logger("WARNING", f"Host '{host_info['host']}' is showing repeated access problems in this run ({host_info['temporary_failures']} temporary failures, {host_info['permanent_failures']} permanent failures, {host_info['cooldown_hits']} cooldown skips, last reason: {host_info['last_reason']}). Try again later or use '--ignore-hosts {host_info['host']}' to skip it.")

    def run_threaded_function(self, items, func, max_workers=1, thread_name_prefix=None, use_tqdm=False, tqdm_initial=0, tqdm_desc="", tqdm_colour=None, store_results=True):
        workers = max_workers or self.max_threads
        results = []
        iterator = iter(items)
        progress = tqdm(total=len(items) + tqdm_initial, initial=tqdm_initial, desc=tqdm_desc,
                        colour=tqdm_colour, disable=not use_tqdm)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix or "odc") as executor:
                pending = set()
                def fill():
                    while len(pending) < workers * 2:
                        try:
                            item = next(iterator)
                        except StopIteration:
                            break
                        pending.add(executor.submit(func, item))
                fill()
                try:
                    while pending:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            value = future.result()
                            if store_results:
                                results.append(value)
                            progress.update(1)
                        fill()
                except BaseException:
                    for future in pending:
                        future.cancel()
                    raise
        finally:
            progress.close()
        return results if store_results else None

    def process_packages_batch(self, packages, phase, tqdm_initial, tqdm_desc, tqdm_colour, downloaded_before_res, failed_before_res, unavailable_permanent_before_res, max_workers=None):
        try:
            if phase == "metadata":
                func = lambda pkg_id: self.process_package_metadata(pkg_id)
            else:
                func = lambda pkg_id: self.process_package(pkg_id)

            workers = max_workers if max_workers is not None else self.max_threads
            if phase != "resources":
                self.run_threaded_function(items=packages, func=func, max_workers=workers, thread_name_prefix="t", use_tqdm=True, tqdm_desc=tqdm_desc, tqdm_colour=tqdm_colour, tqdm_initial=tqdm_initial, store_results=False)
                return

            pending_packages = list(packages)
            current_initial = tqdm_initial
            defer_round = 0
            while pending_packages:
                results = self.run_threaded_function(items=pending_packages, func=func, max_workers=workers, thread_name_prefix="t", use_tqdm=True, tqdm_desc=tqdm_desc, tqdm_colour=tqdm_colour, tqdm_initial=current_initial, store_results=True)
                deferred_packages = []
                deferred_hosts = []
                for result in results:
                    if not isinstance(result, dict) or result.get("status") != "deferred":
                        continue
                    pkg_id = result.get("pkg_id")
                    if pkg_id and pkg_id not in deferred_packages:
                        deferred_packages.append(pkg_id)
                    host = result.get("host")
                    if host and host not in deferred_hosts:
                        deferred_hosts.append(host)

                if not deferred_packages:
                    break

                defer_round += 1
                if defer_round >= 2:
                    host_text = f" Hosts still affected: {', '.join(sorted(deferred_hosts))}." if deferred_hosts else ""
                    logger("WARNING", f"Leaving {len(deferred_packages)} package(s) pending for a later run because their hosts are still unstable or in cooldown.{host_text} Try again later or use '--ignore-hosts' for those hosts.", level="print")
                    break

                host_text = f" Affected hosts: {', '.join(sorted(deferred_hosts))}." if deferred_hosts else ""
                logger("INFO", f"Deferring {len(deferred_packages)} package(s) to the end of the current resources phase because their hosts are unstable or in cooldown.{host_text}", level="print")
                pending_packages = deferred_packages
                current_initial = 0

        except KeyboardInterrupt:
            logger(None, "=" * 80, level="print")

            if self.max_threads == 1:
                logger("WARNING", "Interrupt received, terminating execution", level="print")
            else:
                logger("WARNING", "Interrupt received, terminating all threads immediately", level="print")

            logger(None, "=" * 80, level="print")

            resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(
                save_path=self.save_path,
                accepted_types=self.data_types,
                num_resources=self.num_resources,
                avoid_data=self.avoid_data,
            )

            self.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_permanent_before_res, unavailable_permanent_after_res, resume_data)
            raise
        except LowDiskSpaceError as e:
            logger(None, "=" * 80, level="print")
            logger("ERROR", "Stopping crawler due to low disk space", e, level="print")
            logger(None, "=" * 80, level="print")

            resume_data, downloaded_after_res, failed_after_res, unavailable_permanent_after_res, _ = utils.recover_resume(
                save_path=self.save_path,
                accepted_types=self.data_types,
                num_resources=self.num_resources,
                avoid_data=self.avoid_data,
            )

            self.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, unavailable_permanent_before_res, unavailable_permanent_after_res, resume_data)
            raise

    def init_rate_limit(self, reqs_per_sec=None, safety_factor=0.85):
        if reqs_per_sec is not None and reqs_per_sec <= 0:
            raise ValueError("reqs_per_sec must be positive")
        if reqs_per_sec is None:
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

    def acquire_host_request_slot(self, url):
        host = self.normalize_host(url)
        if not host:
            return None, None
        with self.host_request_lock:
            semaphore = self.host_request_semaphores.get(host)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self.host_max_concurrent_requests)
                self.host_request_semaphores[host] = semaphore
        if not semaphore.acquire(timeout=self.request_deadline_seconds):
            raise requests.Timeout(f"Timed out waiting for a connection slot for {host}")
        return host, semaphore

    def release_host_request_slot(self, semaphore):
        if semaphore is not None:
            semaphore.release()

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
            state = self.host_cooldowns.setdefault(host, {"timeouts": 0, "cooldown_until": 0, "temporary_failures": 0, "permanent_failures": 0, "cooldown_hits": 0, "last_reason": None, "problem_reported": False})

            if error_tag == "resource_temporarily_unavailable":
                backoff_hint = utils.get_host_backoff_hint(error) if error is not None else {"penalty": 1, "cooldown_scale": 1, "reason": "generic"}
                state["timeouts"] += 1
                state["temporary_failures"] = state.get("temporary_failures", 0) + 1
                state["last_reason"] = backoff_hint.get("reason", "generic")
                if state["timeouts"] >= self.host_timeout_threshold:
                    cooldown_seconds = max(1, int(self.host_cooldown_seconds))
                    state["cooldown_until"] = time.time() + cooldown_seconds
                    state["timeouts"] = 0
                    if not state.get("problem_reported"):
                        state["problem_reported"] = True
                    logger("WARNING", f"Host '{host}' is showing repeated temporary failures in this run ({state['temporary_failures']} so far, last reason: {state['last_reason']}) and has entered cooldown for {cooldown_seconds}s. It may be down or unstable. Try again later or use '--ignore-hosts {host}' to skip it.")
                elif not state.get("problem_reported") and state["temporary_failures"] >= self.host_timeout_threshold:
                    state["problem_reported"] = True
                    logger("WARNING", f"Host '{host}' is showing repeated temporary failures in this run ({state['temporary_failures']} so far, last reason: {state['last_reason']}). It may be down or unstable. Try again later or use '--ignore-hosts {host}' to skip it.")
            elif error_tag:
                state["permanent_failures"] = state.get("permanent_failures", 0) + 1
                state["last_reason"] = error_tag
                if not state.get("problem_reported") and state["permanent_failures"] >= self.host_timeout_threshold:
                    state["problem_reported"] = True
                    logger("WARNING", f"Host '{host}' is showing repeated non-retryable access failures in this run ({state['permanent_failures']} so far, last reason: {state['last_reason']}). It may require skipping for this run. Consider '--ignore-hosts {host}'.")
            else:
                state["timeouts"] = 0
                state["cooldown_until"] = 0

    def get_problematic_hosts(self):
        problematic = []
        with self.host_cooldown_lock:
            for host, state in self.host_cooldowns.items():
                temporary_failures = int(state.get("temporary_failures", 0) or 0)
                permanent_failures = int(state.get("permanent_failures", 0) or 0)
                cooldown_hits = int(state.get("cooldown_hits", 0) or 0)
                if temporary_failures < self.host_timeout_threshold and permanent_failures < self.host_timeout_threshold and cooldown_hits < 3:
                    continue
                problematic.append({
                    "host": host,
                    "temporary_failures": temporary_failures,
                    "permanent_failures": permanent_failures,
                    "cooldown_hits": cooldown_hits,
                    "last_reason": state.get("last_reason", "generic"),
                })
        return sorted(problematic, key=lambda item: (item["temporary_failures"], item["permanent_failures"], item["cooldown_hits"]), reverse=True)

    def get_host_runtime_state(self, url):
        host = self.normalize_host(url)
        if not host:
            return None
        now = time.time()
        with self.host_cooldown_lock:
            state = dict(self.host_cooldowns.get(host, {}))
        cooldown_until = state.get("cooldown_until", 0) or 0
        wait_seconds = max(0, int(cooldown_until - now)) if cooldown_until else 0
        return {"host": host, "temporary_failures": int(state.get("temporary_failures", 0) or 0), "cooldown_hits": int(state.get("cooldown_hits", 0) or 0), "last_reason": state.get("last_reason", "generic"), "wait_seconds": wait_seconds, "in_cooldown": wait_seconds > 0}

    def should_defer_package_for_url(self, url):
        state = self.get_host_runtime_state(url)
        if not state:
            return None
        if state["in_cooldown"] or state["temporary_failures"] >= self.host_timeout_threshold:
            return state
        return None

    def clear_recovered_resource_tags(self, resource_info):
        file_info = resource_info.get("fileInfo", {})
        if self.TEMP_UNAVAILABLE_INFO_KEY in file_info:
            file_info.pop(self.TEMP_UNAVAILABLE_INFO_KEY, None)

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
        if ckan_api_key and (urlparse(url).scheme, urlparse(url).netloc) == (urlparse(self.domain).scheme, self.domain_netloc):
            hdrs.setdefault("X-CKAN-API-Key", ckan_api_key)
            hdrs.setdefault("Authorization", ckan_api_key)

        agent = current_agent if current_agent is not None else self.user_agent
        timeout = max_sec if max_sec is not None else self.max_sec
        payload = params or {}

        if self.ckan_action_requires_post:
            return utils.make_request_post(url, agent, headers=hdrs, json_body=payload, max_sec=timeout, return_tag=return_tag, rate_controller=self)

        return utils.make_request(url, agent, headers=hdrs, params=payload, max_sec=timeout, return_tag=return_tag, rate_controller=self)

    def make_request(self, url, current_agent, headers=None, **kwargs):
        headers = dict(headers) if headers else {}

        ckan_api_key = self.get_ckan_api_key()
        if ckan_api_key and (urlparse(url).scheme, urlparse(url).netloc) == (urlparse(self.domain).scheme, self.domain_netloc):
            headers.setdefault("Authorization", ckan_api_key)
            headers.setdefault("X-CKAN-API-Key", ckan_api_key)

        kwargs.setdefault("max_sec", self.max_sec)
        if self.dms == "Zenodo" and (urlparse(url).scheme, urlparse(url).netloc) == (urlparse(self.domain).scheme, self.domain_netloc) and getattr(self.dms_instance, "token", None):
            headers.setdefault("Authorization", f"Bearer {self.dms_instance.token}")
        return utils.make_request(url, current_agent, headers=headers, **kwargs, rate_controller=self)
        
    def save_dataset(self, url, file_name, chunk_size=64*1024, log_indent=4):
        response, self.user_agent, tag, error = self.make_request(url, self.user_agent, stream=True, return_tag=True)
        if response is None:
            return None, tag or "resource_temporarily_unavailable", error
        path = os.path.join(self.save_path, os.path.basename(file_name))
        temporary = None
        try:
            length = response.headers.get("Content-Length", "")
            length = int(length) if str(length).isdigit() else 0
            maximum = int(self.max_file_size_mb or 0) * 1024 * 1024
            if maximum and length > maximum:
                return None, "resource_too_large", {"message": "Content-Length exceeds limit"}
            self.ensure_disk_headroom(path, required_bytes=length, context="downloading")
            digest = hashlib.sha256()
            total = 0
            deadline = time.monotonic() + self.download_deadline_seconds
            next_check = 8 * 1024 * 1024
            with tempfile.NamedTemporaryFile(dir=self.save_path, prefix=utils.ATOMIC_TEMP_PREFIX, delete=False) as output:
                temporary = output.name
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if time.monotonic() >= deadline:
                        raise requests.Timeout("Download deadline exceeded")
                    total += len(chunk)
                    if maximum and total > maximum:
                        return None, "resource_too_large", {"message": "Actual response body exceeds limit", "sizeBytes": total}
                    digest.update(chunk)
                    output.write(chunk)
                    if total >= next_check:
                        self.ensure_disk_headroom(path, context="downloading")
                        next_check = total + 8 * 1024 * 1024
                output.flush()
                os.fsync(output.fileno())
            if not total:
                return None, "no_data", None
            os.replace(temporary, path)
            self.download_results[path] = {"sha256": digest.hexdigest(), "sizeBytes": total,
                "url": response.url, "headers": {key: response.headers[key] for key in
                    ("Content-Type", "Content-Disposition", "ETag", "Last-Modified", "Content-Length") if key in response.headers}}
            return path, None, None
        except LowDiskSpaceError:
            raise
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise LowDiskSpaceError("No disk space during download") from error
            raise
        except requests.RequestException as error:
            return None, "resource_temporarily_unavailable", {"message": str(error)}
        finally:
            response.close()
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def save_metadata(self, package, log_indent=2):
        try:
            file_name = package.get("fileName")
            meta_path = os.path.join(self.save_path, file_name)
            package_status = package.setdefault("crawlerInfo", {}).setdefault("packageStatus", {})

            all_resources_complete = True
            for _, res_info in package["crawlerInfo"]["resourcesInfo"].items():
                if not res_info.get("fileStatus", {}).get("fileCompleted"):
                    all_resources_complete = False
                    break

            if not all_resources_complete:
                package_status.pop("packageCompleted", None)
            if all_resources_complete and not package_status.get("packageCompleted"):
                package_status["packageCompleted"] = datetime.now().isoformat()

            persisted_package = utils.prepare_metadata_for_save(package)
            serialized_package = json.dumps(persisted_package, ensure_ascii=False, indent=log_indent, sort_keys=True)
            required_bytes = len(serialized_package.encode("utf-8"))
            new_hash = hashlib.sha1(serialized_package.encode("utf-8")).hexdigest()

            existing_hash = None
            if os.path.exists(meta_path):
                sha1 = hashlib.sha1()
                with open(meta_path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        sha1.update(chunk)
                existing_hash = sha1.hexdigest()

            if existing_hash == new_hash:
                logger("SKIP", f"Metadata file '{meta_path}' already exists and is up-to-date", indent=log_indent)
                return True

            logger("SAVE", f"Saving metadata to '{meta_path}'...", indent=log_indent)

            self.ensure_disk_headroom(meta_path, required_bytes=required_bytes, context=f"saving metadata '{file_name}'", log_indent=log_indent)

            if utils.atomic_dump_json(meta_path, persisted_package, ensure_ascii=False, indent=log_indent, sort_keys=True):
                logger("OK", f"Metadata saved successfully to '{meta_path}'", indent=log_indent)
                return True
            return False
        
        except LowDiskSpaceError:
            raise
        except Exception as e:
            logger("ERROR", f"Failed to save metadata file '{meta_path}'", [e, traceback.format_exc()], indent=log_indent)
            return False

    def handle_parse_resource(self, resource_meta, base_name, metadata_file_name, reparse_data=None, log_indent=4):
        info = utils.init_metadata(package=False, crawled=False)
        if isinstance(resource_meta, dict) and (resource_meta.get("normalizedResource") or (resource_meta.get("fileName") and resource_meta.get("downloadURL") and resource_meta.get("mediaType"))):
            resource = dict(resource_meta)
            mime = resource.get("mediaType")
        elif reparse_data:
            resource = dict(resource_meta)
            mime = resource.get("mediaType") or reparse_data.get("metaMediaType")
        else:
            resource, mime = self.dms_instance.parse_resource(resource_meta, base_name)
        resource = resource or {"fileName": base_name}
        resource["normalizedResource"] = True
        mime, ext = utils.get_mime_and_ext(mime)
        if not mime:
            mime, ext = utils.get_resource_ext_info(SimpleNamespace(headers={}), resource.get("downloadURL"))
        resource["mediaType"] = mime or "application/octet-stream"
        clean_base = base_name.rsplit(".", 1)[0] if "." in base_name else base_name
        resource["fileName"] = f"{clean_base}.{ext}" if ext else clean_base
        if not resource.get("downloadURL"):
            info["fileInfo"].update(utils.add_tag_explanations("invalid_download_url"))
            info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
        return resource, info

    def retry_temporarily_unavailable_resources(self, package, log_indent=2):
        utils.normalize_metadata(package)
        for info in package.get("crawlerInfo", {}).get("resourcesInfo", {}).values():
            if self.TEMP_UNAVAILABLE_INFO_KEY in info.get("fileInfo", {}):
                info["fileInfo"].pop(self.TEMP_UNAVAILABLE_INFO_KEY, None)
                info["fileStatus"].pop("fileCompleted", None)
        return package

    def init_and_parse_resources(self, metadata, distributions, log_indent=2):
        metadata["resources"] = {}
        metadata["crawlerInfo"]["resourcesInfo"] = {}
        for raw in distributions:
            resource, info = self.handle_parse_resource(raw, "pending", metadata["fileName"])
            identity = resource.get("id") or resource.get("identifier") or resource.get("downloadURL")
            if identity is None:
                identity = json.dumps(resource, sort_keys=True, ensure_ascii=False)
            base = utils.generate_short_filename(f"{metadata['fileName']}:resource:{identity}")
            _, ext = utils.get_mime_and_ext(resource.get("mediaType"))
            name = f"{base}.{ext}" if ext else base
            resource["fileName"] = name
            if self.avoid_data:
                info = self.mark_resource_skipped_by_config(info)
            metadata["resources"][name] = resource
            metadata["crawlerInfo"]["resourcesInfo"][name] = info

    def mark_resource_skipped_by_config(self, resource_info):
        normalized_info = dict(resource_info or {})
        normalized_info["fileMetadataChanges"] = dict(normalized_info.get("fileMetadataChanges", {}))
        normalized_info["binaryFileChanges"] = dict(normalized_info.get("binaryFileChanges", {}))
        normalized_info["fileInfo"] = dict(normalized_info.get("fileInfo", {}))
        normalized_info["fileStatus"] = dict(normalized_info.get("fileStatus", {}))

        normalized_info["fileInfo"].update(utils.add_tag_explanations("dataset_skipped_by_config"))
        normalized_info["fileStatus"]["fileSkippedByConfig"] = datetime.now().isoformat()
        return normalized_info

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

    def add_mimetype_mismatch(self, resource_info, old_media_type, old_file_name, new_media_type, new_file_name):
        old_mime, old_ext = utils.get_mime_and_ext(old_media_type)
        new_mime, new_ext = utils.get_mime_and_ext(new_media_type)
        if not old_mime or not new_mime or old_mime == new_mime or new_mime in utils.GENERIC_MIME_TYPES:
            return
        clean_old = old_file_name.rsplit(".", 1)[0] if old_file_name and "." in old_file_name else old_file_name
        clean_new = new_file_name.rsplit(".", 1)[0] if new_file_name and "." in new_file_name else new_file_name
        values = {
            "<mediaType_old>": old_mime,
            "<fileName_old>": f"{clean_old}.{old_ext}" if old_ext and clean_old else old_file_name,
            "<mediaType_new>": new_mime,
            "<fileName_new>": f"{clean_new}.{new_ext}" if new_ext and clean_new else new_file_name,
        }
        resource_info.setdefault("fileMetadataChanges", {}).update(utils.add_tag_explanations("mimetype_mismatch", values))

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
        return os.path.isfile(abs_path) or os.path.isfile(os.path.join(self.save_path, os.path.basename(path)))

    def prepare_resource_for_processing(self, package, resource_file_name, metadata_file_name):
        resource = package["resources"].get(resource_file_name)
        resource_info = package["crawlerInfo"]["resourcesInfo"].get(resource_file_name, utils.init_metadata(package=False, crawled=False))

        if not resource:
            return resource_file_name, None, resource_info

        tag_info = resource_info.get("fileInfo", {}).get(self.TEMP_UNAVAILABLE_INFO_KEY, {})
        tag_values = tag_info.get("values", {})

        if tag_values:
            parsed_resource, parsed_info = self.handle_parse_resource(resource, resource_file_name, metadata_file_name, reparse_data=tag_values)
            if parsed_resource:
                resource = parsed_resource
                resource_info = parsed_info

            resource_file_name = self.upsert_package_resource(package, resource_file_name, resource, resource_info)
            return resource_file_name, resource, resource_info

        if not resource.get("normalizedResource"):
            parsed_resource, parsed_info = self.handle_parse_resource(resource, resource_file_name, metadata_file_name)
            if parsed_resource:
                resource = parsed_resource
                resource_info = parsed_info

            resource_file_name = self.upsert_package_resource(package, resource_file_name, resource, resource_info)

        if self.data_types and resource.get("mediaType") in {"application/octet-stream", "text/plain", "application/force-download"}:
            response, self.user_agent, tag, error = self.make_request(resource.get("downloadURL"), self.user_agent, stream=True, return_tag=True)
            if response is not None:
                try:
                    old_media_type = resource.get("mediaType")
                    old_file_name = resource.get("fileName") or resource_file_name
                    mime, name, mismatch = utils.resolve_mediatype_conflict(old_media_type, response, resource.get("downloadURL"), resource_file_name)
                    if mismatch:
                        resource_info.setdefault("fileMetadataChanges", {}).update(utils.add_tag_explanations("mimetype_mismatch", mismatch))
                    if mime and mime != resource.get("mediaType"):
                        resource["mediaType"], resource["fileName"] = mime, name
                        resource_file_name = self.upsert_package_resource(package, resource_file_name, resource, resource_info)
                    elif mime:
                        self.add_mimetype_mismatch(resource_info, old_media_type, old_file_name, mime, name or old_file_name)
                finally:
                    response.close()
            elif tag:
                resource_info.setdefault("fileInfo", {}).update(utils.add_tag_explanations(tag, utils.extract_error_tag_values(error)))
                resource_info.setdefault("fileStatus", {})["fileCrawled"] = datetime.now().isoformat()
        return resource_file_name, resource, resource_info

    def metadata_needs_rebuild(self, metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                package = utils.normalize_metadata(json.load(f))
        except Exception:
            return True

        if not isinstance(package, dict):
            return True
        if not package.get("identifier"):
            return True
        if "resources" not in package or not isinstance(package.get("resources"), dict):
            return True
        crawler_info = package.get("crawlerInfo")
        if not isinstance(crawler_info, dict):
            return True
        resources_info = crawler_info.get("resourcesInfo")
        if not isinstance(resources_info, dict):
            return True
        package_status = crawler_info.get("packageStatus")
        if not isinstance(package_status, dict):
            return True
        if set(package.get("resources", {}).keys()) != set(resources_info.keys()):
            return True
        return False

    def process_package_metadata(self, pkg_id, log_indent=1):
        metadata_file_name = self.metadata_file_name(pkg_id)
        metadata_path = os.path.join(self.save_path, metadata_file_name)

        try:
            logger("WORK", f"Processing metadata for package '{pkg_id}' ('{metadata_file_name}')...", indent=log_indent-1)
            refresh_requested = self.needs_package_refresh(pkg_id)
            existing_package = None

            if os.path.exists(metadata_path):
                if self.metadata_needs_rebuild(metadata_path):
                    logger("WARNING", f"Metadata file '{metadata_file_name}' is missing required fields or is invalid, rebuilding it", indent=log_indent)
                    try:
                        os.replace(metadata_path, f"{metadata_path}.damaged.{time.time_ns()}")
                        logger("INFO", "Preserved invalid metadata before rebuilding", indent=log_indent)
                    except Exception as e:
                        logger("ERROR", f"Failed to delete invalid metadata file '{metadata_file_name}'", e, indent=log_indent)
                        return
                elif refresh_requested:
                    logger("INFO", f"Remote metadata changed for package '{pkg_id}', refreshing local metadata", indent=log_indent)
                    try:
                        with open(metadata_path, "r", encoding="utf-8") as f:
                            existing_package = utils.normalize_metadata(json.load(f))
                    except Exception as e:
                        logger("WARNING", f"Could not read existing metadata file '{metadata_file_name}' before refresh; rebuilding from remote data only", e, indent=log_indent)
                else:
                    logger("SKIP", f"Metadata already exists for '{metadata_file_name}', skipping metadata phase", indent=log_indent)
                    return
            elif refresh_requested:
                logger("INFO", f"Refresh requested for package '{pkg_id}', but no local metadata file was found. Rebuilding it from remote data.", indent=log_indent)

            package = self.get_package(pkg_id, metadata_file_name)
            if not package:
                self.failed_packages.add(str(pkg_id))
                return

            if refresh_requested and existing_package:
                package = self.merge_existing_package_state(existing_package, package)
            package.setdefault("resources", {})
            package["crawlerInfo"]["packageStatus"]["metadataChecked"] = datetime.now().isoformat()

            if not package.get("resources"):
                logger("WARNING", f"No distributions found in package '{pkg_id}'", indent=log_indent)
                package["crawlerInfo"]["packageMetadataChanges"].update(utils.add_tag_explanations("missing_distributions"))
                package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                metadata_saved = self.save_metadata(package)
                if refresh_requested and metadata_saved:
                    self.clear_package_refresh_requirement(pkg_id)
                return

            if self.categories:
                mapped_theme = utils.extract_mapped_field(
                    package.get("theme"),
                    utils.DATOSGOBESCRAWLER_THEME_MAP
                )
                if not (mapped_theme and any(cat.casefold() in {v.casefold() for v in utils.flatten_labels(mapped_theme) + utils.flatten_labels(package.get("theme"))} for cat in self.categories)):
                    logger(
                        "SKIP",
                        f"Package '{pkg_id}' does not match categories, skipping",
                        indent=log_indent
                    )
                    metadata_saved = self.save_metadata(package)
                    if refresh_requested and metadata_saved:
                        self.clear_package_refresh_requirement(pkg_id)
                    return

            metadata_saved = self.save_metadata(package)
            if not metadata_saved:
                self.failed_packages.add(str(pkg_id))
            if refresh_requested and metadata_saved:
                self.clear_package_refresh_requirement(pkg_id)

        except LowDiskSpaceError:
            raise
        except Exception as e:
            self.failed_packages.add(str(pkg_id))
            logger(
                "ERROR",
                f"Error processing metadata for package '{pkg_id}' ('{metadata_file_name}')",
                [e, traceback.format_exc()],
                indent=log_indent
            )

    def process_package(self, pkg_id, log_indent=1):
        metadata_file_name = self.metadata_file_name(pkg_id)
        try:
            metadata_path = os.path.join(self.save_path, metadata_file_name)
            logger("WORK", f"Processing package '{pkg_id}' ('{metadata_file_name}')...", indent=log_indent-1)

            if self.needs_package_refresh(pkg_id):
                logger("SKIP", f"Metadata refresh is still pending for package '{pkg_id}', skipping resource phase for safety", indent=log_indent)
                return

            if not os.path.exists(metadata_path):
                logger("SKIP", f"Metadata file '{metadata_file_name}' not found, skipping resource phase", indent=log_indent)
                return

            with open(metadata_path, "r", encoding="utf-8") as f:
                package = utils.normalize_metadata(json.load(f))

            package = self.retry_temporarily_unavailable_resources(package)
            for name, resource in package.get("resources", {}).items():
                if resource.get("processingFingerprint") != self.processing_fingerprint() and self.resource_has_downloaded_file(resource):
                    package["crawlerInfo"]["resourcesInfo"].get(name, {}).get("fileStatus", {}).pop("fileCompleted", None)

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
                if not (mapped_theme and any(cat.casefold() in {v.casefold() for v in utils.flatten_labels(mapped_theme) + utils.flatten_labels(package.get("theme"))} for cat in self.categories)):
                    logger("SKIP", f"Package '{pkg_id}' ('{metadata_path}') does not match specified categories ({', '.join(self.categories)}), skipping all resources", indent=log_indent)
                    self.save_metadata(package)
                    return

            if self.avoid_data:
                logger("SKIP", f"Dataset downloads are disabled, skipping resource phase for package '{pkg_id}'", indent=log_indent)
                self.save_metadata(package)
                return {"status": "done", "pkg_id": pkg_id}

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

                    if utils.is_completed(package, resource_file_name, complete=False, unavailable_permanent=True):
                        continue

                    if self.resource_has_downloaded_file(resource) and utils.is_completed(package, resource_file_name) and self.resource_matches_filters(resource_file_name, resource, log_indent):
                        completed_valid_resources += 1
                        if completed_valid_resources >= self.num_resources:
                            break
                        continue

                    resource_file_name, resource, resource_info = self.prepare_resource_for_processing(package, resource_file_name, metadata_file_name)
                    if not resource:
                        continue

                    defer_info = self.should_defer_package_for_url(resource.get("downloadURL"))
                    if defer_info and self.TEMP_UNAVAILABLE_INFO_KEY in resource_info.get("fileInfo", {}):
                        wait_text = f" in ~{defer_info['wait_seconds']}s" if defer_info["wait_seconds"] else ""
                        logger("INFO", f"Host '{defer_info['host']}' is showing repeated temporary failures ({defer_info['last_reason']}); package '{pkg_id}' will be retried later in this run{wait_text}", indent=log_indent)
                        self.save_metadata(package)
                        return {"status": "deferred", "pkg_id": pkg_id, "host": defer_info["host"]}

                    if not self.resource_matches_filters(resource_file_name, resource, log_indent):
                        continue

                    _, updated_resource, updated_info = self.process_resource(resource, package, metadata_path)
                    resource_file_name = self.upsert_package_resource(package, resource_file_name, updated_resource, updated_info)

                    defer_info = self.should_defer_package_for_url(updated_resource.get("downloadURL"))
                    if defer_info and self.TEMP_UNAVAILABLE_INFO_KEY in updated_info.get("fileInfo", {}):
                        wait_text = f" in ~{defer_info['wait_seconds']}s" if defer_info["wait_seconds"] else ""
                        logger("INFO", f"Host '{defer_info['host']}' is showing repeated temporary failures ({defer_info['last_reason']}); package '{pkg_id}' will be retried later in this run{wait_text}", indent=log_indent)
                        self.save_metadata(package)
                        return {"status": "deferred", "pkg_id": pkg_id, "host": defer_info["host"]}

                    if self.resource_has_downloaded_file(updated_resource) and updated_info.get("fileStatus", {}).get("fileCompleted"):
                        completed_valid_resources += 1
                        if completed_valid_resources >= self.num_resources:
                            break

                self.save_metadata(package)
                return {"status": "done", "pkg_id": pkg_id}

            resources_to_process = []
            for file_name in list(package["resources"].keys()):
                if utils.is_completed(package, file_name, complete=False, unavailable_permanent=True):
                    continue

                file_name, resource, resource_info = self.prepare_resource_for_processing(package, file_name, metadata_file_name)
                if not resource:
                    continue

                if self.resource_has_downloaded_file(resource) and utils.is_completed(package, file_name):
                    continue

                defer_info = self.should_defer_package_for_url(resource.get("downloadURL"))
                if defer_info and self.TEMP_UNAVAILABLE_INFO_KEY in resource_info.get("fileInfo", {}):
                    wait_text = f" in ~{defer_info['wait_seconds']}s" if defer_info["wait_seconds"] else ""
                    logger("INFO", f"Host '{defer_info['host']}' is showing repeated temporary failures ({defer_info['last_reason']}); package '{pkg_id}' will be retried later in this run{wait_text}", indent=log_indent)
                    self.save_metadata(package)
                    return {"status": "deferred", "pkg_id": pkg_id, "host": defer_info["host"]}

                if utils.is_completed(package, file_name, complete=False, unavailable=True):
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
            return {"status": "done", "pkg_id": pkg_id}

        except LowDiskSpaceError:
            raise
        except Exception as e:
            self.failed_packages.add(str(pkg_id))
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

        resource_info["fileStatus"].pop("fileCompleted", None)
        resource_info["fileStatus"]["fileCrawled"] = datetime.now().isoformat()
        logger("...", f"Saving and processing resource '{resource_file_name}' from package '{metadata_path}'...", indent=log_indent)
        for field in ("originalPath", "originalSha256", "originalSizeBytes"):
            resource.pop(field, None)
        path, tag, e = self.save_dataset(download_url, resource_file_name)
        if path or tag:
            error_message = utils.extract_error_message(e)
            tag_values = utils.extract_error_tag_values(e)
            if path:
                resource["path"] = os.path.abspath(path)
                transfer = self.download_results.pop(path, {})
                resource["downloadHeaders"] = transfer.get("headers", resource.get("downloadHeaders", {}))
                self.clear_recovered_resource_tags(resource_info)
                resource_info["fileStatus"]["fileDownloaded"] = datetime.now().isoformat()
                logger("SAVE", f"Resource '{resource_file_name}' from package '{metadata_path}' downloaded successfully", indent=log_indent)

                process_result = self.process_dataset_file(resource_info, resource_file_name, path, resource)
                if process_result is True:
                    logger("OK", f"Resource '{path}' successfully processed from package '{metadata_path}'", indent=log_indent)
                elif process_result == "completed_without_file":
                    logger("WARNING", f"Downloaded content for resource '{resource_file_name}' from package '{metadata_path}' was not a valid dataset file and was discarded", indent=log_indent)

            if tag:
                if tag == "resource_temporarily_unavailable":
                    logger("WARNING", f"Retryable error downloading resource '{resource_file_name}'", error_message, indent=log_indent)
                    retry_tag_values = {"<metaMediaType>": media_type}
                    if tag_values:
                        retry_tag_values.update(tag_values)
                    resource_info["fileInfo"].update(utils.add_tag_explanations(tag, retry_tag_values))
                else:
                    resource_info["fileInfo"].update(utils.add_tag_explanations(tag, tag_values))
                    if tag != "no_data":
                        logger("WARNING", f"Non-retryable error downloading resource '{resource_file_name}'", error_message, indent=log_indent)
                    resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

        return resource_file_name, resource, resource_info

    def processing_fingerprint(self):
        options = {"version": 2, "rows": self.partial_dataset_rows, "sample": self.partial_dataset_sample_mode,
                   "seed": self.partial_dataset_random_seed, "schema": self.extract_schema}
        return hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()

    def process_dataset_file(self, resource_info, dataset_file_name, dataset_path, resource, log_indent=4):
        if not os.path.isfile(dataset_path):
            return False
        size = os.path.getsize(dataset_path)
        self.ensure_disk_headroom(dataset_path, required_bytes=size, context="processing downloaded data")
        checksum = resource.get("checksum")
        if isinstance(checksum, str) and ":" in checksum:
            algorithm, expected = checksum.split(":", 1)
            if algorithm in {"md5", "sha256", "sha1"}:
                with open(dataset_path, "rb") as source:
                    actual = hashlib.file_digest(source, algorithm).hexdigest()
                if actual.lower() != expected.lower():
                    resource_info["fileInfo"].update(utils.add_tag_explanations("resource_temporarily_unavailable"))
                    resource_info["fileStatus"].pop("fileCompleted", None)
                    return False
        old_media_type = resource.get("mediaType")
        old_file_name = resource.get("fileName") or dataset_file_name
        mime, suggested_file_name, mismatch = utils.resolve_mediatype_conflict(old_media_type, SimpleNamespace(headers=resource.get("downloadHeaders", {})), resource.get("downloadURL"), dataset_file_name)
        signature = utils.detect_raw_signature(dataset_path)
        if signature:
            mime = signature
        if zipfile.is_zipfile(dataset_path):
            with zipfile.ZipFile(dataset_path) as archive:
                mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if "xl/workbook.xml" in archive.namelist() else "application/zip"
        if mime in {None, "application/octet-stream", "text/plain"}:
            with open(dataset_path, "rb") as source:
                prefix = source.read(512).lstrip(b"\xef\xbb\xbf \r\n\t")
            if prefix.startswith((b"{", b"[")):
                mime = "application/json"
            elif prefix.startswith(b"%PDF-"):
                mime = "application/pdf"
        if mismatch:
            resource_info.setdefault("fileMetadataChanges", {}).update(utils.add_tag_explanations("mimetype_mismatch", mismatch))
        elif mime:
            self.add_mimetype_mismatch(resource_info, old_media_type, old_file_name, mime, suggested_file_name or dataset_file_name)
        if mime:
            resource["mediaType"] = mime
        if mime == "text/html":
            resource_info["fileInfo"].update(utils.add_tag_explanations("html_page_downloaded"))
            resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
            resource.pop("path", None)
            os.remove(dataset_path)
            return "completed_without_file"
        if mime in {"application/json", "text/json", "application/geo+json"}:
            resource_info["fileInfo"]["jsonValidation"] = {"validated": size <= self.max_metadata_bytes, "maxBytes": self.max_metadata_bytes}
            if size <= self.max_metadata_bytes:
                try:
                    with open(dataset_path, encoding="utf-8-sig") as source:
                        payload = json.load(source)
                    if isinstance(payload, dict) and (payload.get("success") is False or (set(payload) <= {"error", "message", "status", "code", "errors"} and (payload.get("error") or payload.get("errors")))):
                        raise utils.PayloadError("API returned an error document")
                except (ValueError, UnicodeError, utils.PayloadError) as error:
                    resource_info["fileInfo"]["invalidPayload"] = {"reason": str(error)}
                    resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                    resource.pop("path", None)
                    os.remove(dataset_path)
                    return "completed_without_file"
        _, extension = utils.get_mime_and_ext(mime)
        if extension and not dataset_file_name.endswith("." + extension):
            new_name = dataset_file_name.rsplit(".", 1)[0] + "." + extension
            destination = os.path.join(self.save_path, new_name)
            os.replace(dataset_path, destination)
            dataset_path = destination
            resource["fileName"] = new_name
        resource["path"] = os.path.abspath(dataset_path)
        if extension in {"csv", "tsv"}:
            try:
                tabular_result = utils.process_tabular(self, dataset_path, resource, resource_info, log_indent=log_indent)
            except (ValueError, csv.Error, UnicodeError) as error:
                resource_info["fileInfo"]["tabularProcessingError"] = {"reason": str(error)}
                resource_info["fileStatus"].pop("fileCompleted", None)
                return False
            if tabular_result == "completed_without_file":
                resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                resource.pop("path", None)
                os.remove(dataset_path)
                return "completed_without_file"
            if tabular_result is False:
                resource_info["fileStatus"].pop("fileCompleted", None)
                return False
        resource["sizeBytes"] = os.path.getsize(dataset_path)
        resource["size"] = humanize.naturalsize(resource["sizeBytes"])
        resource["processingFingerprint"] = self.processing_fingerprint()
        resource_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
        return True

    def merge_existing_package_state(self, existing, package):
        utils.normalize_metadata(existing)
        def identity(resource):
            return str(resource.get("id") or resource.get("identifier") or resource.get("downloadURL"))
        old_by_id = {identity(resource): (name, resource) for name, resource in existing.get("resources", {}).items()}
        changed_package = existing.get("modified") != package.get("modified")
        resources, infos = {}, {}
        for name, resource in package.get("resources", {}).items():
            info = package["crawlerInfo"]["resourcesInfo"].get(name, utils.init_metadata(package=False, crawled=False))
            match = old_by_id.get(identity(resource))
            if match:
                old_name, old = match
                version_keys = ("modified", "checksum", "remoteHash", "remoteSize")
                signature_keys = ("downloadURL", "mediaType", *version_keys)
                unchanged = all(old.get(key) == resource.get(key) for key in signature_keys)
                has_version = any(resource.get(key) for key in ("modified", "checksum", "remoteHash"))
                if unchanged and (not changed_package or has_version):
                    resource = self.preserve_local_resource_fields(old, resource)
                    name = resource.get("fileName") or old_name
                    info = copy.deepcopy(existing.get("crawlerInfo", {}).get("resourcesInfo", {}).get(old_name, info))
                if resource.get("processingFingerprint") != self.processing_fingerprint():
                    info.get("fileStatus", {}).pop("fileCompleted", None)
            resources[name] = resource
            infos[name] = info
        package["resources"] = resources
        package["crawlerInfo"]["resourcesInfo"] = infos
        return package

    def metadata_file_name(self, key):
        name = f"meta_{utils.generate_short_filename(f'{self.domain}_{key}')}.json"
        if self.domain == "https://api.gbif.org" and not os.path.exists(os.path.join(self.save_path, name)):
            legacy = f"meta_{utils.generate_short_filename(f'http://api.gbif.org_{key}')}.json"
            if os.path.exists(os.path.join(self.save_path, legacy)):
                return legacy
        return name

    def scan_local_requirements(self, packages, explicit=False):
        pending = set()
        ttl = utils.get_config_option(self.dms or "defaults", "metadata_ttl_seconds", int, utils.get_config_option("defaults", "metadata_ttl_seconds", int, 86400))
        fingerprint = self.processing_fingerprint()
        for key in packages:
            name = self.metadata_file_name(key)
            path = os.path.join(self.save_path, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as source:
                    metadata = utils.normalize_metadata(json.load(source))
                resources = metadata.get("resources", {})
                if any(self.resource_matches_filters(n, r) and r.get("processingFingerprint") != fingerprint for n, r in resources.items()):
                    pending.add(key)
                checked = metadata.get("crawlerInfo", {}).get("packageStatus", {}).get("metadataChecked")
                age = (datetime.now() - datetime.fromisoformat(checked)).total_seconds() if checked else float("inf")
                if self.check_remote_updates and (explicit or age >= ttl or metadata.get("schemaVersion") != 2):
                    self.packages_requiring_refresh.add(key)
            except (OSError, ValueError, TypeError, AttributeError):
                self.packages_requiring_refresh.add(key)
        return pending

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages

    def get_package(self, pkg_id, metadata_file_name):
        package = self.dms_instance.get_package(pkg_id, metadata_file_name)
        return package
