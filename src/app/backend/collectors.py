import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Set


PUBLISHED_DATE_PATTERNS = (
    re.compile(r"(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2}):(?P<minute>\d{2}))?"),
    re.compile(r"(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?:[ T](?P<h>\d{1,2}):(?P<minute>\d{2}))?"),
)
ARTICLE_END_MARKERS = (
    "(责编：", "（责编：", "责任编辑：", "编辑：", "分享让更多人看到", "客户端下载",
    "相关阅读", "相关新闻", "版权声明", "免责声明", "违法和不良信息举报",
)


NAVIGATION_MARKERS = (
    "news", "politics", "government", "gov", "leadership", "current", "list", "index",
    "新闻", "时政", "政务", "领导", "活动", "要闻", "资讯",
)
NAVIGATION_TITLE_MARKERS = (
    "首页", "专题首页", "最新播报", "评论解读", "智库报告", "权威速览",
    "要点海报", "学习快评", "更多>>", "更多 >",
)
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".rar",
    ".mp3", ".mp4", ".avi", ".css", ".js", ".xml",
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.title_parts: List[str] = []
        self.skip_depth = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: List[Any]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        clean = " ".join(data.split())
        if clean:
            self.parts.append(clean)
            if self.in_title:
                self.title_parts.append(clean)


def strip_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return "\n".join(parser.parts)


def canonicalize_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value.strip())
    # Government sites expose traditional-Chinese gateway URLs for the same
    # underlying article. Store the original URL so both variants deduplicate.
    if (parsed.hostname or "").lower() == "big5.www.gov.cn" and parsed.path.startswith("/gate/big5/www.gov.cn/"):
        parsed = urllib.parse.urlsplit("https://www.gov.cn/" + parsed.path.split("/gate/big5/www.gov.cn/", 1)[1])
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    clean_path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), clean_path, urllib.parse.urlencode(query), ""))


def infer_published_at(url: str, text: str) -> Optional[str]:
    for pattern in PUBLISHED_DATE_PATTERNS:
        if match := pattern.search(text[:5000]):
            values = match.groupdict()
            return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:00+08:00".format(
                int(values["y"]), int(values["m"]), int(values["d"]),
                int(values.get("h") or 0), int(values.get("minute") or 0),
            )
    path = urllib.parse.urlsplit(url).path
    match = re.search(r"/(20\d{2})/(\d{2})(\d{2})(?:/|$)", path)
    if match:
        return "{}-{}-{}T00:00:00+08:00".format(*match.groups())
    return None


def clean_article_content(url: str, title: str, content: str) -> str:
    clean = " ".join((content or "").split())
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    # Generic boundary rule: if the exact article title occurs after a large
    # navigation prefix, discard everything before it.
    title_index = clean.find(title.strip()) if title.strip() else -1
    if title_index > 100:
        clean = clean[title_index:]
    # Common Chinese news toolbar pattern. Start at the actual dispatch lead,
    # rather than retaining source/date/share/font controls before it.
    lead = re.search(r"(?:新华社|中新社|本报|本刊)[^\s。]{1,50}(?:电|讯)", clean)
    if lead and lead.start() > 0:
        clean = clean[lead.start():]
    ends = [clean.find(marker) for marker in ARTICLE_END_MARKERS if clean.find(marker) >= 0]
    if ends:
        clean = clean[:min(ends)]

    # Site-specific supplements belong below the generic pipeline and should
    # only handle markup conventions that cannot be inferred generally.
    if host == "people.com.cn" or host.endswith(".people.com.cn"):
        # People.cn navigation is frequently composed of ordinary div elements,
        # so generic readability extraction cannot identify it as <nav>.
        clean = re.sub(r"^(?:打开\s+)?(?:登录|订阅|取消订阅|已收藏|收藏|大字号|小字号)\s+", "", clean)
    return clean.strip()


