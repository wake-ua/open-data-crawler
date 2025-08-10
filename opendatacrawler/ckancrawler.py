import requests
import os
from urllib.parse import urlparse
from opendatacrawler import utils
from opendatacrawler.setup_logger import logger
import traceback
import json

class CkanCrawler():
    def __init__(self, domain, data_types, user_agent):
        self.domain = domain.rstrip("/")
        self.data_types = data_types
        self.user_agent = user_agent

    def get_package_list(self):
        ids = []
        url = self.domain+"/api/3/action/package_list"

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }
        
        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
            if not self.user_agent:
                logger("ERROR", f"Error fetching package list from '{self.domain}': no working User-Agent found")
                return ids
            
            response.raise_for_status()

            ids = response.json().get("result", [])

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}'", e)

        return ids
    
    def process_resource(self, resource, metadata_file_name, d_types, partial, avoid_data, save_dataset):
        resource_file_name = resource.get("fileName")
        if not resource_file_name:
            logger("ERROR", f"Missing filename for resource in package '{metadata_file_name}'", indent=4)
            return

        if avoid_data:
            logger("WARNING", f"Skipping resource '{resource_file_name}' of package '{metadata_file_name}'", indent=4)
            return

        download_url = resource.get("downloadURL")
        media_type = resource.get("mediaType")

        if not download_url or not media_type:
            logger("ERROR", f"Missing download URL or media type for resource '{resource_file_name}' in package '{metadata_file_name}'", indent=4)
            return

        ext = resource_file_name.split(".")[-1]
        if not d_types or ext in d_types:
            path = save_dataset(download_url, resource_file_name, partial)

            if path:
                resource["path"] = os.path.relpath(path, start=os.getcwd())
                logger("OK", f"Resource '{resource_file_name}' from package '{metadata_file_name}' saved", indent=4)

    def process_package(self, pkg_id, categories, d_types, partial, avoid_data, save_dataset, save_metadata, save_path):
        metadata_file_name = f"meta_{utils.generate_short_filename(f"{self.domain}_{pkg_id}")}.json"
        try:
            metadata_path = os.path.join(save_path, metadata_file_name)

            if not os.path.exists(metadata_path):
                package = self.get_package(pkg_id, metadata_file_name)
            else:
                logger("INFO", f"Metadata file already exists for package '{metadata_file_name}', loading and updating it if needed.", indent=2)
                with open(metadata_path, "r", encoding="utf-8") as f:
                    package = json.load(f)  

            if not package:
                return
            
            exist_cat = not categories or (package.get("theme") and any(cat in package["theme"] for cat in categories))
            logger("...", f"Processing package: '{metadata_file_name}'", indent=2)

            if exist_cat and package.get("resources"):
                for resource in package["resources"]:
                    if not resource.get("path"):
                        self.process_resource(resource, metadata_file_name, d_types, partial, avoid_data, save_dataset)

                save_metadata(package)

            logger("OK", f"Successfully processed package '{metadata_file_name}'", indent=2)
        except Exception as e:
            logger("ERROR", f"Error processing package '{pkg_id}'", e, indent=2)

    def parse_resource(self, data, base_name):
        resource = {}

        resource["name"] = data.get("name", None)
        resource["downloadURL"] = data.get("url", None)
        resource["sourceHost"] = urlparse(data.get("url", None)).netloc

        resource["mediaType"] = data.get("media_type")
        
        ext = utils.get_extension_mime(resource["mediaType"]) if resource["mediaType"] else data.get("format", "").lower()
        resource["fileName"] = utils.generate_short_filename(base_name, ext=ext)
    
        return resource

    def get_package(self, dataset_id, metadata_file_name):
        url = f"{self.domain}/api/3/action/package_show?id={dataset_id}"
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
        if not self.user_agent:
            logger("ERROR", f"No working User-Agent for URL '{url}'")
            return None
        
        response.raise_for_status()

        data = response.json()["result"]
        metadata = {}

        metadata["identifier"] = dataset_id
        metadata["accessURL"] = url
        metadata["fileName"] = metadata_file_name

        metadata["img"] = "https://www.ckan.org/img/ckan-logo-256.png"

        metadata["title"] = data.get("title", "")
        metadata["description"] = data.get("notes", "")

        metadata["language"] = data.get("language")
        
        theme = data.get("theme")
        if isinstance(theme, list):
            metadata["theme"] = [t.split("/")[-1] for t in theme]
        elif isinstance(theme, str):
            metadata["theme"] = theme.split("/")[-1]
        else:
            metadata["theme"] = None

        resource_list = []
        for idx, res in enumerate(data.get("resources", [])):
            resource_list.append(self.parse_resource(res, f"{metadata["fileName"]}_{idx}"))

        metadata["resources"] = resource_list
        metadata["modified"] = data.get("metadata_modified")
        metadata["issued"] = data.get("metadata_created")
        metadata["license"] = data.get("license_id", "unknown")
        metadata["source"] = self.domain

        metadata["temporal"] = {
            "startDate": data.get("temporal_begin_date", None),
            "endDate": data.get("temporal_end_date", None)
        }

        spatial = data.get("spatial")
        if spatial:
            if isinstance(spatial, list):
                metadata["geo"] = [s.split("/")[-1].replace("-", " ") for s in spatial]
            else:
                metadata["geo"] = spatial.split("/")[-1].replace("-", " ")

        return metadata