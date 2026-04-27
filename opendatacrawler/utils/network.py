import json
import http.client
import ipaddress
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import unquote, urlparse

import requests
from urllib3.exceptions import LocationParseError
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


def _build_error_details(message, tag_values=None):
    details = {"message": message}
    if tag_values:
        details["tag_values"] = tag_values
    return details


def extract_error_message(error):
    if isinstance(error, dict):
        return error.get("message")
    return error


def extract_error_tag_values(error):
    if isinstance(error, dict):
        tag_values = error.get("tag_values")
        if isinstance(tag_values, dict):
            return tag_values
    return None


def _probe_redirect_location(url, headers=None, params=None, json_body=None, max_sec=None, method="GET"):
    try:
        parsed = urlparse(url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or 443,
                timeout=max_sec,
                context=ssl._create_unverified_context(),
            )
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=max_sec)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        request_headers = dict(headers or {})
        if parsed.hostname and "Host" not in request_headers:
            request_headers["Host"] = parsed.netloc

        if method == "POST":
            body = json.dumps(json_body or {})
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(body.encode("utf-8"))))
            conn.request("POST", path, body=body, headers=request_headers)
        else:
            conn.request("GET", path, headers=request_headers)

        response = conn.getresponse()
        try:
            return response.getheader("Location")
        finally:
            response.close()
            conn.close()
    except Exception:
        return None


def build_invalid_redirect_error(url, decode_error, headers=None, params=None, json_body=None, max_sec=None, method="GET"):
    location_header = _probe_redirect_location(
        url,
        headers=headers,
        params=params,
        json_body=json_body,
        max_sec=max_sec,
        method=method,
    ) or "unknown"

    tag = "invalid_redirect_location"
    tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
    tag_values = {
        "<redirect_url>": url,
        "<location_header>": location_header,
        "<decode_error>": str(decode_error),
    }
    message = f"{tag_explanation} ({url}) - [Location: {location_header}] - [{decode_error}]"
    return tag, _build_error_details(message, tag_values)


def validate_http_download_url(url):
    if not isinstance(url, str) or not url.strip():
        return False, "missing download URL"

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False, f"unsupported URL scheme '{parsed.scheme or 'missing'}'"

    if not parsed.netloc or not parsed.hostname:
        return False, "missing URL host"

    hostname = parsed.hostname.strip()
    decoded_host = unquote(hostname)
    if any(token in decoded_host for token in ("=", ";")):
        return False, "host looks like a connection string instead of a valid web host"

    if any(ch.isspace() for ch in decoded_host):
        return False, "host contains whitespace"

    try:
        ipaddress.ip_address(hostname)
        return True, None
    except ValueError:
        pass

    labels = hostname.rstrip(".").split(".")
    if any(not label or len(label) > 63 for label in labels):
        return False, "host contains empty or overlong labels"

    valid_label = re.compile(r"^[A-Za-z0-9-]+$")
    if not all(valid_label.match(label) for label in labels):
        return False, "host contains invalid characters"

    return True, None


def build_invalid_download_url_error(url, reason):
    tag = "invalid_download_url"
    tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
    tag_values = {
        "<download_url>": url,
        "<validation_reason>": str(reason),
    }
    message = f"{tag_explanation} ({url}) - [{reason}]"
    return tag, _build_error_details(message, tag_values)