def ensure_safe_url(value: str, allow_private_hosts: bool = False) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只允许不含内嵌凭证的 HTTP/HTTPS URL")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("无法解析来源主机") from exc
    if not allow_private_hosts:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("来源地址位于未授权的私有或保留网络")


def direct_fetch_url(url: str, config: Dict[str, Any]) -> Dict[str, Any]:
    ensure_safe_url(url, bool(config.get("allow_private_hosts", False)))
    request = urllib.request.Request(url, headers={"User-Agent": str(config.get("user_agent", "PFTS/1.0"))})
    max_bytes = int(config.get("max_response_bytes", 2_000_000))
    timeout = int(config.get("timeout_seconds", 15))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        ensure_safe_url(final_url, bool(config.get("allow_private_hosts", False)))
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("来源响应超过大小限制")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        return {"url": final_url, "body": payload.decode(charset, errors="replace"), "content_type": content_type, "status": response.status}


def _webfetch_request(path: str, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    base_url = str(config.get("webfetch_base_url") or "").rstrip("/")
    key_env = str(config.get("webfetch_api_key_env") or "PFTS_WEBFETCH_API_KEY")
    api_key = os.getenv(key_env, "").strip()
    if not base_url:
        raise ValueError("集中抓取服务地址未配置")
    if not api_key:
        raise ValueError("集中抓取服务 API Key 环境变量 {} 未设置".format(key_env))
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    timeout = int(config.get("timeout_seconds", 15)) + 5
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {})
            message = detail.get("message") or "HTTP {}".format(exc.code)
        except (ValueError, UnicodeDecodeError):
            message = "HTTP {}".format(exc.code)
        raise ValueError("集中抓取服务请求失败：{}".format(message)) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError("集中抓取服务不可用：{}".format(str(exc)[:200])) from exc
    if not isinstance(data, dict):
        raise ValueError("集中抓取服务返回格式无效")
    return data


def webfetch_fetch_url(url: str, config: Dict[str, Any], mode: str = "auto") -> Dict[str, Any]:
    # Basic syntax validation remains local; DNS/IP safety and redirect checks belong to WebFetch.
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只允许不含内嵌凭证的 HTTP/HTTPS URL")
    save_artifact = mode != "http" or bool(config.get("save_rss_artifacts", False))
    payload = {
        "url": url,
        "mode": mode,
        "profile": str(config.get("webfetch_profile") or "anonymous"),
        "proxy_policy": str(config.get("webfetch_proxy_policy") or "auto"),
        "cache_ttl": int(config.get("webfetch_cache_ttl", 900)),
        "force_refresh": False,
        "save_artifact": save_artifact,
        "timeout_seconds": int(config.get("timeout_seconds", 15)),
    }
    data = _webfetch_request("/v1/fetch", payload, config)
    if not data.get("success") or not isinstance(data.get("body"), str):
        raise ValueError("集中抓取失败，状态码 {}".format(data.get("status_code", "未知")))
    return {
        "url": str(data.get("final_url") or url),
        "body": data["body"],
        "content_type": str(data.get("content_type") or "application/octet-stream"),
        "status": int(data.get("status_code", 0)),
        "fetch_metadata": {
            "provider": "webfetch",
            "request_id": data.get("request_id"),
            "artifact_id": data.get("artifact_id"),
            "strategy": data.get("strategy"),
            "from_cache": bool(data.get("from_cache", False)),
            "fetched_at": data.get("fetched_at"),
            "attempts": data.get("attempts", []),
        },
    }


