import json
import os
import shutil
import requests
import humanize
import gc
from datetime import datetime
import traceback
from frictionless import describe, Dialect
from opendatacrawler import utils
from opendatacrawler.datosgobescrawler import DatosGobEsCrawler
from opendatacrawler.ckancrawler import CkanCrawler
from opendatacrawler.zenodocrawler import ZenodoCrawler
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class OpenDataCrawler():
    def __init__(self, domain, path=None, data_types=None, sec=None):
        self.domain = domain
        self.dms = None
        self.dms_instance = None
        self.max_sec = sec

        base_path = path or os.path.join(os.getcwd(), "data")
        utils.create_folder(base_path)

        self.clean_domain = utils.clean_url(self.domain)
        self.save_path = os.path.join(base_path, self.clean_domain)

        self.data_types = data_types

        self.user_agent = None

        logger("...", f"Detecting DMS for domain '{self.domain}'...", level="print")
        self.detect_dms()

    # ==============================
    # DMS detection and setup functions
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
            "OpenDataSoft": "/api/v2/catalog",
            "INE": "/wstempus/js/ES/OPERACIONES_DISPONIBLES"
        }

        base_url = self.domain.rstrip("/")

        headers = {
            "Accept": "application/json",
        }

        for dms_name, endpoint in dms_endpoints.items():
            full_url = base_url + endpoint
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
                
            except requests.RequestException:
                logger("NET", f"Failed to reach '{full_url}'", f"\n{traceback.format_exc()}")
                continue

        dms_classes = {
            "CKAN": CkanCrawler,
            #"Socrata": SocrataCrawler,
            #"WorldBank": WorldBankCrawler,
            #"EuroStat": EurostatCrawler,
            "datosGobEs": DatosGobEsCrawler,
            "Zenodo": ZenodoCrawler,
            #"OpenDataSoft": OpenDataSoftCrawler,
            #"INE": INECrawler,
            #"dataEuropa": DataEuropaCrawler
        }

        if self.dms:
            cls = dms_classes.get(self.dms)
            if cls:
                try:
                    if self.dms in ["EuroStat"]:
                        self.dms_instance = cls(self.domain, self.user_agent)
                    elif self.dms in ["OpenDataSoft", "INE"]:
                        self.dms_instance = cls(self.domain, self.save_path, self.user_agent)
                    elif self.dms in ["Socrata", "WorldBank", "dataEuropa", "datosGobEs", "Zenodo"]:
                        self.dms_instance = cls(self.domain, self.data_types, self.user_agent)
                    else:
                        self.dms_instance = cls(self.domain, self.data_types, self.user_agent)
                except Exception:
                    logger("ERROR", f"Error instantiating DMS class for '{self.dms}'", f"\n{traceback.format_exc()}")
        else:
            logger("ERROR", f"No accessible or supported DMS detected at '{self.domain}'", level="print")

    # ==============================
    # Cleanup and reset functions
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

    # ==============================
    # Dataset download and processing functions
    # ==============================

    def save_dataset(self, url, file_name, partial=False):
        logger("...", f"Attempting to download resource '{file_name}' from '{url}'...", indent=2)

        all_forbidden = True
        for user_agent in utils.get_user_agent_list(self.user_agent):
            self.user_agent = user_agent
            headers = {
                "Accept": "*/*",
                "User-Agent": self.user_agent,
                "Connection": "keep-alive"
            }

            try:
                with requests.get(url, stream=True, timeout=self.max_sec, verify=False, headers=headers) as response:
                    if response.status_code == 403:
                        logger("NET", f"Forbidden access to url '{url}' with User-Agent '{self.user_agent}' (HTTP 403 - Forbidden), trying next one...", indent=2)
                        continue

                    all_forbidden = False
                    response.raise_for_status()

                    path = os.path.join(self.save_path, file_name)
                    total_bytes = 0
                    line_limit = 50
                    lines_downloaded = 0

                    with open(path, "wb") as outfile:
                        for chunk in response.iter_content(chunk_size=1024):
                            if not chunk:
                                continue

                            outfile.write(chunk)
                            total_bytes += len(chunk)

                            if partial:
                                lines_downloaded += chunk.count(b"\n")
                                if lines_downloaded >= line_limit:
                                    logger("WARNING", f"Partial content downloaded (~{line_limit} lines) for '{file_name}'", indent=2)
                                    break

                    if not partial and total_bytes == 0:
                        logger("WARNING", f"No data downloaded for resource '{file_name}'", indent=2)
                        return None, "no_data"

                    return path, None
            except requests.exceptions.SSLError as e:
                logger("ERROR", f"SSL error downloading '{file_name}' ({url})", e, indent=3)
                return None, "ssl_error"
            except requests.exceptions.RequestException as e:
                status_code = getattr(e.response, "status_code", None)
                tag = None
                if status_code:
                    tag = utils.get_https_error_tag(status_code)
                
                logger("ERROR", f"Error downloading '{file_name}' ({url})", e, indent=2)
                return None, tag

        if all_forbidden:
            logger("ERROR", f"Error downloading '{file_name}' ({url}): resource is not publicly accessible (HTTP 403)", indent=2)
            return None, "forbidden_resource"

        return None, None

    def save_metadata(self, package):
        try:
            file_name = package.get("fileName")
            if not file_name:
                logger("ERROR", "No 'fileName' defined in metadata, can't save metadata file", indent=2)
                return

            meta_path = os.path.join(self.save_path, file_name)
            logger("SAVE", f"Saving metadata to '{meta_path}'...", indent=2)

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
                    json.dump(package, f, ensure_ascii=False, indent=4)

                logger("OK", f"Metadata saved successfully to '{meta_path}'", indent=2)
            else:
                logger("SKIP", f"Metadata file '{meta_path}' already exists and is up-to-date", indent=2)
        
            del package, old_package
            gc.collect()
        except Exception as e:
            logger("ERROR", f"Failed to save metadata file '{meta_path}'", [e, traceback.format_exc()], indent=2)

    def process_package(self, pkg_id, categories, d_types, partial, avoid_data):
        metadata_file_name = f"meta_{utils.generate_short_filename(f'{self.domain}_{pkg_id}')}.json"
        try:
            metadata_path = os.path.join(self.save_path, metadata_file_name)

            if not os.path.exists(metadata_path):
                package = self.get_package(pkg_id, metadata_file_name)
            else:
                logger("WARNING", f"Metadata file already exists for package '{metadata_path}', loading and updating it if needed", indent=2)
                with open(metadata_path, "r", encoding="utf-8") as f:
                    package = json.load(f)

                package = self.retry_temporarily_unavailable_resources(package)

            if not package:
                return

            should_process = True
            if categories:
                mapped_theme = utils.extract_mapped_field(package.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)
                should_process = mapped_theme and any(cat in mapped_theme for cat in categories)

            if should_process and package.get("resources"):
                logger("WORK", f"Processing package: '{metadata_path}'...", indent=2)
                for file_name, resource in list(package["resources"].items()):
                    if not utils.is_completed(package, file_name, unavailable=True):
                        package = self.process_resource(resource, package, metadata_path, d_types, partial, avoid_data)
                self.save_metadata(package)
            else:
                if not should_process:
                    logger("WARNING", f"Package '{pkg_id}' does not match specified categories: {', '.join(categories)}", indent=2)
                elif not package.get("resources"):
                    logger("WARNING", f"No distributions found in package metadata '{pkg_id}' ('{metadata_path}')", indent=2)
                    package["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations("no_resources"))
                    package["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                    self.save_metadata(package)

            del package, metadata_path
            gc.collect()

        except Exception as e:
            logger("ERROR", f"Error processing package '{pkg_id}'", [e, traceback.format_exc()], indent=2)

    def retry_temporarily_unavailable_resources(self, package):
        missing_resources = [
            file_name for file_name, _ in package["crawlerInfo"]["resourcesInfo"].items()
            if utils.is_completed(package, file_name, complete=False, unavailable=True)
        ]

        if missing_resources:
            logger("...", f"Re-parsing {len(missing_resources)} temporarily unavailable resources...", indent=2)
            for old_file_name in missing_resources:
                resource_meta = package["resources"].get(old_file_name)
                info = package["crawlerInfo"]["resourcesInfo"].get(old_file_name, {})

                tag_info = info.get("fileInfo", {}).get("resource_temporarily_unavailable", {})
                tag_values = tag_info.get("values", {})

                if resource_meta and tag_values:
                    new_resource, new_info = self.parse_resource(resource_meta, old_file_name, reparse_data=tag_values)
                    new_file_name = new_resource["fileName"]

                    if new_file_name != old_file_name:
                        logger("FIX", f"Resource name changed from '{old_file_name}' to '{new_file_name}'", indent=3)
                        package["resources"].pop(old_file_name, None)
                        package["crawlerInfo"]["resourcesInfo"].pop(old_file_name, None)

                    package["resources"][new_file_name] = new_resource
                    package["crawlerInfo"]["resourcesInfo"][new_file_name] = new_info

                    del new_resource, new_info, new_file_name
                del old_file_name, resource_meta, info, tag_info, tag_values
                gc.collect()

        return package

    def process_resource(self, resource, package, metadata_path, d_types, partial, avoid_data):
        resource_file_name = resource.get("fileName")
        if avoid_data:
            logger("SKIP", f"Skipping resource '{resource_file_name}' of package '{metadata_path}'...", indent=3)
            return package

        download_url = resource.get("downloadURL")
        if not download_url:
            logger("ERROR", f"Missing download URL for resource '{resource_file_name}' in package '{metadata_path}'", indent=3)
            return package

        media_type = resource.get("mediaType")
        if not media_type:
            logger("WARNING", f"Missing media type for resource '{resource_file_name}' in package '{metadata_path}'...", indent=3)
            return package

        ext = resource_file_name.split(".")[-1].lower() if "." in resource_file_name else None
        if not d_types or ext in d_types or not ext:
            path, tag = self.save_dataset(download_url, resource_file_name, partial)

            if path or tag:
                if path:
                    resource["path"] = os.path.relpath(path, start=os.getcwd())
                    package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileDownloaded"] = datetime.now().isoformat()
                    logger("SAVE", f"Resource '{resource_file_name}' from package '{metadata_path}' downloaded successfully", indent=3)

                    process_success = self.process_dataset_file(package, resource_file_name, path, resource)
                    if process_success:
                        logger("OK", f"Resource '{path}' saved successfully processed from package '{metadata_path}'", indent=3)

                if tag:
                    package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileInfo"].update(utils.add_tag_explanations(tag))
                    package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

            del resource_file_name, download_url, media_type, ext, path, tag
            gc.collect()
        return package

    def process_dataset_file(self, package, dataset_file_name, dataset_path, resource):
        if not os.path.exists(dataset_path):
            logger("ERROR", f"Dataset file '{dataset_path}' does not exist", indent=4)
            return False

        try:
            if utils.MIME_TYPE_MAP.get(resource.get("mediaType"), {}).get("compressible", False):
                temp_path, tag = utils.check_file_empty_or_strip(dataset_path)

                if temp_path:
                    shutil.move(temp_path, dataset_path)
                    logger("FIX", f"File '{dataset_path}' content stripped and overwritten", indent=4)
                    package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations("stripped_data"))

                if tag:
                    logger("WARNING", f"File '{dataset_path}' has no data or no valid content", indent=4)
                    package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations(tag))
                    return False
            
                del temp_path, tag
                gc.collect()
        except Exception as e:
            logger("ERROR", f"File '{dataset_path}'", [e, traceback.format_exc()], indent=4)
            return False

        try:
            encoding, temp_path, raw_flags = utils.detect_best_encoding(dataset_path)
            if encoding:
                resource["encoding"] = encoding
            else:
                logger("ERROR", f"No matching encoding found for: {dataset_path}", indent=4)

            if temp_path:
                shutil.move(temp_path, dataset_path)
                logger("FIX", f"Overwrote cleaned content into '{dataset_path}'", indent=4)

            if raw_flags:
                package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations(raw_flags, raw_flags))

            del encoding, temp_path, raw_flags
            gc.collect()
        except Exception as e:
            logger("ERROR", f"Error while detecting encoding for: {dataset_path}", [e, traceback.format_exc()], indent=4)
            return False
        
        if os.path.getsize(dataset_path) > 0 and dataset_path.endswith((".csv", ".tsv")):
            try:
                temp_path, tag = utils.process_fix_tabular(dataset_path, resource["encoding"])

                if temp_path:
                    shutil.move(temp_path, dataset_path)
                    logger("FIX", f"Cleaned and saved fixed tabular file to '{dataset_path}'", indent=4)

                if tag:
                    for tag_key, tag_data in tag:
                        package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations(tag_key, tag_data))
                
                del temp_path, tag
                gc.collect()
            except Exception as e:
                logger("ERROR", f"Failed to clean tabular file '{dataset_path}'", [e, traceback.format_exc()], indent=4)
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
                    logger("ERROR", f"Failed to extract schema from file '{dataset_path}'", [e, traceback.format_exc()], indent=4)
                    return False

                del delimiter, start_row, resource_metadata, dialect
                gc.collect()
            else:
                package["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("no_delimiter_detected"))
                logger("WARNING", f"File '{dataset_path}' appears to not contain a delimiter, likely not a structured/tabular file", indent=4)
                return False
        except Exception as e:
            logger("ERROR", f"Failed to detect delimiter for file '{dataset_path}'", [e, traceback.format_exc()], indent=4)
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
    
    def parse_resource(self, resource_meta, base_name, reparse_data):
        resource, resource_crawler_info = self.dms_instance.parse_resource(resource_meta, base_name, reparse_data)
        return resource, resource_crawler_info