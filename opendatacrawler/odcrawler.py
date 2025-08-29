import json
import os
import requests
import humanize
from frictionless import describe, Dialect
from opendatacrawler import utils
from opendatacrawler.setup_logger import logger
from opendatacrawler.datosgobescrawler import DatosGobEsCrawler
from opendatacrawler.ckancrawler import CkanCrawler

class OpenDataCrawler():
    def __init__(self, domain, path=None, data_types=None, sec=None):
        self.domain = domain
        self.dms = None
        self.dms_instance = None
        self.max_sec = sec

        base_path = path or os.path.join(os.getcwd(), "data")
        utils.create_folder(base_path)

        clean_domain = utils.clean_url(self.domain)
        self.save_path = os.path.join(base_path, clean_domain)

        self.data_types = [x.lower() for x in data_types] if data_types else None

        self.user_agent = None

        logger("...", f"Detecting DMS for domain: {self.domain}")
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
            logger("...", f"Checking DMS: '{dms_name}' at '{full_url}'", level="print")

            try:
                response, self.user_agent = utils.make_request(full_url, self.user_agent, headers=headers)
                if not response:
                    logger("ERROR", f"Error checking DMS for '{dms_name}': no working User-Agent found")
                    continue
                
                response.raise_for_status()

                if "text/html" not in response.headers.get("Content-Type", ""):
                    self.dms = dms_name
                    logger("OK", f"DMS detected: '{dms_name}'", level="print")

                    if not utils.create_folder(self.save_path):
                        logger("ERROR", f"Can't create folder '{self.save_path}'")
                    break
                
            except requests.RequestException as e:
                logger("ERROR", f"Failed to reach '{full_url}'", e)
                break
            
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
                except Exception as e:
                    logger("ERROR", f"Error instantiating DMS class for '{self.dms}'", e)
        else:
            logger("ERROR", f"No accessible or supported DMS detected at '{self.domain}'", level="print")

    def save_dataset(self, url, file_name, partial=False):
        logger("...", f"Attempting to download resource '{file_name}' from: {url}", indent=2)

        if url.lower().endswith("html"):
            logger("WARNING", f"Resource '{file_name}' skipped (HTML detected): {url}", indent=2)
            return None, None

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
                        logger("WARNING", f"Forbidden with User-Agent: {self.user_agent} - trying next one", indent=2)
                        continue

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
                                    logger("OK", f"Partial content downloaded (~{line_limit} lines) for '{file_name}'", indent=2)
                                    break

                    if not partial and total_bytes == 0:
                        logger("ERROR", f"No data downloaded for resource '{file_name}'", indent=2)
                        return None

                    return path, None

            except requests.exceptions.RequestException as e:
                status_code = getattr(e.response, "status_code", None)
                tag = None

                if status_code:
                    if status_code == 400:
                        tag = "invalid_request"
                    elif status_code == 403:
                        tag = "forbidden_resource"
                    elif status_code == 404:
                        tag = "missing_resource"

                logger("ERROR", f"Error downloading '{file_name}' ({url})", e, indent=2)
                return None, tag

        return None, None

    def save_metadata(self, data):
        try:
            meta_path = os.path.join(self.save_path, data["fileName"])
            logger("...", f"Saving metadata to: {meta_path}", indent=2)

            if "resources" in data and isinstance(data["resources"], list):
                for resource in data["resources"]:
                    dataset_path = resource.get("path")

                    if dataset_path and os.path.exists(dataset_path):
                        try:
                            mismatch, guessed_extension, detected_mime, no_data = utils.check_mimetype_mismatch(dataset_path)
                        except Exception as e:
                            logger("ERROR", f"Error while checking MIME type for: {dataset_path}", e, indent=3)
                            continue
                        try:  
                            encoding = raw = raw_flags = None
                            if detected_mime.startswith("text/") or detected_mime in {"application/json", "application/xml"}:
                                if no_data:
                                    encoding = "utf-8"
                                else:
                                    encoding, raw, raw_flags, no_data = utils.detect_best_encoding(dataset_path)

                                resource["encoding"] = encoding
                        except Exception as e:
                            logger("ERROR", f"Error while detecting encoding for: {dataset_path}", e, indent=3)
                            continue

                        resource["crawlerChangesInfo"] = {
                                "resourceMetadataChanges": [],
                                "binaryFileChanges": [],
                                "fileInfo": []
                            }
                        
                        if mismatch or raw or raw_flags or no_data:
                            if mismatch:
                                logger("WARNING", f"Detected a media type mismatch for file '{dataset_path}', was declared as '{resource.get('mediaType')}', but detected as '{detected_mime}'")
                                utils.fix_mime_mismatch(resource, guessed_extension, detected_mime)
                                dataset_path = resource.get("path")
                            if no_data:
                                logger("WARNING", f"File '{dataset_path}' has no data or no valid content")
                                utils.add_tag_explanations(resource, "fileInfo", "no_data")
                            if raw_flags:
                                utils.add_tag_explanations(resource, "binaryFileChanges", raw_flags, raw_flags)
                            
                            if raw:
                                with open(dataset_path, "wb") as f:
                                    f.write(raw)
                                logger("OK", f"Overwrote cleaned content into '{dataset_path}'")

                        if dataset_path.endswith((".csv", ".tsv")) and not no_data:
                            with open(dataset_path, "rb") as f:
                                raw = f.read()

                            decoded_content = utils.safe_decode(raw, resource["encoding"])
                            try:
                                decoded_content, reconstructed_lines = utils.check_unbalanced_quotes(decoded_content)
                                if reconstructed_lines > 0:
                                    logger("WARNING", f"File '{dataset_path}' appears to contain broken multiline values, fixing")
                                    with open(dataset_path, "wb") as f:
                                        f.write(decoded_content.encode(resource["encoding"]).strip())

                                    utils.add_tag_explanations(resource, "binaryFileChanges", "reconstructed_lines", {"reconstructed_lines": reconstructed_lines})
                                    logger("OK", f"Fixed broken multiline values in file '{dataset_path}' by reconstructing logical rows")
                            except Exception as e:
                                logger("ERROR", f"Error during reconstruction for file '{dataset_path}'", e, indent=3)
                                continue
                        
                            if decoded_content.count("\n") < 1:
                                utils.add_tag_explanations(resource, "fileInfo", "one_line")
                                logger("WARNING", f"File '{dataset_path}' appears to contain only one line, likely not a structured/tabular file")
                                continue
                            
                            try:
                                delimiter, start_row = utils.detect_delimiter(decoded_content)
                                resource["delimiter"] = delimiter
                            except Exception as e:
                                logger("ERROR", f"Failed to detect delimiter for file '{dataset_path}'", e, indent=3)
                                continue

                            if start_row:
                                utils.add_tag_explanations(resource, "fileInfo", "skip_rows", {"skipped_rows": start_row})
                                dialect = Dialect.from_descriptor({"delimiter": delimiter, "comment_rows":[start_row]})
                            else:
                                dialect = Dialect.from_descriptor({"delimiter": delimiter})
                            
                            try:
                                resource_metadata = describe(dataset_path, encoding=resource["encoding"], dialect=dialect).to_dict()
                                resource["schema"] = resource_metadata.get("schema")
                                logger("OK", f"Schema extracted from: {dataset_path} (encoding: {encoding})", indent=3)
                            except Exception as e:
                                logger("ERROR", f"Error extracting schema from: {dataset_path}", e, indent=3)
                            
                        if "size" not in resource:
                            resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))

                        crawler_changes = resource.pop("crawlerChangesInfo", {
                            "resourceMetadataChanges": [],
                            "binaryFileChanges": [],
                            "fileInfo": []
                        })
                        crawler_changes["complete"] = True
                        resource["crawlerChangesInfo"] = crawler_changes

            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            logger("OK", f"Metadata saved successfully to: {meta_path}", indent=2)

        except Exception as e:
            logger("ERROR", "Failed to save metadata file", e, indent=2)

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages
    
    def get_package(self, id):
        package = self.dms_instance.get_package(id)
        return package
    
    def process_package(self, pkg_id, categories, d_types, partial, avoid_data):
        return self.dms_instance.process_package(pkg_id, categories, d_types, partial, avoid_data, self.save_dataset, self.save_metadata, self.save_path)