def make_request(url, current_agent, headers=None, params=None, max_sec=None, stream=False, sleep_time=3, return_tag=False, rate_controller=None):
    headers = headers.copy() if headers else {}

    is_valid_url, invalid_reason = validate_http_download_url(url)
    if not is_valid_url:
        tag, error_details = build_invalid_download_url_error(url, invalid_reason)
        return (None, current_agent, tag, error_details) if return_tag else (None, current_agent)

    if rate_controller is not None and hasattr(rate_controller, "is_host_ignored"):
        ignored_host = rate_controller.is_host_ignored(url)
        if ignored_host:
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{ignored_host}' ignored by current configuration]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    if rate_controller is not None and hasattr(rate_controller, "check_host_cooldown"):
        cooldown_info = rate_controller.check_host_cooldown(url)
        if cooldown_info:
            host, wait_seconds = cooldown_info
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{host}' in cooldown for ~{wait_seconds}s]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    host_semaphore = None
    if rate_controller is not None and hasattr(rate_controller, "acquire_host_request_slot"):
        _, host_semaphore = rate_controller.acquire_host_request_slot(url)

    try:
        all_forbidden = True
        for user_agent in get_user_agent_list(current_agent):
            headers["User-Agent"] = user_agent
            while True:
                try:
                    if rate_controller is not None and (
                        not hasattr(rate_controller, "should_rate_limit_url")
                        or rate_controller.should_rate_limit_url(url)
                    ):
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

                except UnicodeDecodeError as e:
                    tag, error_details = build_invalid_redirect_error(
                        url,
                        e,
                        headers=headers,
                        params=params,
                        max_sec=max_sec,
                        method="GET",
                    )
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, tag, e)
                    return (None, user_agent, tag, error_details) if return_tag else (None, user_agent)
                except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema, LocationParseError) as e:
                    tag, error_details = build_invalid_download_url_error(url, e)
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, tag, e)
                    return (None, user_agent, tag, error_details) if return_tag else (None, user_agent)
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
    finally:
        if rate_controller is not None and hasattr(rate_controller, "release_host_request_slot"):
            rate_controller.release_host_request_slot(host_semaphore)


def make_request_post(url, current_agent, headers=None, json_body=None, max_sec=None, stream=False, sleep_time=3, return_tag=False, rate_controller=None):
    headers = headers.copy() if headers else {}

    is_valid_url, invalid_reason = validate_http_download_url(url)
    if not is_valid_url:
        tag, error_details = build_invalid_download_url_error(url, invalid_reason)
        return (None, current_agent, tag, error_details) if return_tag else (None, current_agent)

    if rate_controller is not None and hasattr(rate_controller, "is_host_ignored"):
        ignored_host = rate_controller.is_host_ignored(url)
        if ignored_host:
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{ignored_host}' ignored by current configuration]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    if rate_controller is not None and hasattr(rate_controller, "check_host_cooldown"):
        cooldown_info = rate_controller.check_host_cooldown(url)
        if cooldown_info:
            host, wait_seconds = cooldown_info
            tag = "resource_temporarily_unavailable"
            tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
            message = f"{tag_explanation} ({url}) - [host '{host}' in cooldown for ~{wait_seconds}s]"
            return (None, current_agent, tag, message) if return_tag else (None, current_agent)

    host_semaphore = None
    if rate_controller is not None and hasattr(rate_controller, "acquire_host_request_slot"):
        _, host_semaphore = rate_controller.acquire_host_request_slot(url)

    try:
        all_forbidden = True
        for user_agent in get_user_agent_list(current_agent):
            headers["User-Agent"] = user_agent
            while True:
                try:
                    if rate_controller is not None and (
                        not hasattr(rate_controller, "should_rate_limit_url")
                        or rate_controller.should_rate_limit_url(url)
                    ):
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

                except UnicodeDecodeError as e:
                    tag, error_details = build_invalid_redirect_error(
                        url,
                        e,
                        headers=headers,
                        json_body=json_body,
                        max_sec=max_sec,
                        method="POST",
                    )
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, tag, e)
                    return (None, user_agent, tag, error_details) if return_tag else (None, user_agent)
                except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema, LocationParseError) as e:
                    tag, error_details = build_invalid_download_url_error(url, e)
                    if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                        rate_controller.register_host_result(url, tag, e)
                    return (None, user_agent, tag, error_details) if return_tag else (None, user_agent)
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
    finally:
        if rate_controller is not None and hasattr(rate_controller, "release_host_request_slot"):
            rate_controller.release_host_request_slot(host_semaphore)


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

    if isinstance(e, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema, LocationParseError)):
        return "invalid_download_url"

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
    if status_code in [500, 502, 504, 522, 429]:
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
