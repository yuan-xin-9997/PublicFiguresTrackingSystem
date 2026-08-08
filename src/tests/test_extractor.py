import json
import sqlite3

from app.backend.database import SCHEMA, migrate_event_time_to_publish_time, migrate_published_at_to_url_date
from app.backend.extractor import event_dedup_key, external_extract, extract, local_extract, normalize_location
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


def test_month_day_uses_article_timestamp_instead_of_runtime_year():
    events = local_extract(
        {
            "title": "聚焦建设五个中心重要使命",
            "content_text": "12月3日，张三强调加快推进重点工作。",
            "published_at": "2023-12-04T08:31:00+08:00",
            "language": "zh-CN",
        },
        [{"id": 1, "name": "张三", "aliases": []}], 0.7,
    )
    assert events[0]["start_at"] == "2023-12-04T00:31:00+00:00"
    assert events[0]["time_precision"] == "day"


def test_body_full_date_does_not_override_published_at():
    """The user-reported bug: a body-text full date (e.g. a referenced
    historical or effective date) MUST NOT override the article's publication
    time. start_at is always the published_at; time_precision is "day"."""
    events = local_extract(
        {
            "title": "李强签署国务院令 公布修订后的《集成电路布图设计保护条例》",
            "content_text": "2026年7月15日国务院公布修订后的条例，自2026年9月1日起施行。李强签署国务院令，并表示将推进相关工作。",
            "published_at": "2026-08-03T00:00:00+08:00",
            "language": "zh-CN",
        },
        [{"id": 1, "name": "李强", "aliases": []}], 0.7,
    )
    assert events
    for event in events:
        assert event["start_at"] == "2026-08-02T16:00:00+00:00"
        assert event["time_precision"] == "day"


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


def test_actor_location_and_statement_priority():
    document = {
        "title": "习近平会见柬埔寨首相洪玛奈",
        "published_at": "2026-07-16T00:00:00+00:00",
        "language": "zh-CN",
        "content_text": (
            "习近平会见柬埔寨首相洪玛奈。会见在上海西郊宾馆举行。"
            "习近平表示，中柬友谊历久弥新。习近平获赠纪念品。"
        ),
    }
    events = local_extract(document, [{"id": 1, "name": "习近平", "aliases": []}], 0.7)

    assert {event["event_type"] for event in events} == {"itinerary", "statement"}
    assert next(event for event in events if event["event_type"] == "itinerary")["location_name"] == "上海西郊宾馆"


def test_quoted_leader_is_not_mistaken_for_actor():
    events = local_extract(
        {
            "title": "李鸿忠在安徽、河南开展执法检查时强调",
            "published_at": "2026-07-16T00:00:00+00:00",
            "language": "zh-CN",
            "content_text": "李鸿忠在安徽、河南开展执法检查。他强调，要坚持以习近平总书记关于国家粮食安全重要论述精神为指导。",
        },
        [{"id": 1, "name": "习近平", "aliases": []}, {"id": 2, "name": "李鸿忠", "aliases": []}],
        0.7,
    )

    assert events and {event["person_id"] for event in events} == {2}
    assert {event["location_name"] for event in events} == {"安徽、河南"}


def test_abstract_zai_clause_is_not_a_location():
    events = local_extract(
        {
            "title": "习近平强调党建工作", "published_at": "2026-07-17T00:00:00+00:00",
            "language": "zh-CN", "content_text": "习近平强调，中国共产党在管党治党、兴党强党的伟大实践中形成新时代党建思想。",
        },
        [{"id": 1, "name": "习近平", "aliases": []}], 0.7,
    )
    assert events[0]["location_name"] == ""


