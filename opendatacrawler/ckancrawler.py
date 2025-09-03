import requests
import os
from urllib.parse import urlparse
from opendatacrawler import utils
import traceback
import json
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class CkanCrawler():
    def __init__(self, domain, data_types, user_agent):
        self.domain = domain.rstrip("/")
        self.data_types = data_types
        self.user_agent = user_agent

    def get_package_list(self):
        ids = []
        url = f"{self.domain}/api/3/action/package_list"

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }
        
        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
            if not response:
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

    def parse_resource(self, data, base_name):
        resource = {}

        resource["name"] = data.get("name", {})
        resource["description"] = data.get("description", {})

        resource["downloadURL"] = data.get("url") or data.get("original_url")

        mimetype = data.get("mimetype") or data.get("format")
        if "/" in mimetype:
            resource["mediaType"] = mimetype
        else:
            resource["mediaType"] = utils.get_mime_extension(mimetype)

        resource["fileName"] = utils.generate_short_filename(base_name, ext=utils.get_extension_mime(resource["mediaType"]))

        return resource

    def get_package(self, dataset_id, metadata_file_name):
        url = f"{self.domain}/api/3/action/package_show?id={dataset_id}"
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
        if not response:
            logger("ERROR", f"No working User-Agent for URL '{url}'")
            return None

        response.raise_for_status()

        data = response.json()["result"]
        metadata = {}

        metadata["identifier"] = dataset_id
        metadata["requestURL"] = url
        metadata["accessURL"] = f"{self.domain}/dataset/{data.get("name")}"

        metadata["fileName"] = metadata_file_name

        metadata["img"] = "https://www.ckan.org/img/ckan-logo-256.png"

        metadata["title"] = data.get("title", {})
        metadata["description"] = data.get("notes", {})

        distributions = data.get("resources", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        publisher = data.get("organization", {})
        metadata["publisher"] = {
            "identifier": publisher.get("id", ""),
            "title": utils.extract_first_nonempty_value(publisher.get("title", "")),
        }

        download_url = distributions[0].get("url") or distributions[0].get("original_url")
        if download_url:
            metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language", [])

        metadata["keyword"] = data.get("keywords", {}) or data.get("original_tags", []) or data.get("tags", [])

        # no es tags solo, va cambindo, ojo https://ckan.opendata.swiss/api/3/action/package_show?id=todesfalle-nach-monat-stadtquartier-geschlecht-altersgruppe-und-herkunft-seit-1998

        #theme = data.get("theme")
        #if isinstance(theme, list):
        #    metadata["theme"] = [t.split("/")[-1] for t in theme]
        #elif isinstance(theme, str):
        #    metadata["theme"] = theme.split("/")[-1]
        #else:
        #    metadata["theme"] = None

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("metadata_modified", "")
        metadata["issued"] = data.get("metadata_created", "")
        metadata["license"] = data.get("license_url", "") or data.get("license_id", "")
        if not metadata["license"] and distributions:
            metadata["license"] = distributions[0].get("license", "")

        metadata["source"] = self.domain

        temporal = data.get("temporal", {}) or data.get("temporals", {})
        metadata["temporal"] = {
            "startDate": temporal.get("startDate") or temporal.get("start_date"),
            "endDate": temporal.get("endDate") or temporal.get("end_date"),
        }

        location = data.get("location", "")
        spatial = data.get("spatial", "")
        geo = utils.extract_mapped_field(location, utils.CKANCRAWLER_SPATIAL_MAP)
        if utils.is_geojson(spatial):
            metadata["spatial"] = json.loads(spatial) if isinstance(spatial, str) else spatial
        else:
            if not geo:
                geo = utils.extract_mapped_field(spatial, utils.CKANCRAWLER_SPATIAL_MAP)
            if geo:
                metadata["geo"] = geo

        resource_list = []
        for idx, res in enumerate(distributions):
            resource_list.append(self.parse_resource(res, f"{metadata["fileName"]}_{idx}"))

        metadata["resources"] = resource_list

        return metadata
