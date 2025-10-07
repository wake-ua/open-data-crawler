import json
import os
import shutil
import requests
import humanize
import gc
import threading
from datetime import datetime
import traceback
from frictionless import describe, Dialect
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from opendatacrawler import utils
from opendatacrawler.datosgobescrawler import DatosGobEsCrawler
from opendatacrawler.ckancrawler import CkanCrawler
from opendatacrawler.zenodocrawler import ZenodoCrawler
from opendatacrawler.gbifcrawler import GbifCrawler
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class OpenDataCrawler():
    def __init__(self, domain, path=None, data_types=None, categories=None, partial=False, avoid_data=None, max_sec=None, max_threads=None, max_resource_threads=None):
        self.domain = domain.rstrip("/")
        self.dms = None
        self.dms_instance = None
        self.max_sec = max_sec
        self.max_threads = max_threads
        self.max_resource_threads = max_resource_threads

        base_path = path or os.path.join(os.getcwd(), "data")
        utils.create_folder(base_path)

        self.clean_domain = utils.clean_url(self.domain)
        self.save_path = os.path.join(base_path, self.clean_domain)

        self.data_types = data_types
        self.categories = categories
        self.partial = partial
        self.avoid_data = avoid_data

        self.user_agent = None

        logger("...", f"Detecting DMS for domain '{self.domain}'...", level="print")
        self.detect_dms()

    # ==============================

    def detect_dms(self):
        dms_endpoints = {
            #"dataEuropa": "/api/hub/repo/",
            #"Socrata": "/api/catalog/v1",
            #"datosGobEs": "/apidata/catalog/dataset?_sort=title&_pageSize=1",
            "CKAN": "/api/3/action/package_list",
            #"WorldBank": "/ddhxext/DatasetList",
            #"EuroStat": "/estat-navtree-portlet-prod/BulkDownloadListing?sort=1&dir=metadata",
            "Zenodo": "/oai2d?verb=Identify",
            "GBIF" : "/v1/dataset/search?limit=1&offset=0",
            "OpenDataSoft": "/api/v2/catalog",
            "INE": "/wstempus/js/ES/OPERACIONES_DISPONIBLES"
        }

        base_url = self.domain.rstrip("/")
        headers = {
            "Accept": "application/json",
        }

        for dms_name, endpoint in dms_endpoints.items():
            full_url = utils.fix_url(base_url + endpoint)
            logger("...", f"Checking DMS '{dms_name}' at '{full_url}'...", level="print")

            try:
                response, self.user_agent = utils.make_request(full_url, self.user_agent, headers=headers, max_sec=120)
                if not response:
                    logger("NET", f"No response from endpoint '{full_url}' while checking DMS '{dms_name}'")
                    continue

                if response.status_code != 200:
                    logger("NET", f"Non-successful response (HTTP {response.status_code}) from '{full_url}' while checking DMS '{dms_name}'")
                    continue

                response.raise_for_status()
                if "text/html" not in response.headers.get("Content-Type", ""):
                    self.dms = dms_name
                    logger("OK", f"DMS detected: '{dms_name}'", level="print")

                    if not utils.create_folder(self.save_path):
                        logger("ERROR", f"Can't create folder '{self.save_path}'")
                    break
                
            except requests.RequestException as e:
                logger("NET", f"Failed to reach '{full_url}'", [e, traceback.format_exc()])
                continue

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
            #"dataEuropa": DataEuropaCrawler
        }

        if self.dms:
            cls = dms_classes.get(self.dms)
            if cls:
                try:
                    self.dms_instance = cls(self)
                    if self.domain in ["https://zenodo.org/"]:
                        self.max_threads = 1
                        #self.max_resource_threads = 1
                except Exception:
                    logger("ERROR", f"Error instantiating DMS class for '{self.dms}'", f"\n{traceback.format_exc()}")
        else:
            logger("ERROR", f"No accessible or supported DMS detected at '{self.domain}'", level="print")

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

    def log_run_summary(self, downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, resume_data):
        new_downloads = len(downloaded_after_res) - len(downloaded_before_res)
        new_failures = max(0, len(failed_after_res) - len(failed_before_res))
        recovered = len(set(failed_before_res) - set(failed_after_res))

        logger("OK", f"{new_downloads} new resources downloaded in this run "
                     f"({new_failures} new failures, {recovered} recovered from previous failures): "
                     f"{len(downloaded_after_res)} successfully downloaded resources in total across {len(resume_data)} packages "
                     f"({len(failed_after_res)} failed resources in total)", level="print")

    def run_threaded_function(self, items, func, max_workers=1, thread_name_prefix=None, use_tqdm=False, tqdm_initial=0, tqdm_desc="", tqdm_colour=None):
        results = []
        if max_workers == 1:
            iterable = items
            if use_tqdm:
                iterable = tqdm(items, total=len(items) + tqdm_initial, initial=tqdm_initial, desc=tqdm_desc, colour=tqdm_colour)
            for item in iterable:
                results.append(func(item))
        else:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"{thread_name_prefix}") as executor:
                futures = [executor.submit(func, item) for item in items]
                try:
                    if use_tqdm:
                        futures_iter = tqdm(as_completed(futures), total=len(futures) + tqdm_initial, initial=tqdm_initial, desc=tqdm_desc, colour=tqdm_colour)
                    else:
                        futures_iter = as_completed(futures)
                    for future in futures_iter:
                        results.append(future.result())
                except KeyboardInterrupt:
                    for f in futures:
                        f.cancel()
                    executor.shutdown(wait=False)
                    raise
        return results

    def process_packages_batch(self, packages, tqdm_initial, tqdm_desc, tqdm_colour, downloaded_before_res, failed_before_res):
        try:
            self.run_threaded_function(
                items=packages, func=lambda pkg_id: self.process_package(pkg_id),
                max_workers=self.max_threads, thread_name_prefix="t",
                use_tqdm=True, tqdm_desc=tqdm_desc, tqdm_colour=tqdm_colour, tqdm_initial=tqdm_initial
            )
        except KeyboardInterrupt:
            logger(None, "=" * 80, level="print")
            if self.max_threads == 1:
                logger("WARNING", "Interrupt received, terminating execution", level="print")
            else:
                logger("WARNING", "Interrupt received, terminating all threads immediately", level="print")
            logger(None, "=" * 80, level="print")

            resume_data, downloaded_after_res, failed_after_res, _ = utils.recover_resume(save_path=self.save_path, accepted_types=self.data_types)
            self.log_run_summary(downloaded_before_res, failed_before_res, downloaded_after_res, failed_after_res, resume_data)
            os._exit(1)

    # ==============================

    def save_dataset(self, url, file_name, chunk_size=64*1024, log_indent=4):
        logger("...", f"Attempting to download resource '{file_name}' from '{url}'...", indent=log_indent)

        headers = {
            "Accept": "*/*", 
            "Connection": "keep-alive"
        }

        response, self.user_agent, error_tag, e = utils.make_request(url, self.user_agent, headers=headers, max_sec=self.max_sec, stream=True, return_tag=True)
        if error_tag:
            return None, error_tag, e

        try:
            path = os.path.join(self.save_path, file_name)
            total_bytes = 0
            line_limit = 50
            lines_downloaded = 0

            response.raw.decode_content = True
            try:
                with open(path, "wb") as outfile:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue

                        outfile.write(chunk)
                        total_bytes += len(chunk)

                        if self.partial:
                            lines_downloaded += chunk.count(b"\n")
                            if lines_downloaded >= line_limit:
                                logger("WARNING", f"Partial content downloaded (~{line_limit} lines) for '{file_name}'", indent=log_indent)
                                break
            except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError) as e:
                logger("WARNING", f"Chunked connection error while saving '{file_name}'", e, indent=log_indent)
                return None, "resource_temporarily_unavailable", e

            if not self.partial and total_bytes == 0:
                logger("WARNING", f"No data downloaded for resource '{file_name}'", indent=log_indent)
                return None, "no_data", None

            return path, None, None

        except Exception as e:
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

            old_package = None
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        old_package = json.load(f)
                except Exception:
                    old_package = None

            if old_package != package:
                all_resources_complete = True
                for _, res_info in package["crawlerInfo"]["resourcesInfo"].items():
                    if not res_info.get("fileStatus", {}).get("fileCompleted"):
                        all_resources_complete = False
                        break

                if all_resources_complete:
                    package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(package, f, ensure_ascii=False, indent=log_indent)

                logger("OK", f"Metadata saved successfully to '{meta_path}'", indent=log_indent)
            else:
                logger("SKIP", f"Metadata file '{meta_path}' already exists and is up-to-date", indent=log_indent)
        
            del package, old_package
            gc.collect()
        except Exception as e:
            logger("ERROR", f"Failed to save metadata file '{meta_path}'", [e, traceback.format_exc()], indent=log_indent)

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
                new_resource, new_info = self.parse_resource(resource_meta, old_file_name, package["fileName"], reparse_data=tag_values)
                new_file_name = new_resource["fileName"]

                if new_file_name != old_file_name:
                    logger("FIX", f"Resource name changed from '{old_file_name}' to '{new_file_name}'", indent=log_indent)
                    package["resources"].pop(old_file_name, None)
                    package["crawlerInfo"]["resourcesInfo"].pop(old_file_name, None)

                package["resources"][new_file_name] = new_resource
                package["crawlerInfo"]["resourcesInfo"][new_file_name] = new_info

                gc.collect()

        if missing_resources:
            logger("...", f"Re-parsing {len(missing_resources)} temporarily unavailable resources...", indent=log_indent)
            self.run_threaded_function(
                items=missing_resources, func=reparse_resource,
                max_workers=self.max_resource_threads, thread_name_prefix=threading.current_thread().name
            )

        return package

    def init_and_parse_resources(self, metadata, distributions, log_indent=2):
        metadata["resources"] = {}
        metadata["crawlerInfo"]["resourcesInfo"] = {}

        def parse_resource_func(item):
            idx, resource = item
            base_name = utils.generate_short_filename(f"{metadata['fileName']}_{idx}")
            resource["fileName"] = base_name
            metadata["resources"][base_name] = resource
            metadata["crawlerInfo"]["resourcesInfo"][base_name] = utils.init_metadata(package=False, crawled=False)

            parsed_resource, parsed_info = self.parse_resource(resource, base_name, metadata["fileName"], reparse_data=None)
            new_file_name = parsed_resource["fileName"]

            if new_file_name != base_name:
                logger("FIX", f"Resource name changed from '{base_name}' to '{new_file_name}'", indent=log_indent)
                metadata["resources"].pop(base_name, None)
                metadata["crawlerInfo"]["resourcesInfo"].pop(base_name, None)

            metadata["resources"][new_file_name] = parsed_resource
            metadata["crawlerInfo"]["resourcesInfo"][new_file_name] = parsed_info

        logger("...", f"Parsing {len(distributions)} resources from package '{metadata['fileName']}'...", indent=log_indent)
        self.run_threaded_function(
            items=list(enumerate(distributions)), func=parse_resource_func,
            max_workers=self.max_resource_threads, thread_name_prefix=threading.current_thread().name
        )

    def process_package(self, pkg_id, log_indent=1):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.domain}_{pkg_id}')}.json"
        try:
            metadata_path = os.path.join(self.save_path, metadata_file_name)
            logger("WORK", f"Processing package '{pkg_id}' ('{metadata_file_name}')...", indent=log_indent-1)

            if not os.path.exists(metadata_path):
                package = self.get_package(pkg_id, metadata_file_name)
            else:
                logger("WARNING", f"Metadata file already exists for package '{metadata_path}', loading and updating it if needed", indent=log_indent)
                with open(metadata_path, "r", encoding="utf-8") as f:
                    package = json.load(f)
                package = self.retry_temporarily_unavailable_resources(package)

            if not package:
                return

            if not package.get("resources"):
                logger("WARNING", f"No distributions found in package metadata '{pkg_id}' ('{metadata_path}')", indent=log_indent)
                package["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations("missing_distributions"))
                package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                self.save_metadata(package)
                return

            if self.categories:
                mapped_theme = utils.extract_mapped_field(package.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)
                if not (mapped_theme and any(cat in mapped_theme for cat in self.categories)):
                    logger("SKIP", f"Package '{pkg_id}' ('{metadata_path}') does not match specified categories ({', '.join(self.categories)}), skipping all resources", indent=log_indent)
                    self.save_metadata(package)
                    return

            resources_to_process = []
            for file_name, resource in package["resources"].items():
                if utils.is_completed(package, file_name, unavailable=True):
                    continue

                if self.avoid_data:
                    continue

                media_type = resource.get("mediaType")
                if not media_type:
                    logger("WARNING", f"Missing media type for resource '{file_name}' in package '{metadata_file_name}', skipping...", indent=log_indent)
                    continue

                ext = file_name.split(".")[-1].lower() if "." in file_name else None
                if self.data_types and ext and ext not in self.data_types:
                    logger("SKIP", f"Skipping resource '{file_name}' (media type '{media_type}' with extension '.{ext}' not in accepted types)", indent=log_indent)
                    continue

                resources_to_process.append((file_name, resource))

            self.run_threaded_function(
                items=resources_to_process, func=lambda item: self.process_resource(item[1], package, metadata_path),
                max_workers=self.max_resource_threads, thread_name_prefix=threading.current_thread().name
            )

            self.save_metadata(package)
            del package, metadata_path
            gc.collect()

        except Exception as e:
            logger("ERROR", f"Error processing package '{pkg_id}' ('{metadata_file_name}')", [e, traceback.format_exc()], indent=log_indent)

    def process_resource(self, resource, package, metadata_path, log_indent=3):
        resource_file_name = resource.get("fileName")
        download_url = resource.get("downloadURL")
        media_type = resource.get("mediaType")

        logger("...", f"Saving and processing resource '{resource_file_name}' from package '{metadata_path}'...", indent=log_indent)
        path, tag, e = self.save_dataset(download_url, resource_file_name)
        if path or tag:
            if path:
                resource["path"] = os.path.relpath(path, start=os.getcwd())
                package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileDownloaded"] = datetime.now().isoformat()
                logger("SAVE", f"Resource '{resource_file_name}' from package '{metadata_path}' downloaded successfully", indent=log_indent)

                process_success = self.process_dataset_file(package, resource_file_name, path, resource)
                if process_success:
                    logger("OK", f"Resource '{path}' successfully processed from package '{metadata_path}'", indent=log_indent)

            if tag:
                if tag == "resource_temporarily_unavailable":
                    logger("WARNING", f"Retryable error downloading resource '{resource_file_name}'", e, indent=log_indent)
                    package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileInfo"].update(utils.add_tag_explanations(tag, {"<metaMediaType>": media_type}))
                else:
                    package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileInfo"].update(utils.add_tag_explanations(tag))
                    if tag != "no_data":
                        logger("WARNING", f"Non-retryable error downloading resource '{resource_file_name}'", e, indent=log_indent)

                package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

        del resource_file_name, download_url, path, tag
        gc.collect()
        return package

    def process_dataset_file(self, package, dataset_file_name, dataset_path, resource, log_indent=4):
        if not os.path.exists(dataset_path):
            logger("ERROR", f"Dataset file '{dataset_path}' does not exist", indent=log_indent)
            return False

        try:
            if utils.MIME_TYPE_MAP.get(resource.get("mediaType"), {}).get("compressible", False):
                temp_path, tag = utils.check_file_empty_or_strip(dataset_path)

                if temp_path:
                    shutil.move(temp_path, dataset_path)
                    logger("FIX", f"File '{dataset_path}' content stripped and overwritten", indent=log_indent)
                    package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations("stripped_data"))

                if tag:
                    logger("WARNING", f"File '{dataset_path}' has no data or no valid content", indent=log_indent)
                    package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations(tag))
                    return False
            
                del temp_path, tag
                gc.collect()
        except Exception as e:
            logger("ERROR", f"File '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
            return False

        try:
            encoding, temp_path, raw_flags = utils.detect_best_encoding(dataset_path)
            if encoding:
                resource["encoding"] = encoding
            else:
                logger("ERROR", f"No matching encoding found for: {dataset_path}", indent=log_indent)

            if temp_path:
                shutil.move(temp_path, dataset_path)
                logger("FIX", f"Overwrote cleaned content into '{dataset_path}'", indent=log_indent)
            if raw_flags:
                package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations(raw_flags, raw_flags))

            del encoding, temp_path, raw_flags
            gc.collect()
        except Exception as e:
            logger("ERROR", f"Error while detecting encoding for: {dataset_path}", [e, traceback.format_exc()], indent=log_indent)
            return False
        
        if os.path.getsize(dataset_path) > 0 and dataset_path.endswith((".csv", ".tsv")):
            try:
                temp_path, tag = utils.process_fix_tabular(dataset_path, resource["encoding"])

                if temp_path:
                    shutil.move(temp_path, dataset_path)
                    logger("FIX", f"Cleaned and saved fixed tabular file to '{dataset_path}'", indent=log_indent)
                if tag:
                    for tag_key, tag_data in tag:
                        package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations(tag_key, tag_data))
                
                del temp_path, tag
                gc.collect()
            except Exception as e:
                logger("ERROR", f"Failed to clean tabular file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                return False

            try:
                delimiter, start_row = utils.detect_delimiter(dataset_path, resource["encoding"])
                if delimiter:
                    resource["delimiter"] = delimiter

                    if start_row:
                        package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("skip_rows", {"<skipped_rows>": start_row}))
                        dialect = Dialect.from_descriptor({"delimiter": delimiter, "comment_rows": [start_row]})
                    else:
                        dialect = Dialect.from_descriptor({"delimiter": delimiter})

                    try:
                        resource_metadata = describe(dataset_path, encoding=resource["encoding"], dialect=dialect).to_dict()
                        resource["schema"] = resource_metadata.get("schema")
                        logger("OK", f"Schema extracted from '{dataset_path}' (encoding: '{resource['encoding']}', delimiter: '{delimiter}', start_row: {start_row})", indent=4)
                    except Exception as e:
                        logger("ERROR", f"Failed to extract schema from file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                        return False

                    del delimiter, start_row, resource_metadata, dialect
                    gc.collect()
                else:
                    package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("no_delimiter_detected"))
                    logger("WARNING", f"File '{dataset_path}' appears to not contain a delimiter, likely not a structured/tabular file", indent=log_indent)
                    return False
            except Exception as e:
                logger("ERROR", f"Failed to detect delimiter for file '{dataset_path}'", [e, traceback.format_exc()], indent=log_indent)
                return False

        resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))
        package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

        del dataset_file_name, dataset_path, resource
        gc.collect()
        return True

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages

    def get_package(self, pkg_id, metadata_file_name):
        package = self.dms_instance.get_package(pkg_id, metadata_file_name)
        return package
    
    def parse_resource(self, resource_meta, base_name, metadata_file_name, reparse_data):
        resource, resource_crawler_info = self.dms_instance.parse_resource(resource_meta, base_name, metadata_file_name, reparse_data)
        return resource, resource_crawler_info