def webfetch_extract_article(artifact_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if not artifact_id:
        raise ValueError("集中抓取结果缺少 artifact_id")
    response = _webfetch_request(
        "/v1/extract",
        {"artifact_id": artifact_id, "adapter": "generic.article", "adapter_version": "latest"},
        config,
    )
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("集中正文提取结果无效")
    return data


def webfetch_extract_links(artifact_id: str, config: Dict[str, Any]) -> List[Dict[str, str]]:
    if not artifact_id:
        raise ValueError("集中抓取结果缺少 artifact_id")
    response = _webfetch_request(
        "/v1/extract",
        {"artifact_id": artifact_id, "adapter": "generic.links", "adapter_version": "latest"},
        config,
    )
    links = response.get("data", {}).get("links")
    if not isinstance(links, list):
        raise ValueError("集中链接提取结果无效")
    return [
        {"text": str(item.get("text") or ""), "href": str(item.get("href") or "")}
        for item in links if isinstance(item, dict) and item.get("href")
    ]


def fetch_url(url: str, config: Dict[str, Any], mode: str = "auto") -> Dict[str, Any]:
    provider = str(config.get("provider") or "webfetch").lower()
    if provider == "direct":
        result = direct_fetch_url(url, config)
        result["fetch_metadata"] = {"provider": "direct", "strategy": "http"}
        return result
    if provider != "webfetch":
        raise ValueError("未知抓取服务提供方：{}".format(provider))
    try:
        return webfetch_fetch_url(url, config, mode)
    except ValueError:
        if not bool(config.get("direct_fallback", False)):
            raise
        result = direct_fetch_url(url, config)
        result["fetch_metadata"] = {"provider": "direct-fallback", "strategy": "http"}
        return result


def _parser_config(source: Dict[str, Any]) -> Dict[str, Any]:
    raw = source.get("parser_config") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _site_key(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"com.cn", "org.cn", "net.cn", "gov.cn", "edu.cn"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _discovery_profile(root_host: str) -> Dict[str, Any]:
    if root_host.endswith("www.gov.cn"):
        return {"hosts": {"www.gov.cn", "sousuo.www.gov.cn"}, "search": "https://sousuo.www.gov.cn/sousuo/search.shtml?code=17da70961a7&dataTypeId=107&searchWord={query}"}
    if root_host.endswith("people.com.cn"):
        return {"hosts": {"people.com.cn"}, "search": "http://search.people.cn/s/?keyword={query}"}
    if root_host.endswith("xinhuanet.com") or root_host.endswith("news.cn"):
        return {"hosts": {"xinhuanet.com", "news.cn"}, "search": "https://so.news.cn/#search/0/{query}/1/0"}
    return {"hosts": {_site_key(root_host)}}


def _host_allowed(host: str, allowed: Set[str]) -> bool:
    host = host.lower().strip(".")
    return any(host == item or host.endswith("." + item) for item in allowed)


def _same_site_link(href: str, base_url: str, allowed_hosts: Set[str]) -> str:
    normalized = canonicalize_url(urllib.parse.urljoin(base_url, href))
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname or "", allowed_hosts):
        return ""
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return ""
    return normalized


def _looks_like_navigation(url: str, text: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if leaf in {"", "index", "index.html", "index.htm", "index.shtml", "default.html", "home.html"}:
        return True
    if path.endswith("/"):
        return any(marker in (text + " " + path).lower() for marker in NAVIGATION_MARKERS)
    if path.endswith((".html", ".htm", ".shtml")):
        return False
    return any(marker in path for marker in ("list", "index", "channel", "search", "news", "politics", "gov"))


def _article_rejection_reason(document: Dict[str, Any]) -> str:
    url = str(document.get("canonical_url") or "")
    title = " ".join(str(document.get("title") or "").split())
    content = " ".join(str(document.get("content_text") or "").split())
    if _looks_like_navigation(url, title):
        return "URL 或标题指向栏目/首页"
    if not title or len(title) > 180:
        return "标题缺失或异常过长"
    marker_count = sum(1 for marker in NAVIGATION_TITLE_MARKERS if marker in title)
    if marker_count >= 3:
        return "标题包含多个导航栏目"
    if len(content) < 12:
        return "正文过短"
    # A flattened portal page often repeats many navigation labels in both its
    # extracted title and body, even when the generic article adapter returns data.
    content_marker_count = sum(1 for marker in NAVIGATION_TITLE_MARKERS if marker in content[:1200])
    if marker_count >= 1 and content_marker_count >= 4 and not document.get("published_at"):
        return "页面缺少发布时间且正文呈导航聚合结构"
    return ""


def _article_document(url: str, source: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    result = fetch_url(url, config, "auto")
    metadata = result.get("fetch_metadata", {})
    if metadata.get("provider") == "webfetch" and metadata.get("artifact_id"):
        try:
            extracted = webfetch_extract_article(str(metadata["artifact_id"]), config)
            raw_content = str(extracted.get("content") or "").strip()
            if raw_content:
                title = str(extracted.get("title") or source["name"])
                content = clean_article_content(result["url"], title, raw_content)
                return {
                    "canonical_url": canonicalize_url(result["url"]),
                    "title": title,
                    "author": str(extracted.get("author") or ""),
                    "published_at": extracted.get("date") or infer_published_at(result["url"], raw_content),
                    "content_text": content,
                    "fetch_metadata": metadata,
                }
        except ValueError as exc:
            metadata["extract_fallback_reason"] = str(exc)[:300]
    parser = TextExtractor()
    parser.feed(result["body"])
    title = " ".join(parser.title_parts) or source["name"]
    content = clean_article_content(result["url"], title, "\n".join(parser.parts))
    return {
        "canonical_url": canonicalize_url(result["url"]),
        "title": title,
        "author": "", "published_at": infer_published_at(result["url"], content), "content_text": content,
        "fetch_metadata": metadata,
    }


def discover_website(source: Dict[str, Any], config: Dict[str, Any], max_items: int) -> List[Dict[str, Any]]:
    parser_config = _parser_config(source)
    max_pages = min(int(parser_config.get("discovery_max_pages", 12)), max(3, max_items * 3), 50)
    max_depth = min(max(int(parser_config.get("discovery_max_depth", 1)), 0), 2)
    root_url = canonicalize_url(source["entry_url"])
    root_host = (urllib.parse.urlsplit(root_url).hostname or "").lower()
    profile = _discovery_profile(root_host)
    configured_hosts = parser_config.get("discovery_allowed_hosts") or []
    allowed_hosts = {str(host).lower().strip(".") for host in configured_hosts if str(host).strip()} or set(profile["hosts"])
    terms = list(dict.fromkeys(
        str(term).strip().lower() for term in source.get("discovery_terms", []) if str(term).strip()
    ))
    if not terms:
        raise ValueError("网站自动发现来源没有关联人物姓名或别名")

    stats = {"pages_scanned": 0, "links_extracted": 0, "same_site_links": 0, "search_pages": 0,
             "candidates": 0, "articles_fetched": 0, "accepted": 0, "errors": []}
    queue = [(root_url, 0)]
    queued = {root_url}
    scanned = set()
    candidates: List[str] = []
    candidate_set = set()
    while queue and len(scanned) < max_pages and len(candidates) < max_items * 4:
        page_url, depth = queue.pop(0)
        if page_url in scanned:
            continue
        scanned.add(page_url)
        try:
            page = fetch_url(page_url, config, "auto")
        except ValueError as exc:
            stats["errors"].append(str(exc)[:200])
            continue
        stats["pages_scanned"] += 1
        metadata = page.get("fetch_metadata", {})
        artifact_id = metadata.get("artifact_id")
        if not artifact_id:
            continue
        links = webfetch_extract_links(str(artifact_id), config)
        stats["links_extracted"] += len(links)
        for link in links:
            url = _same_site_link(link["href"], page["url"], allowed_hosts)
            if not url or url == root_url:
                continue
            stats["same_site_links"] += 1
            haystack = (link["text"] + " " + url).lower()
            if any(term in haystack for term in terms):
                if _looks_like_navigation(url, link["text"]):
                    continue
                if url not in candidate_set:
                    candidate_set.add(url)
                    candidates.append(url)
                continue
            if depth < max_depth and url not in queued and _looks_like_navigation(url, link["text"]):
                queued.add(url)
                queue.append((url, depth + 1))

    # Prefer a site's own search when known. Search result links are already scoped
    # by the person query, and the article body is verified again below.
    search_template = str(parser_config.get("discovery_search_url_template") or profile.get("search") or "")
    if search_template:
        # Chinese official sites generally index the native Chinese name much
        # better than a Latin alias. Preserve source order and prefer CJK text.
        search_term = next((term for term in terms if re.search(r"[\u3400-\u9fff]", term)), terms[0])
        query = urllib.parse.quote(search_term)
        search_url = search_template.replace("{query}", query)
        try:
            # Search portals are commonly JavaScript SPAs whose HTTP response is
            # only an app shell. Force browser rendering before extracting links.
            page = fetch_url(search_url, config, "browser")
            artifact_id = page.get("fetch_metadata", {}).get("artifact_id")
            if artifact_id:
                links = webfetch_extract_links(str(artifact_id), config)
                stats["search_pages"] += 1
                stats["links_extracted"] += len(links)
                for link in links:
                    url = _same_site_link(link["href"], page["url"], allowed_hosts)
                    if url and url not in candidate_set and not _looks_like_navigation(url, link["text"]):
                        candidate_set.add(url)
                        candidates.append(url)
        except ValueError as exc:
            stats["errors"].append("站内搜索：" + str(exc)[:180])

    documents: List[Dict[str, Any]] = []
    stats["candidates"] = len(candidates)
    for candidate in candidates[:max_items]:
        try:
            document = _article_document(candidate, source, config)
            stats["articles_fetched"] += 1
        except ValueError as exc:
            stats["errors"].append(str(exc)[:200])
            continue
        document["fetch_metadata"]["discovered_from"] = root_url
        document["fetch_metadata"]["discovery_terms"] = terms
        rejection_reason = _article_rejection_reason(document)
        if rejection_reason:
            stats["errors"].append("拒绝非文章页 {}：{}".format(candidate, rejection_reason))
            continue
        if any(term in (document["title"] + " " + document["content_text"][:4000]).lower() for term in terms):
            documents.append(document)
    stats["accepted"] = len(documents)
    stats["errors"] = stats["errors"][:10]
    source["_discovery_stats"] = stats
    return documents


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: List[str]) -> str:
    for child in list(node):
        if _tag_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def parse_feed(xml_text: str, source_url: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_text)
    entries = [node for node in root.iter() if _tag_name(node.tag) in {"item", "entry"}]
    documents: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        title = _child_text(entry, ["title"]) or "未命名条目"
        link = _child_text(entry, ["link"])
        if not link:
            for child in list(entry):
                if _tag_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        link = urllib.parse.urljoin(source_url, link)
        content = _child_text(entry, ["content", "encoded", "description", "summary"])
        published = _child_text(entry, ["pubdate", "published", "updated"])
        author = _child_text(entry, ["author", "creator"])
        text = strip_html(content) or title
        fallback = "{}#entry-{}-{}".format(source_url, index, hashlib.sha256((title + text).encode("utf-8")).hexdigest()[:12])
        documents.append({
            "canonical_url": canonicalize_url(link or fallback), "title": html.unescape(title),
            "author": author, "published_at": published or None, "content_text": text,
        })
    return documents


def collect_source(source: Dict[str, Any], config: Dict[str, Any], max_items: int) -> List[Dict[str, Any]]:
    if source["type"] == "manual":
        return []
    if source["type"] == "web_page" and bool(_parser_config(source).get("discovery_enabled", False)):
        return discover_website(source, config, max_items)
    if source["type"] == "rss":
        result = fetch_url(source["entry_url"], config, "http")
        documents = parse_feed(result["body"], result["url"])[:max_items]
        for document in documents:
            document["fetch_metadata"] = result.get("fetch_metadata", {})
        return documents
    return [_article_document(source["entry_url"], source, config)]
