import requests
import os
import json
import re
from tqdm import tqdm
from datetime import datetime, date, timedelta
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class ZenodoCrawler():
    def __init__(self, domain, user_agent, max_sec):
        self.domain = domain.rstrip("/")
        self.user_agent = user_agent
        self.max_sec = max_sec

        self.token = utils.AUTH_TOKENS.get("zenodo")
        self.ns = {
            "oai": "http://www.openarchives.org/OAI/2.0/",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "dct": "http://purl.org/dc/terms/",
            "dcat": "http://www.w3.org/ns/dcat#",
            "foaf": "http://xmlns.com/foaf/0.1/",
        }

    def get_state_file(self, url, path=utils.ZENODO_STATE_FILE):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            ids = set(state.get("ids", []))
            last_date = date.fromisoformat(state.get("lastDate"))
            last_hour = state.get("lastHour", 0)
        else:
            response, self.user_agent = utils.make_request(url, self.user_agent, params={"size": 1, "sort": "oldest"}, max_sec=self.max_sec)
            ids = set()
            last_date = datetime.fromisoformat(response.json()["hits"]["hits"][0]["created"].replace("Z", "+00:00")).date()
            last_hour = 0

        return ids, last_date, last_hour

    def get_package_list(self):
        url = f"{self.domain}/api/records?type=dataset"
        ids, last_date, last_hour = self.get_state_file(url)

        page_size = 25
        last_max_date = None
        try:
            while True:
                response, self.user_agent = utils.make_request(url, self.user_agent, params={"size": 1, "sort": "newest"}, max_sec=self.max_sec)
                max_date = datetime.fromisoformat(response.json()["hits"]["hits"][0]["created"].replace("Z", "+00:00")).date()

                if last_max_date == max_date:
                    break

                last_max_date = max_date
                total_hours = ((max_date - last_date).days * 24) + (24 - last_hour)
                with tqdm(total=total_hours, desc="Fetching packages...", colour="blue") as pbar:
                    while last_date <= max_date:
                        last_successful_hour = None
                        for hour in range(last_hour, 24):
                            hour_start = f"{last_date}T{hour:02d}:00:00Z"
                            hour_end = f"{last_date}T{hour:02d}:59:59Z"

                            page = 1
                            
                            while True:
                                params = {
                                    "page": page,
                                    "size": page_size,
                                    "sort": "oldest",
                                    "q": f"created:[{hour_start} TO {hour_end}]"
                                }

                                response, self.user_agent, tag, e = utils.make_request(url, self.user_agent, params=params, max_sec=self.max_sec, return_tag=True)
                                if not response:
                                    if tag == "resource_temporarily_unavailable":
                                        page_size = max(page_size // 2, 10)
                                    continue

                                hits = response.json().get("hits", {}).get("hits", [])
                                if not hits:
                                    break
                                
                                last_successful_hour = hour
                                for record in hits:
                                    if record["id"] not in ids:
                                        ids.add(record["id"])

                                if len(hits) < page_size:
                                    break

                                page += 1

                            if last_date == max_date and last_successful_hour is not None:
                                last_hour = last_successful_hour - 1 if last_successful_hour > 0 else 0
                            else:
                                last_hour = 0

                            state = {
                                "lastDate": last_date.isoformat(),
                                "lastHour" : last_hour, 
                                "totalIds": len(ids),
                                "ids": list(ids)
                            }

                            if last_date == max_date and last_successful_hour is not None:
                                state["lastHour"] = last_successful_hour
                            else:
                                state["lastHour"] = hour + 1 if hour < 23 else 0

                            with open(utils.ZENODO_STATE_FILE, "w", encoding="utf-8") as f:
                                json.dump(state, f, indent=2)

                            pbar.update(1)

                        last_date += timedelta(days=1)
                        last_hour = 0

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}'", e)

        return list(ids)

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

            resource["identifier"] = resource_meta.get("id", None)
            resource["name"] = resource_meta.get("key", None)

            resource["downloadURL"] = utils.fix_url(resource_meta.get("links", {}).get("self", None))
            if not resource["downloadURL"]:
                print(resource_meta)
                return None, None

            meta_media_type = resource_meta.get("key", None)
            if meta_media_type:
                ext = meta_media_type.split(".")[-1].lower() if "." in meta_media_type else None
                meta_media_type = utils.EXT_TO_MIME.get(ext)

        response, self.user_agent, error_tag, e = utils.make_request(resource["downloadURL"], self.user_agent, return_tag=True, max_sec=self.max_sec, stream=True)
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
        url = utils.fix_url(f"{self.domain}/api/records/{package_id}")

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Authorization": f"Bearer {self.token}"
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://zenodo.org/static/images/invenio-rdm.svg"

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

        data = response.json()

        metadata["title"] = data.get("metadata", {}).get("title", {})
        metadata["description"] = data.get("metadata", {}).get("description", {})
        metadata["doi"] = data.get("metadata", {}).get("doi", {})

        distributions = data.get("files", {})
        if not isinstance(distributions, list):
            distributions = [distributions]

        metadata["publisher"] = data.get("metadata", {}).get("creators", {})

        metadata["language"] = data.get("metadata", {}).get("language", {})

        keywords = data.get("metadata", {}).get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        keywords_list = []
        if isinstance(keywords, list):
            for item in keywords:
                if isinstance(item, str):
                    for keyword in re.split(r"[;,]", item):
                        kw = keyword.strip()
                        if kw:
                            keywords_list.append(kw)
        metadata["keyword"] = keywords_list

        #metadata["theme"] = []
        #metadata["accrualPeriodicity"] = None

        metadata["modified"] = data.get("modified", None)
        #metadata["issued"] = None

        metadata["license"] = data.get("metadata", {}).get("license", {}).get("id", None)

        metadata["source"] = self.domain

        #metadata["temporal"] = None
        #metadata["spatial"] = None

        if distributions:
            metadata["resources"] = {}
            metadata["crawlerInfo"]["resourcesInfo"] = {}
            for idx, resource in enumerate(distributions):
                base_name = utils.generate_short_filename(f"{metadata['fileName']}_{idx}")
                resource["fileName"] = base_name
                metadata["resources"][base_name] = resource
                metadata["crawlerInfo"]["resourcesInfo"][base_name] = utils.init_metadata(package=False, crawled=False)

        metadata["dataRaw"] = data

        return metadata