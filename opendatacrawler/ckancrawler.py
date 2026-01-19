import requests
from urllib.parse import urlparse
from opendatacrawler import utils
import json
from datetime import datetime
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class CkanCrawler():
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

    def get_package_list(self):
        ids = []
        url = f"{self.odcrawler.domain}/api/3/action/package_list"

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        try:
            response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=headers, max_sec=self.odcrawler.max_sec)
            if not response:
                logger("ERROR", f"Error fetching package list from '{self.odcrawler.domain}': no working User-Agent found")
                return ids

            response.raise_for_status()
            ids = response.json().get("result", [])

            logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.odcrawler.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.odcrawler.domain}'", e)

        return ids

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("id")
        resource["name"] = resource_meta.get("name")

        resource["description"] = resource_meta.get("description")

        resource["downloadURL"] = utils.fix_url(resource_meta.get("download_url") or resource_meta.get("url") or resource_meta.get("original_url"))

        if resource["id"]:
            url = utils.fix_url(f"{self.odcrawler.domain}/api/3/action/datastore_search?resource_id={resource["id"]}")
            response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent)
            if response:
                schema_data = response.json()["result"]
                if schema_data:
                    resource["schema_og"] = {"fields": []}
                    for field in schema_data.get("fields", []):
                        resource["schema_og"]["fields"].append({
                            "name": field.get("id"),
                            "description": field.get("info", {}).get("notes", ""),
                            "type": field.get("type")
                        })

                    resource["rawDataSchema"] = {k: v for k, v in schema_data.items() if k != "records"}

        meta_media_type = resource_meta.get("mimetype") or resource_meta.get("format")

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.odcrawler.domain}/api/3/action/package_show?id={package_id}")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://www.ckan.org/img/ckan-logo-256.png"

        response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=headers, return_tag=True)
        if not response:
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    logger("WARNING", f"Non-retryable error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
                    metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(error_tag))
                    metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                    return metadata
                else:
                    logger("WARNING", f"Retryable error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
            else:
                logger("ERROR", f"Error accessing package '{package_id}' ('{metadata_file_name}')", e, indent=2)
            return None

        data = response.json()["result"]

        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/dataset/{data.get("name")}")

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

        if distributions:
            download_url = distributions[0].get("url") or distributions[0].get("original_url")
            if download_url:
                metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language", [])

        metadata["keyword"] = data.get("keywords", [])
        if not metadata["keyword"]:
            keywords = data.get("original_tags") or data.get("tags") or []
            if isinstance(keywords, list):
                for keyword in keywords:
                    if isinstance(keyword, dict):
                        state = keyword.get("state")
                        if not state or state == "active":
                            display_name = keyword.get("display_name") or keyword.get("name")
                            if display_name:
                                metadata["keyword"].append(display_name)
                    else:
                        metadata["keyword"].append(keyword)

        metadata["theme"] = []
        themes = data.get("theme") or data.get("groups")
        if isinstance(themes, list):
            for theme in themes:
                if isinstance(theme, dict):
                    display_name = theme.get("display_name") or theme.get("title")
                    if display_name:
                            metadata["theme"].append(display_name)
                else:
                    metadata["theme"].append(theme)

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("metadata_modified", "")
        metadata["issued"] = data.get("metadata_created", "")
        metadata["license"] = data.get("license_url", "") or data.get("license_id", "")
        if not metadata["license"] and distributions:
            metadata["license"] = distributions[0].get("license", "")

        metadata["source"] = self.odcrawler.domain

        temporals = data.get("temporal", {}) or data.get("temporals", {})
        if not isinstance(temporals, list):
            temporals = [temporals]
        
        for temporal in temporals:
            if isinstance(temporal, dict):
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

        if distributions:
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        metadata["rawData"] = data
        
        return metadata
