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


def test_flattened_profile_index_does_not_become_another_persons_itinerary():
    document = {
        "title": "王沪宁-人物资料",
        "published_at": "2026-07-04T00:00:00+00:00",
        "language": "zh-CN",
        "content_text": (
            "王沪宁 汉族，1955年10月生，山东莱州人 现任中共中央政治局常委，"
            "十四届全国政协主席 国内活动更多>> 学习贯彻习近平总书记在庆祝中国共产党"
            "成立105周年大会上的重要讲话精神 2026-07-04 庆祝中国共产党成立105周年大会"
            "在京隆重举行 2026-07-02 王沪宁出席建设强大国内市场调研协商座谈会。"
        ),
    }
    persons = [
        {"id": 1, "name": "习近平", "aliases": []},
        {"id": 2, "name": "王沪宁", "aliases": []},
    ]

    events = local_extract(document, persons, 0.7)

    xi_events = [event for event in events if event["person_id"] == 1]
    assert all(event["event_type"] != "itinerary" for event in xi_events)
    assert not any(event["title"].startswith("王沪宁 汉族") for event in xi_events)
    assert any(event["person_id"] == 2 and event["event_type"] == "itinerary" for event in events)
