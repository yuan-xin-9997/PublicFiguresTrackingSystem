import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ITINERARY_WORDS = ("访问", "出席", "前往", "抵达", "行程", "会见", "将于", "计划", "visit", "attend", "travel")
STATEMENT_WORDS = ("表示", "称", "指出", "强调", "宣布", "说", "statement", "said", "says", "announced")
DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日"),
]
QUOTE_PATTERN = re.compile(r"[“\"]([^”\"]{4,400})[”\"]")
LOCATION_PATTERN = re.compile(r"(?:在|前往|抵达|访问)([\u4e00-\u9fffA-Za-z·\s]{2,24}?)(?:举行|出席|访问|会见|表示|，|。|,|$)")
LOCATION_ALIASES = {"首尔总统府": "韩国总统府"}


def normalize_location(value: str) -> str:
    clean = " ".join(value.split()).strip("，。, .")
    # A person's name may contain 在 (for example 李在明). If the captured
    # candidate contains another 在, the actual prepositional location follows it.
    if "在" in clean:
        clean = clean.rsplit("在", 1)[-1].strip()
    return LOCATION_ALIASES.get(clean, clean)


def event_core_text(text: str) -> str:
    core = " ".join(text.split())
    core = re.sub(r"^(?:新华社|中新社|本报|本刊)[^。]{0,80}?(?:电|讯)\s*", "", core)
    core = re.sub(r"^[（(]记者[^）)]{1,80}[）)]\s*", "", core)
    core = re.sub(r"^\d{1,2}月\d{1,2}日[，,]?\s*", "", core)
    return core or " ".join(text.split())


