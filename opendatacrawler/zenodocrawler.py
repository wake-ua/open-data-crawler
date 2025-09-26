import requests
from tqdm import tqdm
import traceback
from opendatacrawler import utils
from opendatacrawler.setup_logger import log_manager
logger = log_manager.log

class ZenodoCrawler():
    def __init__(self, domain, data_types, user_agent, max_sec):
        self.domain = domain.rstrip("/")
        self.data_types = data_types
        self.user_agent = user_agent
        self.max_sec = max_sec

        self.token = utils.AUTH_TOKENS.get("zenodo", None)

    def get_package_list(self):
        ids = []
        base_url = f"{self.domain}/api/records"

        page_size = 100

        all_ids = set()
        last_max_date = None

        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                all_ids = set(state.get("ids", []))
                last_date = date.fromisoformat(state.get("lastDate"))
                last_hour = state.get("lastHour", 0)
                logger("...", f"Reanudando desde {last_date} {last_hour:02d}h | IDs cargados: {len(all_ids)}", level="print")
            except Exception as e:
                logger("ERROR", f"Error cargando estado desde '{STATE_FILE}'", [e])
                return set()







    def get_package_list_oai2d(self):
        ids = []
        url = f"{self.domain}/oai2d"

        params = {
            "verb": "ListIdentifiers",
            "metadataPrefix": "oai_dc"
        }

        pbar = None
        try:
            while True:
                response, self.user_agent = utils.make_request(url, self.user_agent, params=params)
                if not response:
                    logger("ERROR", f"Error fetching package list from '{self.domain}': no working User-Agent found")
                    return ids

                response.raise_for_status()
                data = ET.fromstring(response.text)

                for header in data.findall(".//oai:header", self.ns):
                    identifier = header.find("oai:identifier", self.ns).text
                    if identifier and identifier.startswith("oai:zenodo.org:"):
                        id_num = int(identifier.split(":")[-1])
                        ids.append(id_num)

                resumption_token = data.find(".//oai:resumptionToken", self.ns)
                if resumption_token is not None and resumption_token.text:
                    total = resumption_token.attrib.get("completeListSize")
                    cursor = resumption_token.attrib.get("cursor")

                    if total and cursor:
                        if pbar is None:
                            pbar = tqdm(total=int(total), desc="Obtaining...", unit="records")
                            pbar.update(int(cursor))
                        else:
                            delta = int(cursor) - pbar.n
                            if delta > 0:
                                pbar.update(delta)

                    params = {
                        "verb": "ListIdentifiers",
                        "resumptionToken": resumption_token.text.strip()
                    }
                else:
                    if pbar:
                        pbar.update(pbar.total - pbar.n)
                        pbar.close()
                    break

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}'", e)
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}'", e)

        return ids

    def get_package_list(self):
        ids = []
        headers = {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Authorization": f"Bearer m69fGQgHiAGSRxRJwUtzXIHcEJy1ZhB71hY5MAYJNQe82aC9NU7A0xrgfJrV"
        }

        page_size = 200
        url = f"{self.domain}/api/records?page=1&size={page_size}&type=dataset"
        try:
            pbar = None

            while url:
                response, self.user_agent = utils.make_request(url, self.user_agent, headers=headers, max_sec=360)
                if not response:
                    logger("ERROR", f"Error fetching package list from '{self.domain}': no working User-Agent found")
                    break

                response.raise_for_status()
                data = response.json()

                if pbar is None:
                    total = data.get("hits", {}).get("total", 0)
                    pbar = tqdm(total=total, desc="Obtaining...", unit="package")

                hits = data.get("hits", {}).get("hits", [])
                for record in hits:
                    ids.append(record.get("id"))
                pbar.update(len(hits))

                url = data.get("links", {}).get("next")

            if pbar:
                pbar.close()

            logger("OK", f"Retrieved {len(ids)} packages from '{self.domain}'", level="print")

        except requests.RequestException as e:
            logger("ERROR", f"Error fetching package list from '{self.domain}': {e}")
        except Exception as e:
            logger("ERROR", f"Unexpected error parsing response from '{self.domain}': {e}")

        return ids

    def get_requests_ids(file_type, token):
        skip = 1
        ids = []
        fin = False

        while not fin:
            response = requests.get('https://zenodo.org/api/records/?type=dataset&file_type=' + file_type + '&size=200&page='+str(skip)+'&access_token='+str(token))
            if response.status_code == 200:
                packages = response.json()['hits']['hits']
                if len(packages) > 0:
                    skip += 1
                    for p in packages:
                        ids.append(p['id'])
                else:
                    fin = True
            else:
                fin = True
        return ids

    def get_package_list_falso(self):
        total_ids = []
        
        ids_csv = get_requests_ids('csv', self.token)
        ids_zip = get_requests_ids('zip', self.token)
        ids_xlsx = get_requests_ids('xlsx', self.token)
        
        # Add ids        
        cont_csv = 0
        for x in ids_csv:
            total_ids.append(x)
            cont_csv = cont_csv + 1
            
        cont_zip = 0    
        for y in ids_zip:
            total_ids.append(y)
            cont_zip = cont_zip + 1
            
        cont_xlsx = 0    
        for z in ids_xlsx:
            total_ids.append(z)
            cont_xlsx = cont_xlsx + 1
            
        return total_ids

    def get_package(self, id):
        """Build a dict of package metadata"""
        try:
            response = requests.get('https://zenodo.org/api/records/' + str(id))
            
            if response.status_code == 200:
                # Timer start counting
                utils.timer_start()
                
                meta_json = response.json()

                metadata = dict()
                
                metadata['identifier'] = id
                
                meta = meta_json.get('metadata', None)
                
                if meta is not None:
                    metadata['title'] = meta.get('title', None)
                    metadata['description'] = meta.get('description', None)
                    if meta.get('keywords', None) is not None:
                        metadata['theme'] = utils.extract_keywords(meta.get('keywords', None)[0])
                
                resource_list = []

                aux = dict()

                if meta_json.get('files', None) is not None:
                    url = meta_json.get('files', None)[0]
                    aux['downloadUrl'] = url.get('links', None).get('self', None)
                    aux['mediaType'] = url.get('type', None)

                resource_list.append(aux)

                metadata['resources'] = resource_list
                metadata['modified'] = meta.get('publication_date', None)
                #metadata['license'] = requests.get('https://zenodo.org/api/licenses/')
                metadata['license'] = None
                metadata['source'] = self.domain
                
                return metadata
            else:
                # Timer stops when it can't make any more calls to the API
                rest = utils.timer_stop()
                if rest < 60:
                    time.sleep(60 - rest)
                    return (self.get_package(id))
        except Exception as e:
            print(traceback.format_exc())
            logger.info(e)
            return None