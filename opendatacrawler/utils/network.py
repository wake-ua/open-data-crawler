import re
import socket
import time
import xml.etree.ElementTree as ET
from io import BytesIO

import requests
from url_normalize import url_normalize
from w3lib.url import url_query_cleaner

from .resources import CRAWLER_CHANGES_INFO, USER_AGENTS


def is_url(text):
    return isinstance(text, str) and (text.startswith("http://") or text.startswith("https://"))


def fix_url(url):
    if not url:
        return None
    url = url.strip().replace(" ", "%20")
    if not is_url(url):
        url = f"https://{url}"
    return url


def normalize_domain(domain):
    if "gbif.org" in domain and not domain.startswith("http://api.gbif.org"):
        return "http://api.gbif.org"

    lang_suffix = re.compile(r"/[a-z]{2}$", re.IGNORECASE)
    domain = lang_suffix.sub("", domain)

    return domain


def get_user_agent_list(current_agent):
    return [current_agent] + [ua for ua in USER_AGENTS if ua != current_agent] if current_agent else USER_AGENTS


def make_request(url, current_agent, headers=None, params=None, max_sec=None, stream=False, sleep_time=3, return_tag=False, rate_controller=None):
    headers = headers.copy() if headers else {}

    if rate_controller is not None and hasattr(rate_controller, "check_host_cooldown"):
        cooldown_info = rate_controller.check_host_cooldown(url)
        if cooldown_info:
            host, wait_seconds = cooldown_info
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{host}' in cooldown for ~{wait_seconds}s]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    all_forbidden = True
    for user_agent in get_user_agent_list(current_agent):
        headers["User-Agent"] = user_agent
        while True:
            try:
                if rate_controller is not None:
                    rate_controller.check_rate_limit()

                response = requests.get(url, headers=headers, params=params, verify=False, timeout=max_sec, stream=stream)

                if response.status_code == 403:
                    break
                elif response.status_code == 429:
                    time.sleep(sleep_time)
                    continue

                all_forbidden = False
                if return_tag:
                    try:
                        response.raise_for_status()
                    except requests.exceptions.RequestException as e:
                        tag = get_error_tag_from_exception(e)
                        if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                            rate_controller.register_host_result(url, tag, e)
                        tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                        if tag == "resource_temporarily_unavailable":
                            return None, user_agent, tag, f"{tag_explanation} ({url}) - [{e}]"
                        return None, user_agent, tag, f"{tag_explanation} ({url})"
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, None)
                    return response, user_agent, None, None

                if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                    rate_controller.register_host_result(url, None)
                return response, user_agent

            except requests.exceptions.RequestException as e:
                tag = get_error_tag_from_exception(e)
                if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                    rate_controller.register_host_result(url, tag, e)
                tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                if tag == "resource_temporarily_unavailable":
                    return (None, user_agent, tag, f"{tag_explanation} ({url}) - [{e}]") if return_tag else (None, user_agent)
                return (None, user_agent, tag, f"{tag_explanation} ({url})") if return_tag else (None, user_agent)

    if return_tag:
        if all_forbidden:
            return None, current_agent, "forbidden_resource", f"{CRAWLER_CHANGES_INFO.get('forbidden_resource', {}).get('tag_explanation', {}).get('reason', 'Unknown reason')} ({url})"
        return None, current_agent, None, None
    return None, current_agent


