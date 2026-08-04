import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple


ITINERARY_WORDS = ("访问", "出席", "前往", "抵达", "行程", "会见", "开展", "检查", "调研", "考察", "将于", "计划", "visit", "attend", "travel")
STATEMENT_WORDS = ("表示", "称", "指出", "强调", "宣布", "说", "statement", "said", "says", "announced")
OTHER_WORDS = ("获颁", "获赠", "获得", "赢得", "当选", "就任", "担任", "卸任", "辞去", "逝世", "去世", "被任命", "任命为")
DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<m>\d{1,2})月(?P<d>\d{1,2})日"),
]
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
QUOTE_PATTERN = re.compile(r"[“\"]([^”\"]{4,400})[”\"]")
LOCATION_PATTERN = re.compile(r"(?:在|前往|抵达|访问)([\u4e00-\u9fffA-Za-z·、\s]{2,30}?)(?=举行|出席|访问|会见|表示|指出|强调|宣布|开展|进行|调研|考察|检查|主持|召开)")
LOCATION_ALIASES = {"首尔总统府": "韩国总统府"}
SUBJECT_WINDOW = 72
BACKGROUND_PATTERNS = (
    re.compile(r"以[^，。；]{0,8}{name}[^，。；]{0,36}(?:为指导|为指引|精神)"),
    re.compile(r"(?:学习|贯彻|落实|遵循)[^，。；]{0,16}{name}[^，。；]{0,36}(?:讲话|论述|思想|精神|指示|要求)"),
    re.compile(r"{name}(?:主席|总书记)?(?:的)?(?:特使|代表|思想|精神|论述|指示|要求)[^，。；]{0,30}"),
    re.compile(
        r"(?:请[^，。；]{0,12})?(?:转达|代转)(?:对)?[^，。；]{0,8}{name}"
        r"(?:主席|总书记)?(?:的)?[^，。；]{0,20}(?:问候|祝愿|致意|慰问)"
    ),
)


def normalize_location(value: str) -> str:
    clean = " ".join(value.split()).strip("，。, .")
    # A person's name may contain 在 (for example 李在明). If the captured
    # candidate contains another 在, the actual prepositional location follows it.
    if "在" in clean:
        clean = clean.rsplit("在", 1)[-1].strip()
    return LOCATION_ALIASES.get(clean, clean)


def _person_names(person: Dict[str, Any]) -> List[str]:
    return [str(value).strip() for value in [person.get("name"), *person.get("aliases", [])] if str(value or "").strip()]


def _predicate_positions(text: str, event_type: str) -> List[int]:
    lowered = text.lower()
    words = {
        "itinerary": ITINERARY_WORDS,
        "statement": STATEMENT_WORDS,
        "other": OTHER_WORDS,
    }[event_type]
    positions = [match.start() for word in words for match in re.finditer(re.escape(word.lower()), lowered)]
    if event_type == "statement":
        positions.extend(match.start() for match in QUOTE_PATTERN.finditer(text))
    return sorted(set(positions))


def _event_types(text: str) -> List[str]:
    return [
        event_type for event_type in ("itinerary", "statement", "other")
        if _predicate_positions(text, event_type)
    ]


def _latest_name_before(text: str, names: List[str], position: int) -> Tuple[int, str]:
    lowered = text.lower()
    matches: List[Tuple[int, str]] = []
    for name in names:
        name_lower = name.lower()
        cursor = lowered.find(name_lower)
        while 0 <= cursor < position:
            matches.append((cursor, name))
            cursor = lowered.find(name_lower, cursor + len(name_lower))
    return max(matches, default=(-1, ""), key=lambda item: item[0])


def _clause_start(text: str, predicate_position: int) -> int:
    return max(text.rfind(mark, 0, predicate_position) for mark in ("\n", "。", "！", "？", "!", "?", "；", ";")) + 1


def _background_mention(text: str, name: str, mention_position: int, predicate_position: int) -> bool:
    start = max(0, _clause_start(text, predicate_position))
    context = text[start:predicate_position]
    escaped = re.escape(name)
    for pattern in BACKGROUND_PATTERNS:
        match = re.search(pattern.pattern.replace("{name}", escaped), context, flags=re.IGNORECASE)
        if match and match.start() <= mention_position - start <= match.end():
            return True
    return False


