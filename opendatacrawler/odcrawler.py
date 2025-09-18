import json
import os
import shutil
import requests
import humanize
from datetime import datetime
import traceback
from frictionless import describe, Dialect
from opendatacrawler import utils
from opendatacrawler.datosgobescrawler import DatosGobEsCrawler
from opendatacrawler.ckancrawler import CkanCrawler
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

        self.data_types = [x.lower() for x in data_types] if data_types else None

        self.user_agent = None

        logger("...", f"Detecting DMS for domain '{self.domain}'...", level="print")
        self.detect_dms()

    def detect_dms(self):
        dms_endpoints = {
            "dataEuropa": "/api/hub/repo/",
            "Socrata": "/api/catalog/v1",
            "datosGobEs": "/apidata/catalog/dataset?_sort=title&_pageSize=1",
            "CKAN": "/api/3/action/package_list",
            "WorldBank": "/ddhxext/DatasetList",
            "EuroStat": "/estat-navtree-portlet-prod/BulkDownloadListing?sort=1&dir=metadata",
            "Zenodo": "/api/records/",
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
                response, self.user_agent = utils.make_request(full_url, self.user_agent, headers=headers)
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
            #"Zenodo": ZenodoCrawler,
            #"OpenDataSoft": OpenDataSoftCrawler,
            #"INE": INECrawler,
            #"dataEuropa": DataEuropaCrawler
        }

        if self.dms:
            cls = dms_classes.get(self.dms)
            if cls:
                try:
                    if self.dms in ["EuroStat", "Zenodo"]:
                        self.dms_instance = cls(self.domain, self.user_agent)
                    elif self.dms in ["OpenDataSoft", "INE"]:
                        self.dms_instance = cls(self.domain, self.save_path, self.user_agent)
                    elif self.dms in ["Socrata", "WorldBank", "dataEuropa", "datosGobEs"]:
                        self.dms_instance = cls(self.domain, self.data_types, self.user_agent)
                    else:
                        self.dms_instance = cls(self.domain, self.data_types, self.user_agent)
                except Exception:
                    logger("ERROR", f"Error instantiating DMS class for '{self.dms}'", f"\n{traceback.format_exc()}")
        else:
            logger("ERROR", f"No accessible or supported DMS detected at '{self.domain}'", level="print")

    def reset_domain(self, reset_domain, has_data, has_logs):
        if reset_domain and (has_data or has_logs):
            try:
                log_manager.move_to_domain(self.clean_domain, move_file=False)

                if has_data:
                    if os.path.exists(self.save_path):
                        shutil.rmtree(self.save_path)
                        logger("OK", f"Deleted data folder: {self.save_path}", level="print")

                if has_logs:
                    domain_logs_path = os.path.join(os.getcwd(), "logs", self.clean_domain)
                    if os.path.exists(domain_logs_path):
                        shutil.rmtree(domain_logs_path)
                        logger("OK", f"Deleted log folder: {domain_logs_path}", level="print")

                logger("OK", f"All data and logs for '{self.domain}' have been removed", level="print")
                utils.create_folder(self.save_path)
            except Exception:
                logger("ERROR", f"Failed to delete data/logs for '{self.domain}'", f"\n{traceback.format_exc()}", level="print")

        log_manager.move_to_domain(self.clean_domain, move_file=True)
        log_manager.clean_unused_logs()

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

    def save_metadata(self, data):
        try:
            file_name = data.get("fileName")
            if not file_name:
                logger("ERROR", "No 'fileName' defined in metadata, can't save metadata file", indent=2)
                return

            meta_path = os.path.join(self.save_path, file_name)
            logger("...", f"Saving metadata to '{meta_path}'...", indent=2)
            if "resources" in data:
                for dataset_file_name, resource in data["resources"].items():
                    dataset_path = os.path.join(self.save_path, dataset_file_name)
                    if not os.path.exists(dataset_path):
                        continue

                    if not utils.is_completed(data, dataset_file_name):
                        mime_type = resource.get("mediaType")
                        try:
                            encoding = temp_path = raw_flags = no_data = None
                            if utils.MIME_TYPE_MAP.get(mime_type, {}).get("compressible", False):
                                encoding, temp_path, raw_flags, no_data = utils.detect_best_encoding(dataset_path)
                                if encoding:
                                    resource["encoding"] = encoding
                                else:
                                    logger("ERROR", f"No matching encoding found for: {dataset_path}", indent=3)
                        except Exception:
                            logger("ERROR", f"Error while detecting encoding for: {dataset_path}", f"\n{traceback.format_exc()}", indent=3)
                            continue

                        if temp_path or raw_flags or no_data or not resource.get("mediaType"):
                            if no_data:
                                logger("WARNING", f"File '{dataset_path}' has no data or no valid content", indent=3)
                                data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("no_data"))
                            if raw_flags:
                                data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations(raw_flags, raw_flags))

                            if temp_path:
                                try:
                                    shutil.move(temp_path, dataset_path)
                                    logger("OK", f"Overwrote cleaned content into '{dataset_path}'", indent=3)
                                except Exception as e:
                                    logger("ERROR", f"Failed to move temp file to '{dataset_path}'", e, indent=3)

                        if dataset_path.endswith((".csv", ".tsv")) and not no_data:
                            with open(dataset_path, "rb") as f:
                                raw = f.read()  
                                
                            decoded_content = utils.safe_decode(raw, resource["encoding"])
                            try:
                                decoded_content, was_stripped = utils.strip_outer_quotes(decoded_content)
                                if was_stripped:
                                    logger("WARNING", f"File '{dataset_path}' appears to have unnecessary outer quotes, attempting removal...", indent=3)
                                    data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations("stripped_outer_quotes"))
                            except Exception as e:
                                logger("ERROR", f"Error during outer quote stripping for file '{dataset_path}'", e, indent=3)
                                continue

                            try:
                                decoded_content, reconstructed_lines = utils.check_unbalanced_quotes(decoded_content)
                                if reconstructed_lines > 0:
                                    logger("WARNING", f"File '{dataset_path}' appears to contain broken multiline values, attempting reconstruction...", indent=3)
                                    data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["binaryFileChanges"].update(utils.add_tag_explanations("reconstructed_lines", {"<reconstructed_lines>": reconstructed_lines}))
                            except Exception as e:
                                logger("ERROR", f"Error during line reconstruction for file '{dataset_path}'", e, indent=3)
                                continue

                            if was_stripped or reconstructed_lines > 0:
                                with open(dataset_path, "wb") as f:
                                    f.write(decoded_content.encode(resource["encoding"]).strip())

                                if was_stripped:
                                    logger("OK", f"Removed unnecessary outer quotes in file '{dataset_path}'", indent=3)
                                if reconstructed_lines > 0:
                                    logger("OK", f"Reconstructed {reconstructed_lines} multiline rows in file '{dataset_path}'", indent=3)

                            if decoded_content.count("\n") < 1:
                                data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("one_line"))
                                logger("WARNING", f"File '{dataset_path}' appears to contain only one line, likely not a structured/tabular file", indent=3)
                                continue

                            try:
                                delimiter, start_row = utils.detect_delimiter(decoded_content)
                            except Exception as e:
                                logger("ERROR", f"Failed to detect delimiter for file '{dataset_path}'", e, indent=3)
                                continue

                            if delimiter:
                                resource["delimiter"] = delimiter
                                if start_row:
                                    data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("skip_rows", {"<skipped_rows>": start_row}))
                                    dialect = Dialect.from_descriptor({"delimiter": delimiter, "comment_rows": [start_row]})
                                else:
                                    dialect = Dialect.from_descriptor({"delimiter": delimiter})

                                try:
                                    resource_metadata = describe(dataset_path, encoding=resource["encoding"], dialect=dialect).to_dict()
                                    resource["schema"] = resource_metadata.get("schema")
                                    logger("OK", f"Schema extracted from '{dataset_path}' (encoding: '{resource['encoding']}', delimiter: '{delimiter}', start_row: {start_row})", indent=3)
                                except Exception as e:
                                    logger("ERROR", f"Failed to extract schema from file '{dataset_path}'", e, indent=3)
                            else:
                                data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileInfo"].update(utils.add_tag_explanations("no_delimiter_detected"))
                                logger("WARNING", f"File '{dataset_path}' appears to not contain a delimiter, likely not a structured/tabular file", indent=3)
                                continue

                        if "size" not in resource:
                            resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))

                        data["crawlerInfo"]["resourcesInfo"][dataset_file_name]["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                        logger("OK", f"Resource '{dataset_path}' saved successfully from package '{meta_path}'", indent=3)
            else:
                logger("WARNING", f"No distributions found in package metadata '{meta_path}'", indent=2)
                data["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations("no_resources"))
                data["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

            if "crawlerInfo" in data:
                data["crawlerInfo"] = data.pop("crawlerInfo")

            old_data = None
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                except Exception:
                    old_data = None

            if old_data != data:
                all_resources_complete = True
                for _, res_info in data["crawlerInfo"]["resourcesInfo"].items():
                    if not res_info.get("fileStatus", {}).get("fileCompleted"):
                        all_resources_complete = False
                        break

                if all_resources_complete:
                    data["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)

                logger("OK", f"Metadata saved successfully to: {meta_path}", indent=2)
                
            else:
                logger("WARNING", f"Metadata file '{meta_path}' already exists and is up-to-date", indent=2)
        except Exception as e:
            logger("ERROR", f"Failed to save metadata file '{meta_path}'", [e, traceback.format_exc()], indent=2)

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages

    def get_package(self, pkg_id, metadata_file_name):
        package = self.dms_instance.get_package(pkg_id, metadata_file_name)
        return package

    def process_package(self, pkg_id, categories, d_types, partial, avoid_data):
        metadata_file_name = f"meta_{utils.generate_short_filename(f"{self.domain}_{pkg_id}")}.json"
        try:
            metadata_path = os.path.join(self.save_path, metadata_file_name)

            if not os.path.exists(metadata_path):
                package = self.get_package(pkg_id, metadata_file_name)
            else:
                logger("INFO", f"Metadata file already exists for package '{metadata_path}', loading and updating it if needed", indent=2)
                with open(metadata_path, "r", encoding="utf-8") as f:
                    package = json.load(f)

            if not package:
                return

            should_process = True
            if categories:
                mapped_theme = utils.extract_mapped_field(package.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)
                should_process = mapped_theme and any(cat in mapped_theme for cat in categories)

            if should_process and package.get("resources"):
                logger("...", f"Processing package: '{metadata_path}'...", indent=2)
                for file_name, resource in package["resources"].items():
                    if not utils.is_completed(package, file_name):
                        package = self.process_resource(resource, package, metadata_path, d_types, partial, avoid_data)

                self.save_metadata(package)
                logger("OK", f"Successfully processed package '{metadata_path}'", indent=2)
            else:
                if not should_process:
                    logger("WARNING", f"Package '{pkg_id}' does not match specified categories: {', '.join(categories)}", indent=2)
                elif not package.get("resources"):
                    logger("WARNING", f"Package '{pkg_id}' has no resources", indent=2)
        except Exception as e:
            logger("ERROR", f"Error processing package '{pkg_id}'", [e, traceback.format_exc()], indent=2)

    def process_resource(self, resource, package, metadata_path, d_types, partial, avoid_data):
        resource_file_name = resource.get("fileName")
        if not resource_file_name:
            logger("ERROR", f"Missing filename for resource in package '{metadata_path}'", indent=4)

        if avoid_data:
            logger("....", f"Skipping resource '{resource_file_name}' of package '{metadata_path}'...", indent=4)
            return package

        download_url = resource.get("downloadURL")
        if not download_url:
            logger("ERROR", f"Missing download URL for resource '{resource_file_name}' in package '{metadata_path}'", indent=4)
            return package

        media_type = resource.get("mediaType")
        if not media_type:
            logger("WARNING", f"Missing media type for resource '{resource_file_name}' in package '{metadata_path}', downloading anyways...", indent=4)

        _, ext = os.path.splitext(resource_file_name)
        if not d_types or ext.lstrip(".") in d_types or not ext:
            path, tag = self.save_dataset(download_url, resource_file_name, partial)

            if path:
                resource["path"] = os.path.relpath(path, start=os.getcwd())
                package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileDownloaded"] = datetime.now().isoformat()

            if tag:
                package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileInfo"].update(utils.add_tag_explanations(tag))
                package["crawlerInfo"]["resourcesInfo"][resource_file_name]["fileStatus"]["fileCompleted"] = datetime.now().isoformat()

        return package