def test_external_other_event_uses_document_publication_time(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            payload = {"events": [{
                "person_id": 1, "event_type": "other", "title": "获颁奖项",
                "summary": "张三获颁年度奖项。", "start_at": None, "location_name": "",
                "confirmation_status": "completed", "confidence": 0.8,
                "quote_text": "", "evidence_text": "张三获颁年度奖项。",
            }]}
            return json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()

    monkeypatch.setenv("TEST_AI_KEY", "secret")
    monkeypatch.setattr("app.backend.extractor.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    events = external_extract(
        {
            "title": "年度奖项", "content_text": "张三获颁年度奖项。",
            "published_at": "2026-07-08T09:30:00+08:00", "language": "zh-CN",
        },
        [{"id": 1, "name": "张三", "aliases": []}],
        {"base_url": "https://ai.example", "api_key_env": "TEST_AI_KEY", "model": "test", "review_threshold": 0.7},
    )

    assert events[0]["start_at"] == "2026-07-08T01:30:00+00:00"
    assert events[0]["time_precision"] == "day"


def test_target_person_must_be_the_action_subject():
    persons = [{"id": 1, "name": "习近平", "aliases": []}]
    for title, content in (
        ("吉尔吉斯斯坦总统扎帕罗夫会见王毅", "吉尔吉斯斯坦总统扎帕罗夫会见王毅。会见中转达了习近平的问候。"),
        ("王毅会见伊朗外长阿拉格齐", "王毅会见伊朗外长阿拉格齐。双方提到习近平主席重视两国关系。"),
        ("主席特使会见外宾", "习近平主席特使王毅会见来访外宾并交换意见。"),
    ):
        events = local_extract(
            {"title": title, "content_text": content, "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN"},
            persons, 0.7,
        )
        assert events == []


def test_relayed_greetings_are_not_attributed_to_message_originator():
    for evidence in [
        "扎帕罗夫请王毅转达对习近平主席的亲切问候，表示吉方愿深化合作。",
        "王毅转达习近平主席对扎帕罗夫的良好祝愿，表示双方应加强协作。",
    ]:
        result = extract(
            {
                "title": "吉尔吉斯斯坦总统扎帕罗夫会见王毅",
                "content_text": evidence,
                "published_at": "2026-07-22T10:00:00+08:00",
                "language": "zh-CN",
            },
            [{"id": 1, "name": "习近平", "aliases": []}],
            {"provider": "local", "review_threshold": 0.7},
        )

        assert result["events"] == []
        assert result["attribution_stats"]["rejection_reasons"]["target_not_subject"] >= 1


def test_background_guidance_and_name_only_do_not_create_other():
    persons = [{"id": 1, "name": "习近平", "aliases": []}]
    background = local_extract(
        {
            "title": "专题学习会", "content_text": "会议学习贯彻习近平重要论述精神，部署下一阶段工作。",
            "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
        },
        persons, 0.7,
    )
    name_only = local_extract(
        {
            "title": "背景资料", "content_text": "这份资料多次提到习近平的公开履历。",
            "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
        },
        persons, 0.7,
    )
    assert background == []
    assert name_only == []


def test_pronoun_continuation_is_limited_to_unique_same_paragraph_owner():
    persons = [
        {"id": 1, "name": "习近平", "aliases": []},
        {"id": 2, "name": "王毅", "aliases": []},
    ]
    clear = local_extract(
        {
            "title": "会见", "content_text": "习近平会见来宾。他表示，中方愿深化合作。",
            "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
        },
        persons, 0.7,
    )
    ambiguous = local_extract(
        {
            "title": "共同活动", "content_text": "习近平出席会议，王毅也出席会议。他表示，将推进后续工作。",
            "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
        },
        persons, 0.7,
    )
    cross_paragraph = local_extract(
        {
            "title": "会见", "content_text": "习近平会见来宾。\n他表示，中方愿深化合作。",
            "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
        },
        persons, 0.7,
    )
    assert any(event["person_id"] == 1 and event["event_type"] == "statement" for event in clear)
    assert not any(event["summary"].startswith("他表示") for event in ambiguous)
    assert not any(event["summary"].startswith("他表示") for event in cross_paragraph)


def test_external_wrong_person_is_rejected_and_fallback_uses_same_rules(monkeypatch):
    class WrongResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            payload = {"events": [{
                "person_id": 1, "event_type": "itinerary", "title": "错误归属",
                "summary": "习近平主席特使王毅会见来宾。", "start_at": None, "location_name": "",
                "confirmation_status": "completed", "confidence": 0.9, "quote_text": "",
                "evidence_text": "习近平主席特使王毅会见来宾。",
            }]}
            return json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()

    document = {
        "title": "王毅会见来宾", "content_text": "习近平主席特使王毅会见来宾。",
        "published_at": "2026-07-20T00:00:00Z", "language": "zh-CN",
    }
    persons = [{"id": 1, "name": "习近平", "aliases": []}]
    config = {
        "provider": "external", "base_url": "https://ai.example", "api_key_env": "TEST_AI_KEY",
        "model": "test", "review_threshold": 0.7,
    }
    monkeypatch.setenv("TEST_AI_KEY", "secret")
    monkeypatch.setattr("app.backend.extractor.urllib.request.urlopen", lambda *_args, **_kwargs: WrongResponse())
    external_result = extract(document, persons, config)
    assert external_result["events"] == []
    assert external_result["attribution_stats"]["rejected"] == 1

    monkeypatch.setattr(
        "app.backend.extractor.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    fallback_result = extract(document, persons, config)
    assert fallback_result["provider"] == "local-fallback"
    assert fallback_result["events"] == []


def test_external_body_date_in_start_at_is_overridden_by_published_at(monkeypatch):
    """The external model is no longer asked for start_at; even if it returns
    a body-text date, the system MUST overwrite it with the article's
    published_at."""
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self):
            payload = {"events": [{
                "person_id": 1, "event_type": "statement", "title": "签署",
                "summary": "李强签署国务院令并表示将推进相关工作。",
                "start_at": "2025-06-01T00:00:00+08:00",
                "location_name": "", "confirmation_status": "completed",
                "confidence": 0.8, "quote_text": "",
                "evidence_text": "李强签署国务院令并表示将推进相关工作。",
            }]}
            return json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}).encode()

    monkeypatch.setenv("TEST_AI_KEY2", "secret")
    monkeypatch.setattr("app.backend.extractor.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    events = external_extract(
        {
            "title": "签署", "content_text": "李强签署国务院令并表示将推进相关工作。",
            "published_at": "2026-08-03T00:00:00+08:00", "language": "zh-CN",
        },
        [{"id": 1, "name": "李强", "aliases": []}],
        {"base_url": "https://ai.example", "api_key_env": "TEST_AI_KEY2", "model": "test", "review_threshold": 0.7},
    )
    assert events[0]["start_at"] == "2026-08-02T16:00:00+00:00"
    assert events[0]["time_precision"] == "day"