def event_dedup_key(person_id: int, event_type: str, start_at: Optional[str], text: str) -> str:
    core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", event_core_text(text)).lower()
    raw = "{}|{}|{}|{}".format(person_id, event_type, (start_at or "")[:10], core[:80])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso_date(text: str, fallback: Optional[str]) -> Optional[str]:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            now = datetime.now(timezone.utc)
            year = int(match.groupdict().get("y") or now.year)
            try:
                return datetime(year, int(match.group("m")), int(match.group("d")), tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
    if fallback:
        try:
            normalized = fallback.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            return None
    return None


def _content_segments(text: str) -> List[str]:
    """Split flattened news pages without merging unrelated people and headlines."""
    segments: List[str] = []
    for paragraph in re.split(r"[\r\n]+", text):
        paragraph = " ".join(paragraph.split()).strip()
        if not paragraph:
            continue
        # HTML-to-text output commonly has no whitespace after Chinese punctuation.
        sentences = re.findall(r".*?(?:[。！？!?][”\"]?|$)", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            navigation_parts = re.split(r"(?:国内|国际)?活动更多>>\s*", sentence)
            # List/index pages often flatten many dated headlines into one punctuation-free
            # line. A new ISO-style date is a reliable boundary between those entries.
            for navigation_part in navigation_parts:
                dated_parts = re.split(r"\s+(?=20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", navigation_part)
                segments.extend(part.strip() for part in dated_parts if len(part.strip()) >= 6)
    return segments


def local_extract(document: Dict[str, Any], persons: List[Dict[str, Any]], review_threshold: float) -> List[Dict[str, Any]]:
    text = document["content_text"]
    segments = _content_segments(text)
    events: List[Dict[str, Any]] = []
    for person in persons:
        names = [person["name"]] + [a for a in person.get("aliases", []) if a]
        relevant = [segment for segment in segments if any(name.lower() in segment.lower() for name in names)]
        if not relevant and any(name.lower() in (document.get("title") or "").lower() for name in names):
            relevant = segments[:1] or [document["title"]]
        for segment in relevant[:8]:
            lowered = segment.lower()
            quote_match = QUOTE_PATTERN.search(segment)
            if quote_match or any(word in lowered for word in STATEMENT_WORDS):
                event_type = "statement"
            elif any(word in lowered for word in ITINERARY_WORDS):
                event_type = "itinerary"
            else:
                event_type = "other"
            start_at = _iso_date(segment, document.get("published_at"))
            location_match = LOCATION_PATTERN.search(segment)
            location = normalize_location(location_match.group(1)) if location_match and event_type == "itinerary" else ""
            confidence = 0.55 + (0.12 if start_at else 0) + (0.08 if quote_match else 0) + min(0.1, len(segment) / 1000)
            confirmation = "completed" if start_at and start_at <= datetime.now(timezone.utc).isoformat() else "expected"
            if any(word in segment for word in ("据称", "可能", "预计", "传闻", "或将")):
                confirmation = "rumored" if "传闻" in segment or "据称" in segment else "expected"
                confidence -= 0.1
            events.append({
                "person_id": person["id"], "event_type": event_type, "title": str(document.get("title") or "未命名材料")[:500],
                "summary": segment[:500], "start_at": start_at, "end_at": None,
                "original_timezone": "", "time_precision": "day" if start_at else "unknown",
                "location_name": location, "location_precision": "city" if location else "unknown",
                "confirmation_status": confirmation,
                "review_status": "approved" if confidence >= review_threshold and confirmation not in {"rumored", "disputed"} else "needs_review",
                "confidence": round(max(0.05, min(0.98, confidence)), 2),
                "quote_text": quote_match.group(1) if quote_match else "",
                "translated_text": "", "original_language": document.get("language", ""), "speech_context": "",
                "evidence_text": segment[:1000], "dedup_key": event_dedup_key(person["id"], event_type, start_at, segment),
            })
    return events


def external_extract(document: Dict[str, Any], persons: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    api_key = os.getenv(str(config.get("api_key_env", "PFTS_AI_API_KEY")), "")
    base_url = str(config.get("base_url", "")).rstrip("/")
    if not base_url or not api_key:
        raise ValueError("外部模型未配置")
    prompt = {
        "task": "只根据正文抽取公开人物相关事实，类型限行程、言论、其他；无法明确归为行程或言论时使用 other，不得遗漏相关事实。未知字段必须为空，证据必须逐字来自正文。",
        "persons": [{"id": p["id"], "name": p["name"], "aliases": p.get("aliases", [])} for p in persons],
        "document": {"title": document["title"], "published_at": document.get("published_at"), "content": document["content_text"][:12000]},
        "output": "JSON object with events array; fields: person_id,event_type,title,summary,start_at,location_name,confirmation_status,confidence,quote_text,evidence_text",
    }
    body = json.dumps({
        "model": config.get("model"), "temperature": 0,
        "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=int(config.get("timeout_seconds", 30))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    parsed = json.loads(payload["choices"][0]["message"]["content"])
    if not isinstance(parsed.get("events"), list):
        raise ValueError("模型返回缺少 events 数组")
    allowed_person_ids = {p["id"] for p in persons}
    events = []
    for item in parsed["events"]:
        if item.get("person_id") not in allowed_person_ids or item.get("event_type") not in {"itinerary", "statement", "other"}:
            continue
        evidence = str(item.get("evidence_text", ""))
        if not evidence or evidence not in document["content_text"]:
            continue
        extracted_title = str(item.get("title") or evidence)
        item["title"] = str(document.get("title") or "未命名材料")[:500]
        if item.get("event_type") == "other" and not item.get("start_at"):
            item["start_at"] = _iso_date("", document.get("published_at"))
        item["review_status"] = "approved" if float(item.get("confidence", 0)) >= float(config.get("review_threshold", 0.7)) else "needs_review"
        item.setdefault("time_precision", "day" if item.get("start_at") else "unknown")
        item.setdefault("location_precision", "city" if item.get("location_name") else "unknown")
        item.setdefault("end_at", None)
        item.setdefault("original_timezone", "")
        item.setdefault("translated_text", "")
        item.setdefault("original_language", document.get("language", ""))
        item.setdefault("speech_context", "")
        item["dedup_key"] = event_dedup_key(item["person_id"], item["event_type"], item.get("start_at"), extracted_title)
        events.append(item)
    return events


def extract(document: Dict[str, Any], persons: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    provider = str(config.get("provider", "local"))
    error = ""
    try:
        if provider == "local":
            events = local_extract(document, persons, float(config.get("review_threshold", 0.7)))
            model = "local-rules-v1"
        else:
            events = external_extract(document, persons, config)
            model = str(config.get("model", ""))
    except Exception as exc:
        error = "{}: {}".format(type(exc).__name__, str(exc)[:300])
        provider = "local-fallback"
        model = "local-rules-v1"
        events = local_extract(document, persons, float(config.get("review_threshold", 0.7)))
    return {
        "events": events, "provider": provider, "model": model, "error": error,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
