from app.backend.extractor import event_dedup_key, local_extract, normalize_location
from app.backend.services import _event_similarity


def test_location_ignores_zai_inside_person_name_and_normalizes_alias():
    assert normalize_location("明在首尔总统府") == "韩国总统府"


def test_rewritten_reports_of_same_fact_are_similar():
    left = "何立峰在人民大会堂会见美国联邦众议员科雷亚及其一行"
    right = "国务院副总理何立峰在人民大会堂会见美国众议员科雷亚一行"
    assert _event_similarity(left, right) >= 0.72


def test_event_dedup_ignores_news_agency_dateline_and_reporter():
    first = "新华社首尔5月13日电（记者甲 乙）5月13日，韩国总统会见何立峰。"
    second = "本报首尔5月13日电 （记者丙、丁）5月13日，韩国总统会见何立峰。"
    assert event_dedup_key(1, "itinerary", "2026-05-13T00:00:00Z", first) == event_dedup_key(1, "itinerary", "2026-05-13T08:00:00+08:00", second)


def test_related_unclassified_fact_becomes_other():
    events = local_extract(
        {"title": "任免消息", "content_text": "7月5日，张三获颁年度公共服务奖。", "published_at": "2026-07-05T00:00:00Z"},
        [{"id": 1, "name": "张三", "aliases": []}], 0.7,
    )
    assert events[0]["event_type"] == "other"


def test_local_extractor_keeps_evidence_and_unknowns():
    document = {
        "title": "黄仁勋公开活动", "published_at": "2026-07-02T00:00:00+00:00", "language": "zh-CN",
        "content_text": "2026年7月2日，黄仁勋将在上海出席人工智能大会。\n黄仁勋表示：“人工智能将改变每一个行业。”",
    }
    persons = [{"id": 1, "name": "黄仁勋", "aliases": ["Jensen Huang"]}]
    events = local_extract(document, persons, 0.7)
    assert {event["event_type"] for event in events} == {"itinerary", "statement"}
    assert all(event["evidence_text"] in document["content_text"] for event in events)
    statement = next(event for event in events if event["event_type"] == "statement")
    assert statement["quote_text"] == "人工智能将改变每一个行业。"
