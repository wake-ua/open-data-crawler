import requests
from urllib.parse import urlparse
from datetime import datetime
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class DatosGobEsCrawler():
    def __init__(self, domain, user_agent, max_sec):
        self.domain = domain.rstrip("/")
        self.user_agent = user_agent
        self.max_sec = max_sec

    def get_package_list(self):
        ids = []
        url = "http://datos.gob.es/virtuoso/sparql"

        params = {
            "query": "SELECT DISTINCT ?dataset WHERE {?dataset a <http://www.w3.org/ns/dcat#Dataset>}"
        }

        headers = {
            "Accept": "application/sparql-results+json",
            "User-Agent": self.user_agent,
            "Connection": "keep-alive"
        }

        try:
            response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers, params=params)
            if not response:
                logger("ERROR", f"Error fetching package list from '{self.domain}': no working User-Agent found")
                return ids

            response.raise_for_status()
            for result in response.json().get("results", {}).get("bindings", []):
                ids.append(result.get("dataset", {}).get("value").split("/")[-1])

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}'", e)

        return ids

    def parse_resource(self, resource_meta, base_name, reparse=None):
        resource = {}
        resource_crawler_info = utils.init_metadata(package=False)

        resource["name"] = utils.extract_multilang_field(resource_meta.get("title", []), "_lang", "_value")

        resource["downloadURL"] = utils.fix_url(resource_meta.get("accessURL") or resource_meta.get("downloadURL"))

        meta_media_type = resource_meta.get("format", {}).get("value")
        response, self.user_agent = utils.make_request(resource["downloadURL"], self.user_agent)
        media_type, file_name, tag_val = utils.resolve_mediatype_conflict(meta_media_type, response, base_name)

        resource["mediaType"] = media_type
        resource["fileName"] = file_name
        if tag_val:
            logger("WARNING", f"Detected a media type mismatch for file {resource['downloadURL']} '{base_name}'", indent=3)
            resource_crawler_info["fileMetadataChanges"].update(utils.add_tag_explanations("mimetype_mismatch", tag_val))

        return resource, resource_crawler_info

    def get_package(self, dataset_id, metadata_file_name):
        url = utils.fix_url(f"{self.domain}/apidata/catalog/dataset/{dataset_id}")
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive"
        }

        metadata = utils.init_metadata()
        response, self.user_agent, error_tag, e = utils.make_request(url, self.user_agent, headers=headers, return_tag=True)
        if not response:
            logger("ERROR", f"Error downloading '{metadata_file_name}'", e, indent=2)

            if error_tag:
                metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(error_tag))
                metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()

            return metadata

        items = response.json()["result"].get("items", [])
        if not items:
            logger("WARNING", f"No data returned for package '{dataset_id}'")
            return None

        data = items[0]

        metadata["identifier"] = dataset_id
        metadata["accessURL"] = utils.fix_url(f"https://datos.gob.es/es/catalogo/{dataset_id}")
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name

        metadata["img"] = "https://datos.gob.es/sites/default/files/favicon.png"

        metadata["title"] = utils.extract_multilang_field(data.get("title", []), "_lang", "_value")
        metadata["description"] = utils.extract_multilang_field(data.get("description", []), "_lang", "_value")

        distributions = data.get("distribution", [])
        if not isinstance(distributions, list):
            distributions = [distributions]

        metadata["publisher"] = utils.extract_mapped_field(data.get("publisher"), utils.DATOSGOBESCRAWLER_PUBLISHER_MAP)[0]

        download_url = distributions[0].get("accessURL") or distributions[0].get("downloadURL")
        if download_url:
            metadata["publisher"]["homepage"] = f"https://{urlparse(download_url).netloc}"

        metadata["language"] = data.get("language")

        metadata["keyword"] = utils.extract_multilang_field(data.get("keyword", []), "_lang", "_value")

        metadata["theme"] = utils.extract_mapped_field(data.get("theme"), utils.DATOSGOBESCRAWLER_THEME_MAP)

        metadata["accrualPeriodicity"] = {k: v for k, v in data.get("accrualPeriodicity", {}).get("value", {}).items() if k != "_about"}
        metadata["modified"] = data.get("modified")
        metadata["issued"] = data.get("issued")
        metadata["license"] = data.get("license")
        metadata["source"] = self.domain

        temporal = data.get("temporal", {})
        metadata["temporal"] = temporal

        if isinstance(temporal, dict):
            metadata["temporal"] = {
                "startDate": temporal.get("startDate") or temporal.get("start_date"),
                "endDate": temporal.get("endDate") or temporal.get("end_date"),
            }

        metadata["geo"] = utils.extract_mapped_field(data.get("spatial"), utils.DATOSGOBESCRAWLER_SPATIAL_MAP)

        if distributions:
            metadata["resources"] = {}
            metadata["crawlerInfo"]["resourcesInfo"] = {}
            for idx, resource in enumerate(distributions):
                base_name = utils.generate_short_filename(f"{metadata['fileName']}_{idx}")
                resource["fileName"] = base_name
                metadata["resources"][base_name] = resource
                metadata["crawlerInfo"]["resourcesInfo"][base_name] = utils.init_metadata(package=False, crawled=False)

        return metadata