def test_future_tense_event_uses_published_at_and_completed():
    """Future-scheduled events (将于/计划) also use the publish time. Their
    confirmation is "completed" (publish time is in the past), but rumor
    keywords (预计/或将) still flip the status to "expected"."""
    document = {
        "title": "公开行程", "published_at": "2026-08-05T00:00:00+08:00", "language": "zh-CN",
        "content_text": "李强将于8月20日访问俄罗斯。",
    }
    events = local_extract(document, [{"id": 1, "name": "李强", "aliases": []}], 0.7)
    assert events
    itinerary = next(event for event in events if event["event_type"] == "itinerary")
    assert itinerary["start_at"] == "2026-08-04T16:00:00+00:00"
    assert itinerary["time_precision"] == "day"
    assert itinerary["confirmation_status"] == "completed"

    rumored_document = {
        "title": "可能出席", "published_at": "2026-08-05T00:00:00+08:00", "language": "zh-CN",
        "content_text": "李强或将出席下周论坛。",
    }
    rumored_events = local_extract(rumored_document, [{"id": 1, "name": "李强", "aliases": []}], 0.7)
    assert rumored_events
    assert rumored_events[0]["start_at"] == "2026-08-04T16:00:00+00:00"
    assert rumored_events[0]["confirmation_status"] in ("expected", "rumored")