def validate_subject_evidence(
    evidence: str,
    person: Dict[str, Any],
    persons: List[Dict[str, Any]],
    event_type: str,
    previous_person_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """Conservatively prove that ``person`` owns a predicate in ``evidence``."""
    text = " ".join(str(evidence or "").split()).strip()
    if not text:
        return False, "evidence_empty"
    predicates = _predicate_positions(text, event_type)
    if not predicates:
        return False, "predicate_missing"
    names = _person_names(person)
    target_id = int(person["id"])
    pronoun = re.match(
        r"^(?:20\d{2}年\d{1,2}月\d{1,2}日[，,]?\s*)?(?:他|她)(?:表示|指出|强调|宣布|称|说)",
        text,
    )
    if pronoun and event_type == "statement":
        if previous_person_id == target_id:
            return True, "pronoun_continuation"
        return False, "pronoun_ambiguous"

    saw_target = any(name.lower() in text.lower() for name in names)
    if not saw_target:
        return False, "target_not_mentioned"
    for predicate_position in predicates:
        mention_position, matched_name = _latest_name_before(text, names, predicate_position)
        if mention_position < 0:
            continue
        start = _clause_start(text, predicate_position)
        if mention_position < start or predicate_position - (mention_position + len(matched_name)) > SUBJECT_WINDOW:
            continue
        if _background_mention(text, matched_name, mention_position, predicate_position):
            continue
        competing: List[Tuple[int, int]] = []
        for candidate in persons:
            if int(candidate["id"]) == target_id:
                continue
            position, _ = _latest_name_before(text, _person_names(candidate), predicate_position)
            if position >= start:
                competing.append((position, int(candidate["id"])))
        if competing and max(competing)[0] > mention_position:
            continue
        return True, "explicit_subject"
    return False, "target_not_subject"


def _primary_person_id(text: str, persons: List[Dict[str, Any]]) -> Optional[int]:
    """Compatibility helper returning a unique explicitly validated owner."""
    owners = []
    for person in persons:
        if any(validate_subject_evidence(text, person, persons, event_type)[0] for event_type in _event_types(text)):
            owners.append(int(person["id"]))
    return owners[0] if len(set(owners)) == 1 else None


def _nearby_location(segments: List[str], index: int) -> str:
    for candidate_index in (index, index + 1, index - 1):
        if 0 <= candidate_index < len(segments):
            candidate = segments[candidate_index]
            match = LOCATION_PATTERN.search(candidate)
            if match and (candidate_index == index or "举行" in candidate or any(word in candidate.lower() for word in ITINERARY_WORDS)):
                return normalize_location(match.group(1))
    return ""


def _prefer_statements(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    statement_people = {event["person_id"] for event in events if event["event_type"] == "statement"}
    return [event for event in events if event["event_type"] != "other" or event["person_id"] not in statement_people]


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
    # A month/day mention is not sufficiently anchored on its own: using the
    # process year turned old articles into current-year events. Prefer the
    # article's complete timestamp unless the evidence states the year too.
    match = DATE_PATTERNS[0].search(text)
    if match:
        try:
            return datetime(
                int(match.group("y")), int(match.group("m")), int(match.group("d")),
                tzinfo=BEIJING_TIMEZONE,
            ).isoformat()
        except ValueError:
            return None
    if fallback:
        try:
            normalized = fallback.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            return None
    match = DATE_PATTERNS[1].search(text)
    if match:
        try:
            now = datetime.now(BEIJING_TIMEZONE)
            return datetime(now.year, int(match.group("m")), int(match.group("d")), tzinfo=BEIJING_TIMEZONE).isoformat()
        except ValueError:
            return None
    return None


def _content_units(text: str) -> List[Dict[str, Any]]:
    """Split flattened news pages without merging unrelated people and headlines."""
    units: List[Dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(re.split(r"[\r\n]+", text)):
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
                for part_index, part in enumerate(dated_parts):
                    clean = part.strip()
                    if len(clean) >= 6:
                        units.append({
                            "text": clean,
                            "paragraph": paragraph_index,
                            "list_boundary": part_index > 0 or clean != sentence,
                        })
    return units


def _content_segments(text: str) -> List[str]:
    return [unit["text"] for unit in _content_units(text)]


def _previous_explicit_owner(units: List[Dict[str, Any]], index: int, persons: List[Dict[str, Any]]) -> Optional[int]:
    if index <= 0 or units[index - 1]["paragraph"] != units[index]["paragraph"] or units[index]["list_boundary"]:
        return None
    previous = units[index - 1]["text"]
    owners = {
        int(person["id"])
        for person in persons
        for event_type in _event_types(previous)
        if validate_subject_evidence(previous, person, persons, event_type)[0]
    }
    return next(iter(owners)) if len(owners) == 1 else None


def validate_document_evidence_subject(
    document: Dict[str, Any],
    evidence: str,
    person: Dict[str, Any],
    persons: List[Dict[str, Any]],
    event_type: str,
) -> Tuple[bool, str]:
    units = _content_units(str(document.get("content_text") or ""))
    index = next(
        (candidate for candidate, unit in enumerate(units) if evidence in unit["text"] or unit["text"] in evidence),
        -1,
    )
    previous_person_id = _previous_explicit_owner(units, index, persons) if index >= 0 else None
    return validate_subject_evidence(evidence, person, persons, event_type, previous_person_id)


def _record_rejection(stats: Dict[str, Any], reason: str) -> None:
    stats["rejected"] += 1
    reasons = stats.setdefault("rejection_reasons", {})
    reasons[reason] = int(reasons.get(reason, 0)) + 1


def _local_extract_with_stats(
    document: Dict[str, Any], persons: List[Dict[str, Any]], review_threshold: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    units = _content_units(document["content_text"])
    segments = [unit["text"] for unit in units]
    events: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {"candidates": 0, "accepted": 0, "rejected": 0, "rejection_reasons": {}}
    for segment_index, segment in enumerate(segments):
        event_types = _event_types(segment)
        if not event_types:
            continue
        previous_person_id = _previous_explicit_owner(units, segment_index, persons)
        for person in persons:
            has_target_or_pronoun = any(name.lower() in segment.lower() for name in _person_names(person)) or bool(
                re.match(r"^(?:他|她)(?:表示|指出|强调|宣布|称|说)", segment)
            )
            if not has_target_or_pronoun:
                continue
            for event_type in event_types:
                stats["candidates"] += 1
                valid, reason = validate_subject_evidence(
                    segment, person, persons, event_type, previous_person_id,
                )
                if not valid:
                    _record_rejection(stats, reason)
                    continue
                quote_match = QUOTE_PATTERN.search(segment) if event_type == "statement" else None
                start_at = _iso_date(segment, document.get("published_at"))
                has_explicit_full_date = bool(DATE_PATTERNS[0].search(segment))
                location = _nearby_location(segments, segment_index)
                confidence = 0.55 + (0.12 if start_at else 0) + (0.08 if quote_match else 0) + min(0.1, len(segment) / 1000)
                confirmation = "completed" if start_at and start_at <= datetime.now(timezone.utc).isoformat() else "expected"
                if any(word in segment for word in ("据称", "可能", "预计", "传闻", "或将")):
                    confirmation = "rumored" if "传闻" in segment or "据称" in segment else "expected"
                    confidence -= 0.1
                events.append({
                    "person_id": person["id"], "event_type": event_type,
                    "title": str(document.get("title") or "未命名材料")[:500],
                    "summary": segment[:500], "start_at": start_at, "end_at": None,
                    "original_timezone": "Asia/Shanghai" if start_at else "",
                    "time_precision": "day" if has_explicit_full_date else ("exact" if start_at else "unknown"),
                    "location_name": location, "location_precision": "city" if location else "unknown",
                    "confirmation_status": confirmation,
                    "review_status": "approved",
                    "confidence": round(max(0.05, min(0.98, confidence)), 2),
                    "quote_text": quote_match.group(1) if quote_match else "",
                    "translated_text": "", "original_language": document.get("language", ""), "speech_context": "",
                    "evidence_text": segment[:1000],
                    "dedup_key": event_dedup_key(person["id"], event_type, start_at, segment),
                })
                stats["accepted"] += 1
    return _prefer_statements(events), stats


def local_extract(document: Dict[str, Any], persons: List[Dict[str, Any]], review_threshold: float) -> List[Dict[str, Any]]:
    return _local_extract_with_stats(document, persons, review_threshold)[0]


def _external_extract_with_stats(
    document: Dict[str, Any], persons: List[Dict[str, Any]], config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    api_key = os.getenv(str(config.get("api_key_env", "PFTS_AI_API_KEY")), "")
    base_url = str(config.get("base_url", "")).rstrip("/")
    if not base_url or not api_key:
        raise ValueError("外部模型未配置")
    prompt = {
        "task": "只根据正文抽取公开人物相关事实，类型限行程、言论、其他；逐个动作或言论谓词识别语法主语，事件只能归属实施动作、发表言论或承受明确事实的主体。不得归属仅被引用、作为指导思想、身份修饰或背景提及的人物；只有姓名命中不得输出其他事件。同一人物同篇材料已有言论时不要再输出其他。地点应从事件句及相邻句明确公开的场所中提取。未知字段必须为空，证据必须逐字来自正文。",
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
    segments = _content_segments(document["content_text"])
    events = []
    stats: Dict[str, Any] = {"candidates": 0, "accepted": 0, "rejected": 0, "rejection_reasons": {}}
    for item in parsed["events"]:
        stats["candidates"] += 1
        if item.get("person_id") not in allowed_person_ids or item.get("event_type") not in {"itinerary", "statement", "other"}:
            _record_rejection(stats, "schema_or_person_invalid")
            continue
        evidence = str(item.get("evidence_text", ""))
        if not evidence or evidence not in document["content_text"]:
            _record_rejection(stats, "evidence_not_in_document")
            continue
        person = next(person for person in persons if person["id"] == item["person_id"])
        valid, reason = validate_document_evidence_subject(
            document, evidence, person, persons, str(item["event_type"]),
        )
        if not valid:
            _record_rejection(stats, reason)
            continue
        extracted_title = str(item.get("title") or evidence)
        item["title"] = str(document.get("title") or "未命名材料")[:500]
        if not item.get("location_name"):
            segment_index = next((index for index, segment in enumerate(segments) if evidence in segment or segment in evidence), -1)
            item["location_name"] = _nearby_location(segments, segment_index) if segment_index >= 0 else ""
        if item.get("event_type") == "other" and not item.get("start_at"):
            item["start_at"] = _iso_date("", document.get("published_at"))
        item["review_status"] = "approved"
        item.setdefault("time_precision", "day" if item.get("start_at") else "unknown")
        item.setdefault("location_precision", "city" if item.get("location_name") else "unknown")
        item.setdefault("end_at", None)
        item.setdefault("original_timezone", "")
        item.setdefault("translated_text", "")
        item.setdefault("original_language", document.get("language", ""))
        item.setdefault("speech_context", "")
        item["dedup_key"] = event_dedup_key(item["person_id"], item["event_type"], item.get("start_at"), extracted_title)
        events.append(item)
        stats["accepted"] += 1
    return _prefer_statements(events), stats


def external_extract(document: Dict[str, Any], persons: List[Dict[str, Any]], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _external_extract_with_stats(document, persons, config)[0]


def extract(document: Dict[str, Any], persons: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    provider = str(config.get("provider", "local"))
    error = ""
    try:
        if provider == "local":
            events, attribution_stats = _local_extract_with_stats(
                document, persons, float(config.get("review_threshold", 0.7)),
            )
            model = "local-rules-v2"
        else:
            events, attribution_stats = _external_extract_with_stats(document, persons, config)
            model = str(config.get("model", ""))
    except Exception as exc:
        error = "{}: {}".format(type(exc).__name__, str(exc)[:300])
        provider = "local-fallback"
        model = "local-rules-v2"
        events, attribution_stats = _local_extract_with_stats(
            document, persons, float(config.get("review_threshold", 0.7)),
        )
    return {
        "events": events, "provider": provider, "model": model, "error": error,
        "attribution_stats": attribution_stats,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
