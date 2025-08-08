import os
import requests
import humanize
import json
from frictionless import describe
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
            return None

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

                    return path

            except requests.exceptions.RequestException as e:
                logger("ERROR", f"Error downloading '{file_name}'", e, indent=2)
                break

        return None

    def save_metadata(self, data):
        try:
            meta_path = os.path.join(self.save_path, data["fileName"])
            logger("...", f"Saving metadata to: {meta_path}", indent=2)

            if "resources" in data and isinstance(data["resources"], list):
                for resource in data["resources"]:
                    dataset_path = resource.get("path")

                    if dataset_path and os.path.exists(dataset_path):
                        try:
                            if "size" not in resource:
                                resource["size"] = humanize.naturalsize(os.path.getsize(dataset_path))
                                
                            if "schema" not in resource:
                                encoding_options = [
                                    utils.detect_encoding(dataset_path) or "utf-8",
                                    "latin1",
                                    "iso-8859-1"
                                ]

                                for encoding in encoding_options:
                                    try:
                                        resource["encoding"] = encoding
                                        resource_metadata = describe(dataset_path, encoding=encoding).to_dict()
                                        schema = resource_metadata.get("schema")

                                        if schema:
                                            resource["schema"] = schema

                                        logger("OK", f"Schema extracted from: {dataset_path} (encoding: {encoding})", indent=3)
                                        break
                                    except Exception:
                                        continue

                        except Exception as e:
                            logger("ERROR", f"Error extracting schema from: {dataset_path}", e, indent=3)

            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            logger("OK", f"Metadata saved successfully to: {meta_path}", indent=2)

        except Exception as e:
            logger("ERROR", f"Failed to save metadata file", e, indent=2)

    def get_package_list(self):
        packages = self.dms_instance.get_package_list()
        return packages
    
    def get_package(self, id):
        package = self.dms_instance.get_package(id)
        return package
    
    def process_package(self, pkg_id, categories, d_types, partial, avoid_data):
        return self.dms_instance.process_package(pkg_id, categories, d_types, partial, avoid_data, self.save_dataset, self.save_metadata, self.save_path)