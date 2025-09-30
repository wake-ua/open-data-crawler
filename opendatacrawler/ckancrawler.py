import requests
from urllib.parse import urlparse
from opendatacrawler import utils
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import threading
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class CkanCrawler():
    def __init__(self, domain, user_agent, max_sec):
        self.domain = domain.rstrip("/")
        self.user_agent = user_agent
        self.max_sec = max_sec

    def get_package_list(self):
        ids = []
        url = f"{self.domain}/api/3/action/package_list"

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers, max_sec=self.max_sec)
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

    def parse_resource(self, resource_meta, base_name, metadata_file_name, reparse_data=None):
        resource_crawler_info = utils.init_metadata(resource=True)

        if reparse_data:
            logger("...", f"Re-parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=4)
            resource = resource_meta
            meta_media_type = reparse_data.get("metaMediaType")
        else:
            logger("...", f"Parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=4)
            resource = {}
            resource["fileName"] = base_name

            resource["name"] = resource_meta.get("name")
            resource["description"] = resource_meta.get("description")

            resource["downloadURL"] = utils.fix_url(resource_meta.get("download_url") or resource_meta.get("url") or resource_meta.get("original_url"))

            meta_media_type = resource_meta.get("mimetype") or resource_meta.get("format") or ""

        response, self.user_agent, error_tag, e = utils.make_request(resource["downloadURL"], self.user_agent, return_tag=True, max_sec=self.max_sec)
        if not response:
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    resource_crawler_info["fileInfo"].update(utils.add_tag_explanations(error_tag))
                    logger("WARNING", f"Non-retryable error accessing resource '{base_name}' for parsing", e, indent=4)
                    resource_crawler_info["fileStatus"]["fileCompleted"] = datetime.now().isoformat()
                else:
                    resource_crawler_info["fileInfo"].update(utils.add_tag_explanations(error_tag, {"<metaMediaType>": meta_media_type}))
                    logger("WARNING", f"Retryable error accessing resource '{base_name}' for parsing", e, indent=4)
            else:
                logger("ERROR", f"Error accessing resource '{base_name}'", e, indent=4)
            
            return resource, resource_crawler_info

        media_type, file_name, tag_val = utils.resolve_mediatype_conflict(meta_media_type, response, base_name)
        resource["mediaType"] = media_type
        resource["fileName"] = file_name

        if tag_val:
            logger("WARNING", f"Detected a media type mismatch for file '{resource['downloadURL']}' '{base_name}'", indent=4)
            resource_crawler_info["fileMetadataChanges"].update(utils.add_tag_explanations("mimetype_mismatch", tag_val))

        if reparse_data:
            logger("OK", f"Successfully re-parsed resource '{resource["fileName"]}' from package '{metadata_file_name}'", indent=4)
        else:
            logger("OK", f"Successfully parsed resource '{resource["fileName"]}' from package '{metadata_file_name}'", indent=4)

        return resource, resource_crawler_info

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.domain}/api/3/action/package_show?id={package_id}")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://www.ckan.org/img/ckan-logo-256.png"

        response, self.user_agent, error_tag, e = utils.make_request(url, self.user_agent, headers=headers, return_tag=True)
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

        metadata["accessURL"] = utils.fix_url(f"{self.domain}/dataset/{data.get("name")}")

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

        metadata["source"] = self.domain

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
            logger("...", f"Parsing {len(distributions)} resources from package '{package_id}' ('{metadata_file_name}')...", indent=3)
            metadata["resources"] = {}
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"{threading.current_thread().name}") as executor:
                futures = []
                for idx, resource in enumerate(distributions):
                    futures.append(executor.submit(self.parse_resource, resource, utils.generate_short_filename(f"{metadata['fileName']}_{idx}"), metadata_file_name))

                for future in futures:
                    resource_meta, resource_crawler_info = future.result()
                    metadata["resources"][resource_meta["fileName"]] = resource_meta
                    metadata["crawlerInfo"]["resourcesInfo"][resource_meta["fileName"]] = resource_crawler_info

        return metadata
