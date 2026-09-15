import json
import http.client
import ipaddress
import re
import socket
import ssl
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import unquote, urlparse, urlencode, urljoin

import requests
from urllib3.exceptions import LocationParseError
from url_normalize import url_normalize
from w3lib.url import url_query_cleaner

from .resources import CRAWLER_CHANGES_INFO, USER_AGENTS

class CatalogError(RuntimeError):
    pass

class PayloadError(RuntimeError):
    pass

def read_json(response, context):
    if response is None:
        raise CatalogError(f"No response while {context}")
    try:
        response.raise_for_status()
        return response.json()
    except Exception as error:
        raise CatalogError(f"Invalid catalog response while {context}: {error}") from error
    finally:
        response.close()

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
    if urlparse(domain).hostname in {"gbif.org", "www.gbif.org", "api.gbif.org"}:
        return "https://api.gbif.org"

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
                timeout=max_sec or 30,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=max_sec)

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        if params:
            path += ("&" if "?" in path else "?") + urlencode(params, doseq=True)

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

def build_too_many_redirects_error(url, redirect_error):
    response = getattr(redirect_error, "response", None)
    final_url = getattr(response, "url", None) or url
    history = getattr(response, "history", None) or []
    redirect_count = len(history) if history else 30
    location_header = response.headers.get("Location") if response is not None and getattr(response, "headers", None) else None

    tag = "too_many_redirects"
    tag_explanation = CRAWLER_CHANGES_INFO.get(tag, {}).get("tag_explanation", {}).get("reason", "Unknown reason")
    tag_values = {
        "<redirect_url>": url,
        "<final_url>": final_url,
        "<redirect_count>": redirect_count,
        "<location_header>": location_header or "unknown",
    }
    message = f"{tag_explanation} ({url}) - [final URL: {final_url}] - [redirects: {redirect_count}]"
    return tag, _build_error_details(message, tag_values)

class SafeSession(requests.Session):
    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        def origin(url):
            p = urlparse(url)
            return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)
        if origin(prepared_request.url) != origin(response.request.url):
            for key in ("Authorization", "X-CKAN-API-Key", "Proxy-Authorization"):
                prepared_request.headers.pop(key, None)

_sessions = threading.local()

def get_session():
    if not hasattr(_sessions, "session"):
        _sessions.session = SafeSession()
    return _sessions.session

def _retry_delay(response, attempt, base):
    value = response.headers.get("Retry-After") if response is not None else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, base * 2 ** attempt)

