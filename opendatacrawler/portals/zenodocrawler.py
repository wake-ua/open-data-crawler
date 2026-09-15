import requests
import os
import json
import re
import sqlite3
from contextlib import closing
import hashlib
from tqdm import tqdm
from datetime import datetime, date, timedelta, timezone
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class ZenodoCrawler():
    SUPPORTS_CATEGORY_FILTER = False
    def __init__(self, odcrawler):
        self.odcrawler = odcrawler

        reqs_per_sec = utils.get_config_option("zenodo", "reqs_per_sec", cast=float, fallback=5000/3600)
        if self.odcrawler.config_reqs_per_sec is None:
            self.odcrawler.config_reqs_per_sec = reqs_per_sec
            self.odcrawler.init_rate_limit(reqs_per_sec=reqs_per_sec)

        self.token = utils.AUTH_TOKENS.get("zenodo")

    def _search(self, params):
        response, self.odcrawler.user_agent = self.odcrawler.make_request(
            f"{self.odcrawler.domain}/api/records", self.odcrawler.user_agent,
            params={"type": "dataset", **params})
        payload = utils.read_json(response, "enumerating Zenodo")
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
            raise utils.CatalogError("Zenodo response lacks hits.hits")
        total = hits.get("total")
        if isinstance(total, dict):
            if total.get("relation") == "gte":
                total = max(10001, total.get("value", 0))
            else:
                total = total.get("value")
        if not isinstance(total, int):
            raise utils.CatalogError("Zenodo response lacks total count")
        return hits["hits"], total

    def get_package_list(self):
        cache = os.path.join(self.odcrawler.base_domain_path, ".cache")
        os.makedirs(cache, exist_ok=True)
        scope = hashlib.sha256(f"{self.odcrawler.domain}:type=dataset:{self.token or ''}".encode()).hexdigest()[:20]
        path = os.path.join(cache, f"zenodo_{scope}.sqlite")
        now = datetime.now(timezone.utc)
        with utils.file_lock(path + ".lock"), closing(sqlite3.connect(path)) as database:
            database.execute("CREATE TABLE IF NOT EXISTS ids (id TEXT PRIMARY KEY)")
            database.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            row = database.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
            if row:
                start = datetime.fromisoformat(row[0]) - timedelta(days=1)
            else:
                hits, total = self._search({"size": 1, "sort": "oldest"})
                if not hits:
                    if total:
                        raise utils.CatalogError("Zenodo returned no first record despite a nonzero total")
                    return []
                start = datetime.fromisoformat(hits[0]["created"].replace("Z", "+00:00"))
            def harvest(lower, upper):
                lo = lower.isoformat().replace("+00:00", "Z")
                hi = upper.isoformat().replace("+00:00", "Z")
                query = f"created:[{lo} TO {hi}}}"
                params = {"q": query, "sort": "oldest", "size": 100, "page": 1}
                hits, total = self._search(params)
                if total > 10000:
                    if upper - lower <= timedelta(microseconds=1):
                        raise utils.CatalogError("Zenodo window exceeds API pagination limit; cannot enumerate safely")
                    middle = lower + (upper - lower) / 2
                    harvest(lower, middle); harvest(middle, upper)
                    return
                window_ids = set()
                while True:
                    for record in hits:
                        key = str(record.get("id") or "")
                        if not key or key in window_ids:
                            raise utils.CatalogError("Zenodo repeated/missing record ID in paginated window")
                        window_ids.add(key)
                    if len(window_ids) >= total:
                        break
                    if not hits:
                        raise utils.CatalogError("Zenodo returned an incomplete page sequence")
                    params["page"] += 1
                    hits, next_total = self._search(params)
                    if next_total != total:
                        raise utils.CatalogError("Zenodo window changed during pagination; retry")
                if len(window_ids) != total:
                    raise utils.CatalogError("Zenodo count mismatch")
                with database:
                    database.executemany("INSERT OR IGNORE INTO ids VALUES (?)", ((key,) for key in window_ids))
                    database.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (upper.isoformat(),))
            while start < now:
                end = min(start + timedelta(days=366), now)
                harvest(start, end)
                start = end
            return [row[0] for row in database.execute("SELECT id FROM ids ORDER BY id")]

    def parse_resource(self, resource_meta, base_name):
        resource = {}
        resource["fileName"] = base_name

        resource["id"] = resource_meta.get("id")
        resource["checksum"] = resource_meta.get("checksum")
        resource["remoteSize"] = resource_meta.get("size")
        resource["name"] = resource_meta.get("key", None)

        resource["downloadURL"] = utils.fix_url(resource_meta.get("links", {}).get("self", None))

        meta_media_type = resource_meta.get("key", None)
        if meta_media_type:
            ext = meta_media_type.split(".")[-1].lower() if "." in meta_media_type else None
            meta_media_type = utils.EXT_TO_MIME.get(ext)

        return resource, meta_media_type

    def get_package(self, package_id, metadata_file_name):
        url = utils.fix_url(f"{self.odcrawler.domain}/api/records/{package_id}")

        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {})
        }

        metadata = utils.init_metadata()
        metadata["identifier"] = package_id
        metadata["requestURL"] = url

        metadata["fileName"] = metadata_file_name
        metadata["img"] = "https://zenodo.org/static/images/invenio-rdm.svg"

        response, self.odcrawler.user_agent, error_tag, e = self.odcrawler.make_request(url, self.odcrawler.user_agent, headers=headers, return_tag=True)
        if not response:
            error_message = utils.extract_error_message(e)
            tag_values = utils.extract_error_tag_values(e)
            if error_tag:
                if error_tag != "resource_temporarily_unavailable":
                    logger("WARNING", f"Non-retryable error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
                    metadata["crawlerInfo"]["packageInfo"].update(utils.add_tag_explanations(error_tag, tag_values))
                    metadata["crawlerInfo"]["packageStatus"]["packageCompleted"] = datetime.now().isoformat()
                    return metadata
                else:
                    logger("WARNING", f"Retryable error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
            else:
                logger("ERROR", f"Error accessing package '{package_id}' ('{metadata_file_name}')", error_message, indent=2)
            return None

        data = utils.read_json(response, f"reading Zenodo record {package_id}")
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            raise utils.CatalogError("Invalid Zenodo record envelope")

        metadata["title"] = data.get("metadata", {}).get("title", {})
        metadata["description"] = data.get("metadata", {}).get("description", {})
        metadata["doi"] = data.get("metadata", {}).get("doi", {})

        distributions = data.get("files", {})
        if not isinstance(distributions, list):
            distributions = [distributions]

        metadata["creator"] = data.get("metadata", {}).get("creators", [])
        metadata["publisher"] = data.get("metadata", {}).get("publisher")
        metadata["accessURL"] = data.get("links", {}).get("html") or f"{self.odcrawler.domain}/records/{package_id}"
        metadata["issued"] = data.get("metadata", {}).get("publication_date") or data.get("created")

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

        metadata["modified"] = data.get("modified", None)

        metadata["license"] = data.get("metadata", {}).get("license", {}).get("id", None)

        metadata["source"] = self.odcrawler.domain

        if distributions:
            self.odcrawler.init_and_parse_resources(metadata, distributions)

        if self.odcrawler.save_raw_data:
            metadata["rawData"] = data

        return metadata