def _seed_migration_db():
    """Build an in-memory database with the production schema and seed it
    with events that simulate pre-migration body-date contamination."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO users(username,password_hash,role,enabled,created_at,updated_at) "
        "VALUES('admin','x','admin',1,datetime('now'),datetime('now'))"
    )
    connection.execute(
        "INSERT INTO public_figures(name,enabled,created_at,updated_at) VALUES('张三',1,datetime('now'),datetime('now'))"
    )
    person_id = 1
    connection.execute(
        "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES('seed','rss',datetime('now'),datetime('now'))"
    )
    source_id = connection.execute("SELECT id FROM information_sources WHERE name='seed'").fetchone()[0]
    # Document with a known published_at.
    connection.execute(
        "INSERT INTO raw_documents(source_id,canonical_url,title,author,published_at,collected_at,content_text,content_hash,fetch_metadata_json) "
        "VALUES(?, 'https://example/lq', '签署', '', '2026-08-03T00:00:00+08:00', '2026-08-03T00:00:00+08:00', '签署', 'h1','{}')",
        (source_id,),
    )
    document_id = connection.execute("SELECT id FROM raw_documents WHERE canonical_url='https://example/lq'").fetchone()[0]
    # Non-locked event with a body-date start_at and a stale dedup_key.
    connection.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,location_name,location_precision,confirmation_status,review_status,confidence,dedup_key,human_locked,created_at,updated_at) "
        "VALUES(?, 'other', '签署', '李强签署国务院令。', '2014-10-01T00:00:00+08:00', NULL, 'Asia/Shanghai', 'day', '', 'unknown', 'completed', 'approved', 0.6, 'stale-key-1', 0, datetime('now'), datetime('now'))",
        (person_id,),
    )
    target_event_id = connection.execute("SELECT id FROM timeline_events WHERE dedup_key='stale-key-1'").fetchone()[0]
    connection.execute(
        "INSERT INTO event_evidence(event_id, document_id, evidence_text, supports_fields_json, source_claim_json) "
        "VALUES(?, ?, '李强签署国务院令。', '[]', '{}')",
        (target_event_id, document_id),
    )
    # Human-locked event that MUST be skipped.
    connection.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,location_name,location_precision,confirmation_status,review_status,confidence,dedup_key,human_locked,created_at,updated_at) "
        "VALUES(?, 'other', '锁定事件', '人工锁定', '2010-01-01T00:00:00+08:00', NULL, 'Asia/Shanghai', 'day', '', 'unknown', 'completed', 'approved', 0.5, 'locked-key', 1, datetime('now'), datetime('now'))",
        (person_id,),
    )
    locked_event_id = connection.execute("SELECT id FROM timeline_events WHERE dedup_key='locked-key'").fetchone()[0]
    connection.execute(
        "INSERT INTO event_evidence(event_id, document_id, evidence_text, supports_fields_json, source_claim_json) "
        "VALUES(?, ?, '人工锁定证据。', '[]', '{}')",
        (locked_event_id, document_id),
    )
    # Second non-locked event pointing at the same document with the same evidence
    # text. After the migration they collapse onto the same dedup_key; the
    # smaller id wins and the other is deleted.
    connection.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,location_name,location_precision,confirmation_status,review_status,confidence,dedup_key,human_locked,created_at,updated_at) "
        "VALUES(?, 'other', '签署副本', '李强签署国务院令。', '2014-10-01T00:00:00+08:00', NULL, 'Asia/Shanghai', 'day', '', 'unknown', 'completed', 'approved', 0.6, 'stale-key-2', 0, datetime('now'), datetime('now'))",
        (person_id,),
    )
    dup_event_id = connection.execute("SELECT id FROM timeline_events WHERE dedup_key='stale-key-2'").fetchone()[0]
    connection.execute(
        "INSERT INTO event_evidence(event_id, document_id, evidence_text, supports_fields_json, source_claim_json) "
        "VALUES(?, ?, '李强签署国务院令。', '[]', '{}')",
        (dup_event_id, document_id),
    )
    return connection, document_id, target_event_id, locked_event_id, dup_event_id


def test_migration_repairs_start_at_and_merges_duplicates_and_is_idempotent():
    connection, document_id, target_id, locked_id, dup_id = _seed_migration_db()
    counts = migrate_event_time_to_publish_time(connection)
    assert counts["repaired"] == 1
    assert counts["deleted_duplicates"] == 1
    assert counts["skipped_locked"] == 1

    rows = list(connection.execute(
        "SELECT id, person_id, event_type, start_at, time_precision, original_timezone, dedup_key, human_locked "
        "FROM timeline_events ORDER BY id"
    ).fetchall())
    # The locked event is untouched.
    locked_row = next(row for row in rows if row["id"] == locked_id)
    assert locked_row["start_at"] == "2010-01-01T00:00:00+08:00"
    assert locked_row["dedup_key"] == "locked-key"
    assert locked_row["human_locked"] == 1
    # The duplicate non-locked event was removed; the original survivor was
    # rewritten to the document's published_at with a recomputed dedup_key.
    surviving_ids = {row["id"] for row in rows if row["id"] != locked_id}
    assert surviving_ids == {target_id}
    repaired_row = next(row for row in rows if row["id"] == target_id)
    assert repaired_row["start_at"] == "2026-08-02T16:00:00+00:00"
    assert repaired_row["time_precision"] == "day"
    assert repaired_row["original_timezone"] == "Asia/Shanghai"
    assert repaired_row["dedup_key"] != "stale-key-1"
    assert repaired_row["human_locked"] == 0
    # An audit row was written.
    audit_count = connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    assert audit_count == 1

    # Idempotency: a second pass produces no further changes.
    second = migrate_event_time_to_publish_time(connection)
    assert second["repaired"] == 0
    assert second["deleted_duplicates"] == 0
    again = connection.execute(
        "SELECT start_at, dedup_key FROM timeline_events WHERE id=?", (target_id,)
    ).fetchone()
    assert again[0] == "2026-08-02T16:00:00+00:00"
    connection.close()


def _seed_published_at_pollution_db():
    """Simulate a document whose published_at was wrongly inferred from a body
    effective date (10-15) while the URL publish path says 08-03, and an event
    whose start_at was set to that polluted published_at by the V10 migration."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO users(username,password_hash,role,enabled,created_at,updated_at) "
        "VALUES('admin','x','admin',1,datetime('now'),datetime('now'))"
    )
    connection.execute(
        "INSERT INTO public_figures(name,enabled,created_at,updated_at) VALUES('李强',1,datetime('now'),datetime('now'))"
    )
    person_id = 1
    connection.execute(
        "INSERT INTO information_sources(name,type,created_at,updated_at) VALUES('seed','rss',datetime('now'),datetime('now'))"
    )
    source_id = connection.execute("SELECT id FROM information_sources WHERE name='seed'").fetchone()[0]
    # Document: polluted published_at (10-15 body effective date), URL path 20260803 (08-03).
    connection.execute(
        "INSERT INTO raw_documents(source_id,canonical_url,title,author,published_at,collected_at,content_text,content_hash,fetch_metadata_json) "
        "VALUES(?, 'https://www.news.cn/politics/leaders/20260803/c.html', '李强签署国务院令', '', "
        "'2026-10-14T16:00:00+00:00', '2026-08-03T09:45:27+00:00', '正文', 'h1','{}')",
        (source_id,),
    )
    document_id = connection.execute(
        "SELECT id FROM raw_documents WHERE canonical_url='https://www.news.cn/politics/leaders/20260803/c.html'"
    ).fetchone()[0]
    # Non-locked event whose start_at was set to the polluted published_at.
    connection.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,location_name,location_precision,confirmation_status,review_status,confidence,dedup_key,human_locked,created_at,updated_at) "
        "VALUES(?, 'statement', '签署', '李强签署国务院令并表示推进。', '2026-10-14T16:00:00+00:00', NULL, 'Asia/Shanghai', 'day', '', 'unknown', 'completed', 'approved', 0.6, 'stale-pub-key', 0, datetime('now'), datetime('now'))",
        (person_id,),
    )
    target_event_id = connection.execute(
        "SELECT id FROM timeline_events WHERE dedup_key='stale-pub-key'"
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO event_evidence(event_id, document_id, evidence_text, supports_fields_json, source_claim_json) "
        "VALUES(?, ?, '李强签署国务院令并表示推进。', '[]', '{}')",
        (target_event_id, document_id),
    )
    # Human-locked event that must be skipped.
    connection.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,end_at,original_timezone,time_precision,location_name,location_precision,confirmation_status,review_status,confidence,dedup_key,human_locked,created_at,updated_at) "
        "VALUES(?, 'other', '锁定事件', '人工锁定', '2010-01-01T00:00:00+00:00', NULL, 'Asia/Shanghai', 'day', '', 'unknown', 'completed', 'approved', 0.5, 'locked-pub-key', 1, datetime('now'), datetime('now'))",
        (person_id,),
    )
    locked_event_id = connection.execute(
        "SELECT id FROM timeline_events WHERE dedup_key='locked-pub-key'"
    ).fetchone()[0]
    return connection, document_id, target_event_id, locked_event_id


