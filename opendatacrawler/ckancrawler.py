import requests
from urllib.parse import urlparse
from opendatacrawler import utils
import json
from datetime import datetime
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

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource_crawler_info = utils.init_metadata(resource=True)

        resource["name"] = resource_meta.get("name")
        resource["description"] = resource_meta.get("description")

        resource["downloadURL"] = (resource_meta.get("download_url") or resource_meta.get("url") or resource_meta.get("original_url"))
        if not utils.is_url(resource["downloadURL"]):
            resource["downloadURL"] = f"https://{resource['downloadURL']}"

        meta_mimetype = resource_meta.get("mimetype") or resource_meta.get("format") or ""
        meta_ext = utils.get_extension_mime(meta_mimetype) if "/" in meta_mimetype else meta_mimetype.lower()

        meta_mimetype_op, meta_ext_op = utils.get_mime_and_ext(meta_mimetype)
        if meta_mimetype_op and meta_ext_op:
            meta_mimetype = meta_mimetype_op
            meta_ext = meta_ext_op

        try:
            response, self.user_agent = utils.make_request(resource["downloadURL"], self.user_agent)
            media_type, ext = utils.get_resource_ext_info(response)
            media_type, ext = utils.get_mime_and_ext(media_type)
        except Exception:
            resource["mediaType"] = meta_mimetype
            resource["fileName"] = f"{base_name}.{meta_ext}"

            return resource, resource_crawler_info

        if meta_ext and ext != meta_ext:
            if media_type and media_type not in ["application/octet-stream", "text/plain"]:
                resource["mediaType"] = media_type
            else:
                if meta_mimetype:
                    resource["mediaType"] = meta_mimetype
                    ext = meta_ext
                elif media_type:
                    resource["mediaType"] = media_type
                else:
                    resource["mediaType"] = None

            logger("WARNING", f"Detected a media type mismatch for file {resource['downloadURL']} '{base_name}', was declared as '{meta_mimetype}' (.{meta_ext}), but detected as '{media_type}' (.{ext})", indent=3)
            resource_crawler_info["fileMetadataChanges"].update(
                utils.add_tag_explanations("mimetype_mismatch", {
                    "<mediaType_old>": meta_mimetype, "<fileName_old>": f"{base_name}.{meta_ext}",
                    "<mediaType_new>": media_type, "<fileName_new>": f"{base_name}.{ext}"
                })
            )
        else:
            if media_type:
                resource["mediaType"] = media_type
            else:
                resource["mediaType"] = meta_mimetype
                ext = meta_ext

        resource["fileName"] = f"{base_name}.{ext}"

        return resource, resource_crawler_info

    def get_package(self, dataset_id, metadata_file_name):
        url = f"{self.domain}/api/3/action/package_show?id={dataset_id}"
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers)
            if not response:
                logger("ERROR", f"No working User-Agent for URL '{url}'")
                return None

            response.raise_for_status()

        except requests.exceptions.RequestException as e:
                status_code = getattr(e.response, "status_code", None)
                tag = None
                if status_code:
                    tag = utils.get_https_error_tag(status_code)

                logger("ERROR", f"Error downloading '{metadata_file_name}' ({url})", e, indent=2)
                if tag:
                    metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(tag))
                    metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

                return metadata

        data = response.json()["result"]

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

        if distributions:
            download_url = distributions[0].get("url") or distributions[0].get("original_url")
            if download_url:
                metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language", [])

        metadata["keyword"] = data.get("keywords", {}) or data.get("original_tags", []) or data.get("tags", [])

        #theme = data.get("theme")

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("metadata_modified", "")
        metadata["issued"] = data.get("metadata_created", "")
        metadata["license"] = data.get("license_url", "") or data.get("license_id", "")
        if not metadata["license"] and distributions:
            metadata["license"] = distributions[0].get("license", "")

        metadata["source"] = self.domain

        temporal = data.get("temporal", {}) or data.get("temporals", {})
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

        metadata["resources"] = {}
        for idx, resource in enumerate(distributions):
            resource_meta, resource_crawler_info = self.parse_resource(resource, utils.generate_short_filename(f"{metadata['fileName']}_{idx}"))

            metadata["resources"][resource_meta["fileName"]] = resource_meta
            metadata["crawlerInfo"]["resourcesInfo"][resource_meta["fileName"]] = resource_crawler_info

        return metadata
