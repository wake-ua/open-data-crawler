import requests
from urllib.parse import urlparse
from opendatacrawler import utils
import json
from datetime import datetime
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class GbifCrawler():
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

    def get_package_list(self):
        url = f"{self.odcrawler.domain}/v1/dataset/search?limit=1&offset=0"
        response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, max_sec=self.odcrawler.max_sec)
        if not response:
            logger("ERROR", f"Error fetching package list from '{self.odcrawler.domain}'")
            return []
        
        limit = 100
        max_pages = response.json().get("count") // limit + 1
        offsets = [i * limit for i in range(max_pages)]

        def fetch_page(offset):
            url = f"{self.odcrawler.domain}/v1/dataset/search?limit={limit}&offset={offset}"
            
            response, self.odcrawler.user_agent = self.odcrawler.make_request(url, self.odcrawler.user_agent, max_sec=self.odcrawler.max_sec)
            if not response:
                return []

            data = response.json()
            ids = [str(pkg["key"]) for pkg in data.get("results", []) if pkg.get("key")]

            return ids

        ids_list = self.odcrawler.run_threaded_function(
            items=offsets, func=fetch_page, max_workers=self.odcrawler.max_threads,
            use_tqdm=True, tqdm_desc="Fetching packages IDs...", tqdm_colour="blue"
        )

        ids = [pkg_id for sublist in ids_list for pkg_id in sublist]

        logger("OK", f"Retrieved {len(ids)} packages from '{self.odcrawler.domain}'", level="print")
        return ids

    def parse_resource(self, resource_meta, base_name, metadata_file_name, reparse_data=None):
        resource_crawler_info = utils.init_metadata(package=False)

        if reparse_data:
            logger("...", f"Re-parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=4)
            resource = resource_meta
            meta_media_type = reparse_data.get("metaMediaType")
        else:
            logger("...", f"Parsing resource '{base_name}' from package '{metadata_file_name}'...", indent=4)
            resource = {}
            resource["fileName"] = base_name

            resource["name"] = resource_meta.get("name") or resource_meta.get("key")
            resource["description"] = resource_meta.get("description")

            resource["downloadURL"] = utils.fix_url(resource_meta.get("url"))

            meta_media_type = resource_meta.get("format", "")

        response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_request(resource["downloadURL"], self.odcrawler.user_agent, return_tag=True, max_sec=self.odcrawler.max_sec)
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
        url = utils.fix_url(f"{self.odcrawler.domain}/v1/dataset/{package_id}")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://images.ctfassets.net/uo17ejk9rkwj/4rmEF9F4ZGiCwQk2WSMcce/b47146eacaf7b0dfc166656678c7ffe6/GBIF-2015.png"

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

        data = response.json()

        metadata["accessURL"] = utils.fix_url(f"{self.odcrawler.domain}/dataset/{package_id}")

        metadata["title"] = data.get("title", {})
        metadata["description"] = data.get("description", {})

        language = data.get("language", [])
        language = [language] if isinstance(language, str) else language
        data_language = data.get("dataLanguage", [])
        data_language = [data_language] if isinstance(data_language, str) else data_language
        metadata["language"] = list(set(language + data_language))

        metadata["doi"] = data.get("doi", "")

        publisher = next((contact for contact in data.get("contacts", []) if contact.get("type") == "ORIGINATOR"),{})
        metadata["publisher"] = {
            "identifier": publisher.get("key", ""),
            "title": utils.extract_first_nonempty_value(publisher.get("organization", "")),
            "homepage": data.get("homepage", ""),
        }

        distributions = []
        endpoints_res = data.get("endpoints", [])
        for endpoint in endpoints_res:
            if endpoint.get("type") not in {"EML"}:
                distributions.append(endpoint)

        data_descriptions_res = data.get("dataDescriptions", [])
        for dd_res in data_descriptions_res:
            if dd_res.get("url"):
                distributions.append(dd_res)

        metadata["theme"] = data.get("tags", [])

        metadata["keyword"] = []
        keywords = data.get("keywords", [])
        if isinstance(keywords, list):
            metadata["keyword"].extend(keywords)
        for collection in data.get("keywordCollections", []):
            keywords = collection.get("keywords", [])
            if isinstance(keywords, list):
                metadata["keyword"].extend(keywords)
        metadata["keyword"] = list(dict.fromkeys(metadata["keyword"]))

        metadata["accrualPeriodicity"] = data.get("accrualPeriodicity")

        metadata["modified"] = data.get("modified", "")
        metadata["issued"] = data.get("metadata_created", "")
        metadata["license"] = data.get("license", "")

        metadata["source"] = self.odcrawler.domain

        temporals = data.get("temporalCoverages", {})
        for temporal in temporals:
            if isinstance(temporal, dict):
                metadata["temporal"] = {
                    "startDate": temporal.get("start"),
                    "endDate": temporal.get("end")
                }

        spatials = data.get("geographicCoverages", [])
        metadata["geo"] = []
        metadata["spatial"] = []
        for spatial in spatials:
            if isinstance(spatial, dict):
                description = spatial.get("description")
                bbox = spatial.get("boundingBox")
                if description:
                    metadata["geo"].append(description)
                if bbox and not bbox.get("globalCoverage"):
                    metadata["spatial"].append(bbox)

        if distributions:
            logger("WORK", f"Processing {len(distributions)} resources from package '{package_id}' ('{metadata_file_name}')...", indent=2)
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        metadata["distributions"] = distributions
        metadata["rawData"] = data

        return metadata