def test_migration_resets_polluted_published_at_and_recomputes_events():
    connection, document_id, target_id, locked_id = _seed_published_at_pollution_db()
    counts = migrate_published_at_to_url_date(connection)
    assert counts["repaired_documents"] == 1
    assert counts["repaired_events"] == 1
    assert counts["skipped_locked"] == 1

    # Document published_at reset to the URL publish day (08-03 Beijing).
    doc = connection.execute(
        "SELECT published_at FROM raw_documents WHERE id=?", (document_id,)
    ).fetchone()
    assert doc["published_at"] == "2026-08-03T00:00:00+08:00"

    # Event start_at recomputed from the corrected published_at.
    row = connection.execute(
        "SELECT start_at, time_precision, dedup_key, human_locked FROM timeline_events WHERE id=?",
        (target_id,),
    ).fetchone()
    assert row["start_at"] == "2026-08-02T16:00:00+00:00"
    assert row["time_precision"] == "day"
    assert row["dedup_key"] != "stale-pub-key"
    assert row["human_locked"] == 0

    # Locked event untouched.
    locked = connection.execute(
        "SELECT start_at, dedup_key, human_locked FROM timeline_events WHERE id=?", (locked_id,)
    ).fetchone()
    assert locked["start_at"] == "2010-01-01T00:00:00+00:00"
    assert locked["dedup_key"] == "locked-pub-key"

    # Audit row written.
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='migrate_published_at_to_url'"
    ).fetchone()[0] == 1

    # Idempotent: a second pass produces no further changes.
    second = migrate_published_at_to_url_date(connection)
    assert second["repaired_documents"] == 0
    assert second["repaired_events"] == 0
    doc2 = connection.execute(
        "SELECT published_at FROM raw_documents WHERE id=?", (document_id,)
    ).fetchone()
    assert doc2["published_at"] == "2026-08-03T00:00:00+08:00"
    connection.close()