def _request(method, url, current_agent, headers=None, params=None, json_body=None,
             max_sec=None, stream=False, sleep_time=1, return_tag=False, rate_controller=None):
    agent = current_agent or "OpenDataCrawler/2.3"
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", agent)
    timeout = float(max_sec or getattr(rate_controller, "max_sec", None) or 30)
    if timeout <= 0:
        raise ValueError("max_sec must be positive")
    budget = float(getattr(rate_controller, "request_deadline_seconds", 120))
    deadline = time.monotonic() + budget
    max_attempts = max(1, int(getattr(rate_controller, "http_max_attempts", 3)))
    max_body = int(getattr(rate_controller, "max_metadata_bytes", 64 * 1024 * 1024))

    def fail(tag, message, response=None):
        details = {"message": str(message), "url": url}
        if response is not None:
            details["httpStatus"] = response.status_code
        if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
            error = requests.HTTPError(str(message), response=response) if response is not None else message
            rate_controller.register_host_result(url, tag, error)
        return (None, agent, tag, details) if return_tag else (response, agent)

    valid, reason = validate_http_download_url(url)
    if not valid:
        return fail("invalid_download_url", reason)
    if rate_controller is not None:
        if hasattr(rate_controller, "is_host_ignored") and rate_controller.is_host_ignored(url):
            return fail("resource_temporarily_unavailable", "Host excluded by configuration")
        if hasattr(rate_controller, "check_host_cooldown") and rate_controller.check_host_cooldown(url):
            return fail("resource_temporarily_unavailable", "Host cooldown is active")

    for attempt in range(max_attempts):
        response = None
        slot = None
        transferred = False
        try:
            if rate_controller is not None:
                if hasattr(rate_controller, "acquire_host_request_slot"):
                    _, slot = rate_controller.acquire_host_request_slot(url)
                if hasattr(rate_controller, "check_rate_limit"):
                    rate_controller.check_rate_limit()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("HTTP retry deadline exceeded")
            request_url, request_method = url, method
            request_params, request_json = params, json_body
            redirects = []
            for redirect_number in range(31):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout("Redirect deadline exceeded")
                response = get_session().request(
                    request_method, request_url, headers=request_headers, params=request_params,
                    json=request_json if request_method == "POST" else None,
                    verify=True, timeout=(min(10, timeout, remaining), min(timeout, remaining)),
                    stream=stream, allow_redirects=False,
                )
                if not response.is_redirect:
                    response.history = redirects
                    break
                if redirect_number == 30:
                    response.close()
                    raise requests.TooManyRedirects("Exceeded 30 redirects", response=response)
                next_url = urljoin(response.url, response.headers["Location"])
                valid, reason = validate_http_download_url(next_url)
                if not valid:
                    response.close()
                    raise requests.exceptions.InvalidURL(reason)
                prepared = requests.Request(request_method, next_url, headers=request_headers).prepare()
                SafeSession().rebuild_auth(prepared, response)
                request_headers = dict(prepared.headers)
                request_headers.pop("Cookie", None)
                if response.status_code == 303 or (response.status_code in {301, 302} and request_method == "POST"):
                    request_method, request_json = "GET", None
                    request_headers.pop("Content-Type", None)
                    request_headers.pop("Content-Length", None)
                redirects.append(response)
                response.close()
                if slot is not None:
                    rate_controller.release_host_request_slot(slot)
                    slot = None
                request_url, request_params = next_url, None
                if rate_controller is not None:
                    if hasattr(rate_controller, "is_host_ignored") and rate_controller.is_host_ignored(next_url):
                        return fail("resource_temporarily_unavailable", "Redirect target excluded by configuration")
                    if hasattr(rate_controller, "check_host_cooldown") and rate_controller.check_host_cooldown(next_url):
                        return fail("resource_temporarily_unavailable", "Redirect target in cooldown")
                    if hasattr(rate_controller, "acquire_host_request_slot"):
                        _, slot = rate_controller.acquire_host_request_slot(next_url)
                    if hasattr(rate_controller, "check_rate_limit"):
                        rate_controller.check_rate_limit()
            tag = get_https_error_tag(response.status_code)
            if tag:
                delay = _retry_delay(response, attempt, sleep_time)
                body = bytearray()
                try:
                    for chunk in response.iter_content(8192):
                        body.extend(chunk)
                        if len(body) >= 65536 or time.monotonic() >= deadline:
                            break
                    response._content = bytes(body[:65536])
                    response._content_consumed = True
                finally:
                    response.close()
                if tag == "resource_temporarily_unavailable" and attempt + 1 < max_attempts and time.monotonic() + delay < deadline:
                    if slot is not None:
                        rate_controller.release_host_request_slot(slot)
                        slot = None
                    time.sleep(delay)
                    continue
                return fail(tag, f"HTTP {response.status_code}", response)

            if not stream:
                if len(response.content) > max_body:
                    raise requests.RequestException("Metadata response exceeds configured byte limit")
                response.close()
                if slot is not None:
                    rate_controller.release_host_request_slot(slot)
                    slot = None
                if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                    rate_controller.register_host_result(url, None)
                return (response, agent, None, None) if return_tag else (response, agent)

            original_close = response.close
            original_iter = response.iter_content
            held_slot = slot
            released = False
            watchdog = None
            body_expired = threading.Event()
            release_lock = threading.Lock()
            def close():
                nonlocal released
                if watchdog is not None:
                    watchdog.cancel()
                try:
                    original_close()
                finally:
                    with release_lock:
                        if not released:
                            released = True
                            if held_slot is not None:
                                rate_controller.release_host_request_slot(held_slot)
            def bounded_iter(chunk_size=1, decode_unicode=False):
                total = 0
                body_deadline = time.monotonic() + float(getattr(rate_controller, "download_deadline_seconds", 600)) if stream else deadline
                try:
                    for chunk in original_iter(chunk_size=chunk_size, decode_unicode=decode_unicode):
                        total += len(chunk)
                        if not stream and total > max_body:
                            raise requests.RequestException("Metadata response exceeds configured byte limit")
                        if body_expired.is_set() or time.monotonic() >= body_deadline:
                            raise requests.Timeout("Response body deadline exceeded")
                        yield chunk
                    if body_expired.is_set():
                        raise requests.Timeout("Response body deadline exceeded")
                finally:
                    close()
            def abort_body():
                body_expired.set()
                raw = getattr(response, "raw", None)
                connection = getattr(raw, "_connection", None)
                sock = getattr(connection, "sock", None)
                if sock is None:
                    fp = getattr(getattr(raw, "_fp", None), "fp", None)
                    sock = getattr(getattr(fp, "raw", None), "_sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                close()
            body_budget = float(getattr(rate_controller, "download_deadline_seconds", 600)) if stream else max(0.001, deadline - time.monotonic())
            watchdog = threading.Timer(body_budget, abort_body)
            watchdog.daemon = True
            watchdog.start()
            response.close = close
            response.iter_content = bounded_iter
            transferred = True
            if rate_controller is not None and hasattr(rate_controller, "register_host_result"):
                rate_controller.register_host_result(url, None)
            return (response, agent, None, None) if return_tag else (response, agent)
        except (requests.RequestException, LocationParseError, UnicodeDecodeError) as error:
            if response is not None:
                response.close()
            tag = get_error_tag_from_exception(error)
            delay = max(0, sleep_time * 2 ** attempt)
            if tag == "resource_temporarily_unavailable" and not transferred and attempt + 1 < max_attempts and time.monotonic() + delay < deadline:
                if slot is not None:
                    rate_controller.release_host_request_slot(slot)
                    slot = None
                time.sleep(delay)
                continue
            return fail(tag, error)
        finally:
            if slot is not None and not transferred:
                rate_controller.release_host_request_slot(slot)
    return fail("resource_temporarily_unavailable", "HTTP retry budget exhausted")

def make_request(url, current_agent=None, headers=None, params=None, max_sec=None, stream=False, sleep_time=1, return_tag=False, rate_controller=None):
    return _request("GET", url, current_agent, headers=headers, params=params, max_sec=max_sec,
                    stream=stream, sleep_time=sleep_time, return_tag=return_tag, rate_controller=rate_controller)

def make_request_post(url, current_agent=None, headers=None, json_body=None, max_sec=None, stream=False, sleep_time=1, return_tag=False, rate_controller=None):
    return _request("POST", url, current_agent, headers=headers, json_body=json_body, max_sec=max_sec,
                    stream=stream, sleep_time=sleep_time, return_tag=return_tag, rate_controller=rate_controller)

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

    if isinstance(e, requests.exceptions.TooManyRedirects):
        return "too_many_redirects"

    if isinstance(e, requests.exceptions.SSLError):
        return "ssl_error"

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

    return "resource_temporarily_unavailable"

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
    if status_code is None or status_code < 400:
        return None
    specific = {400: "invalid_request", 401: "unauthorized_access", 403: "forbidden_resource",
                404: "missing_resource", 405: "method_not_allowed", 410: "resource_removed"}
    if status_code in specific:
        return specific[status_code]
    if status_code in {408, 425, 429, 499} or status_code >= 500:
        return "resource_temporarily_unavailable"
    return "invalid_request"

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