def make_request_post(url, current_agent, headers=None, json_body=None, max_sec=None, stream=False, sleep_time=3, return_tag=False, rate_controller=None):
    headers = headers.copy() if headers else {}

    if rate_controller is not None and hasattr(rate_controller, "check_host_cooldown"):
        cooldown_info = rate_controller.check_host_cooldown(url)
        if cooldown_info:
            host, wait_seconds = cooldown_info
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{host}' in cooldown for ~{wait_seconds}s]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    all_forbidden = True
    for user_agent in get_user_agent_list(current_agent):
        headers["User-Agent"] = user_agent
        while True:
            try:
                if rate_controller is not None:
                    rate_controller.check_rate_limit()

                response = requests.post(
                    url,
                    headers=headers,
                    json=(json_body or {}),
                    verify=False,
                    timeout=max_sec,
                    stream=stream,
                )

                if response.status_code == 403:
                    break
                elif response.status_code == 429:
                    time.sleep(sleep_time)
                    continue

                all_forbidden = False
                if return_tag:
                    try:
                        response.raise_for_status()
                    except requests.exceptions.RequestException as e:
                        tag = get_error_tag_from_exception(e)
                        if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                            rate_controller.register_host_result(url, tag, e)
                        tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                        if tag == "resource_temporarily_unavailable":
                            return None, user_agent, tag, f"{tag_explanation} ({url}) - [{e}]"
                        return None, user_agent, tag, f"{tag_explanation} ({url})"
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, None)
                    return response, user_agent, None, None

                if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                    rate_controller.register_host_result(url, None)
                return response, user_agent

            except requests.exceptions.RequestException as e:
                tag = get_error_tag_from_exception(e)
                if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                    rate_controller.register_host_result(url, tag, e)
                tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
                if tag == "resource_temporarily_unavailable":
                    return (None, user_agent, tag, f"{tag_explanation} ({url}) - [{e}]") if return_tag else (None, user_agent)
                return (None, user_agent, tag, f"{tag_explanation} ({url})") if return_tag else (None, user_agent)

    if return_tag:
        if all_forbidden:
            return None, current_agent, "forbidden_resource", f"{CRAWLER_CHANGES_INFO.get('forbidden_resource', {}).get('tag_explanation', {}).get('reason', 'Unknown reason')} ({url})"
        return None, current_agent, None, None
    return None, current_agent


def clean_url(u):
    u = url_normalize(u)
    u = url_query_cleaner(u, remove=True, parameterlist=["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"])

    for prefix in ("http://", "https://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]

    return u.split("/")[0]


def get_error_tag_from_exception(e):
    if isinstance(e, tuple) and len(e) == 2 and isinstance(e[1], BaseException):
        e = e[1]

    if isinstance(e, socket.gaierror):
        return "unresolvable_domain"

    if isinstance(e, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ChunkedEncodingError,
        ConnectionResetError,
    )):
        return "resource_temporarily_unavailable"

    if isinstance(e, requests.exceptions.SSLError):
        return "ssl_error"

    status_code = getattr(getattr(e, "response", None), "status_code", None)
    tag = get_https_error_tag(status_code)
    if tag:
        return tag

    raise ValueError(f"Unhandled exception type: {e}")


def get_host_backoff_hint(e):
    status_code = getattr(getattr(e, "response", None), "status_code", None)
    if status_code == 503:
        return {"penalty": 2, "cooldown_scale": 2, "reason": "503"}
    if status_code in [502, 504, 522, 429]:
        return {"penalty": 2, "cooldown_scale": 1.5, "reason": str(status_code)}
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return {"penalty": 2, "cooldown_scale": 1.5, "reason": "timeout"}
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, ConnectionResetError)):
        return {"penalty": 1, "cooldown_scale": 1, "reason": "connection"}
    return {"penalty": 1, "cooldown_scale": 1, "reason": "generic"}


def get_https_error_tag(status_code):
    if status_code:
        if status_code == 400:
            return "invalid_request"
        elif status_code == 401:
            return "unauthorized_access"
        elif status_code == 404:
            return "missing_resource"
        elif status_code == 405:
            return "method_not_allowed"
        elif status_code == 410:
            return "resource_removed"
        elif status_code in [408, 429, 499, 500, 502, 503, 504, 522]:
            return "resource_temporarily_unavailable"
    return None


def extract_namespaces(xml_data):
    ns = {}
    for _, elem in ET.iterparse(BytesIO(xml_data), events=("start-ns",)):
        prefix, uri = elem
        ns[prefix or "default"] = uri
    return ns


def get_xml_text(element):
    if element is not None and element.text:
        return element.text.strip()
    return None


def get_xml_attr(element, ns, attr_name):
    if element is None:
        return None
    return element.attrib.get(f"{{{ns}}}{attr_name}")
