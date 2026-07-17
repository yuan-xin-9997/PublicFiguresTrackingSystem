def test_health_auth_and_permissions(client):
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").json()["status"] == "ready"
    assert client.get("/api/v1/dashboard/summary").status_code == 401

    login = client.post("/api/v1/auth/login", json={"username": "analyst", "password": "reader123"})
    assert login.status_code == 200
    assert "timeline" in login.json()["user"]["pages"]
    assert client.get("/api/v1/sources").status_code == 403
    assert client.post("/api/v1/auth/logout").status_code == 200


def test_complete_manual_document_to_review_flow(admin_client):
    person = admin_client.post("/api/v1/persons", json={
        "name": "黄仁勋", "native_name": "Jensen Huang", "bio": "", "organization": "NVIDIA",
        "title": "CEO", "country_region": "美国", "language": "zh-CN", "avatar_path": "",
        "enabled": True, "aliases": ["Jensen Huang", "黃仁勳"],
    })
    assert person.status_code == 201, person.text
    person_id = person.json()["id"]

    source = admin_client.post("/api/v1/sources", json={
        "name": "人工公开材料", "type": "manual", "entry_url": "", "organization": "",
        "language": "zh-CN", "trust_level": 4, "schedule_seconds": 3600,
        "enabled": True, "person_ids": [person_id],
    })
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    document = admin_client.post("/api/v1/documents/manual", json={
        "source_id": source_id, "title": "黄仁勋上海公开行程",
        "content_text": "2026年7月2日，黄仁勋将在上海出席人工智能大会。\n黄仁勋表示：“人工智能将改变每一个行业。”",
        "canonical_url": "https://example.com/public/1", "author": "测试记者",
        "published_at": "2026-07-02T08:00:00+08:00",
    })
    assert document.status_code == 201, document.text
    assert document.json()["event_count"] == 2

    events = admin_client.get("/api/v1/events", params={"person_id": person_id, "page_size": 20})
    assert events.status_code == 200
    assert events.json()["total"] == 2
    assert {item["title"] for item in events.json()["items"]} == {"黄仁勋上海公开行程"}
    assert {item["source_names"] for item in events.json()["items"]} == {"人工公开材料"}
    first = events.json()["items"][0]
    detail = admin_client.get("/api/v1/events/{}".format(first["id"]))
    assert detail.status_code == 200
    assert detail.json()["evidence"][0]["canonical_url"] == "https://example.com/public/1"

    reviewed = admin_client.post("/api/v1/events/{}/review".format(first["id"]), json={
        "action": "approve", "reason": "证据与原文一致"
    })
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "approved"

    second_id = next(item["id"] for item in events.json()["items"] if item["id"] != first["id"])
    rejected = admin_client.post("/api/v1/events/{}/review".format(second_id), json={"action": "reject", "reason": "测试驳回"})
    assert rejected.status_code == 200
    assert admin_client.get("/api/v1/events").json()["total"] == 1
    rejected_list = admin_client.get("/api/v1/events", params={"review_status": "rejected"}).json()
    assert rejected_list["total"] == 1
    assert rejected_list["items"][0]["id"] == second_id
    assert reviewed.json()["human_locked"] == 1
    assert reviewed.json()["history"][0]["reason"] == "证据与原文一致"

    dashboard = admin_client.get("/api/v1/dashboard/summary").json()
    assert dashboard["counts"]["persons"] == 1
    assert dashboard["counts"]["sources"] == 1
    assert dashboard["counts"]["events_today"] == 1

    task = admin_client.get("/api/v1/tasks").json()["items"][0]
    run = admin_client.post("/api/v1/tasks/{}/run".format(task["id"]))
    assert run.status_code == 200
    assert run.json()["status"] == "success"


def test_timeline_filters_by_date_location_and_sort_order(admin_client, configured_app):
    db = configured_app.state.db
    person_id = db.execute(
        "INSERT INTO public_figures(name,created_at,updated_at) VALUES(?,?,?)", ("测试人物", "2026-07-01", "2026-07-01")
    )
    rows = [
        (person_id, "itinerary", "北京事件", "摘要", "2026-07-01T00:00:00+00:00", "北京", "filter-1", "2026-07-01", "2026-07-01"),
        (person_id, "itinerary", "上海事件", "摘要", "2026-07-05T00:00:00+00:00", "上海", "filter-2", "2026-07-05", "2026-07-05"),
        (person_id, "itinerary", "深圳事件", "摘要", "2026-07-09T00:00:00+00:00", "深圳", "filter-3", "2026-07-09", "2026-07-09"),
    ]
    db.execute_many(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,start_at,location_name,dedup_key,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)", rows,
    )

    filtered = admin_client.get("/api/v1/events", params=[
        ("start_date", "2026-07-01"), ("end_date", "2026-07-05"),
        ("location", "北京"), ("location", "上海"), ("sort_order", "asc"),
    ])
    assert filtered.status_code == 200
    assert [item["title"] for item in filtered.json()["items"]] == ["北京事件", "上海事件"]
    assert admin_client.get("/api/v1/events", params={"sort_order": "invalid"}).status_code == 422
    assert admin_client.get("/api/v1/events", params={"start_date": "2026-07-10", "end_date": "2026-07-01"}).status_code == 422
    assert admin_client.get("/api/v1/events/locations").json()["items"] == ["上海", "北京", "深圳"]


def test_timeline_hides_legacy_other_when_same_document_has_statement(admin_client, configured_app):
    db = configured_app.state.db
    person_id = db.execute("INSERT INTO public_figures(name,created_at,updated_at) VALUES('张三','2026-07-01','2026-07-01')")
    source_id = db.execute("INSERT INTO information_sources(name,type,created_at,updated_at) VALUES('新华社','manual','2026-07-01','2026-07-01')")
    document_id = db.execute(
        "INSERT INTO raw_documents(source_id,title,collected_at,content_text,content_hash,status) VALUES(?,?,?,?,?,'analyzed')",
        (source_id, "张三发表讲话", "2026-07-01", "张三发表讲话。", "statement-doc"),
    )
    event_ids = []
    for event_type in ("statement", "other"):
        event_ids.append(db.execute(
            "INSERT INTO timeline_events(person_id,event_type,title,summary,dedup_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (person_id, event_type, "张三发表讲话", "摘要", "legacy-" + event_type, "2026-07-01", "2026-07-01"),
        ))
    db.execute_many(
        "INSERT INTO event_evidence(event_id,document_id,evidence_text) VALUES(?,?,?)",
        [(event_id, document_id, "张三发表讲话。") for event_id in event_ids],
    )

    payload = admin_client.get("/api/v1/events", params={"person_id": person_id}).json()
    assert [(item["event_type"], item["source_names"]) for item in payload["items"]] == [("statement", "新华社")]


def test_config_masking_and_user_permissions(admin_client):
    config = admin_client.get("/api/v1/config/effective")
    assert config.status_code == 200
    payload = config.json()["config"]
    assert payload["security"]["password_file"] == "******"
    assert payload["ai"]["api_key_env"]["environment_variable"] == "PFTS_AI_API_KEY"

    users = admin_client.get("/api/v1/users").json()
    analyst = next(user for user in users["items"] if user["username"] == "analyst")
    changed = admin_client.put("/api/v1/users/{}/permissions".format(analyst["id"]), json={"pages": ["dashboard", "timeline"]})
    assert changed.status_code == 200
    assert changed.json()["pages"] == ["dashboard", "timeline"]

    audit = admin_client.get("/api/v1/audit-logs").json()
    assert any(item["action"] == "permissions" for item in audit["items"])


def test_admin_can_preview_and_cleanup_navigation_documents(admin_client, configured_app):
    person = admin_client.post("/api/v1/persons", json={
        "name": "习近平", "native_name": "", "bio": "", "organization": "", "title": "",
        "country_region": "中国", "language": "zh-CN", "avatar_path": "", "enabled": True, "aliases": [],
    }).json()
    source = admin_client.post("/api/v1/sources", json={
        "name": "新华网", "type": "website", "entry_url": "https://www.news.cn/", "organization": "",
        "language": "zh-CN", "trust_level": 5, "schedule_seconds": 3600, "enabled": True,
        "person_ids": [person["id"]], "discovery_enabled": True, "discovery_max_pages": 10,
        "discovery_max_depth": 1,
    }).json()
    db = configured_app.state.db
    document_id = db.execute(
        "INSERT INTO raw_documents(source_id,canonical_url,title,collected_at,content_text,content_hash,status) "
        "VALUES(?,?,?,?,?,?,?)",
        (source["id"], "https://www.news.cn/politics/xxjxs/", "新华网首页 专题首页 最新播报 评论解读 智库报告",
         "2026-07-08T00:00:00+00:00", "习近平专题首页 最新播报 评论解读 智库报告 权威速览" * 5, "nav-hash", "analyzed"),
    )
    event_id = db.execute(
        "INSERT INTO timeline_events(person_id,event_type,title,summary,dedup_key,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (person["id"], "other", "错误聚合事件", "摘要", "nav-event", "2026-07-08", "2026-07-08"),
    )
    db.execute(
        "INSERT INTO event_evidence(event_id,document_id,evidence_text) VALUES(?,?,?)",
        (event_id, document_id, "习近平专题首页"),
    )

    preview = admin_client.post("/api/v1/maintenance/cleanup-navigation-pages", json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json()["documents"] == 1
    assert preview.json()["events"] == 1
    assert db.fetch_one("SELECT id FROM raw_documents WHERE id=?", (document_id,))

    cleaned = admin_client.post("/api/v1/maintenance/cleanup-navigation-pages", json={"dry_run": False})
    assert cleaned.status_code == 200
    assert cleaned.json()["deleted"] is True
    assert db.fetch_one("SELECT id FROM raw_documents WHERE id=?", (document_id,)) is None
    assert db.fetch_one("SELECT id FROM timeline_events WHERE id=?", (event_id,)) is None
    assert any(item["action"] == "cleanup_navigation_pages" for item in admin_client.get("/api/v1/audit-logs").json()["items"])


def test_person_can_be_edited_and_soft_deleted(admin_client):
    created = admin_client.post("/api/v1/persons", json={
        "name": "测试人物", "native_name": "", "bio": "初始简介", "organization": "甲组织",
        "title": "初始职位", "country_region": "中国", "language": "zh-CN", "avatar_path": "",
        "enabled": True, "aliases": ["旧别名"],
    })
    assert created.status_code == 201
    person_id = created.json()["id"]

    updated = admin_client.put("/api/v1/persons/{}".format(person_id), json={
        "name": "更新人物", "native_name": "Updated Person", "bio": "更新简介", "organization": "乙组织",
        "title": "更新职位", "country_region": "中国", "language": "zh-CN", "avatar_path": "",
        "enabled": True, "aliases": ["新别名", "Updated Person"],
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "更新人物"
    assert set(updated.json()["aliases"]) == {"新别名", "Updated Person"}

    deleted = admin_client.delete("/api/v1/persons/{}".format(person_id))
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert admin_client.get("/api/v1/persons/{}".format(person_id)).status_code == 404
    assert all(item["id"] != person_id for item in admin_client.get("/api/v1/persons").json()["items"])
    audit_items = admin_client.get("/api/v1/audit-logs").json()["items"]
    assert any(item["action"] == "delete" and item["object_id"] == str(person_id) for item in audit_items)


def test_source_can_be_edited_discovered_and_soft_deleted(admin_client):
    person = admin_client.post("/api/v1/persons", json={
        "name": "何立峰", "native_name": "", "bio": "", "organization": "", "title": "",
        "country_region": "中国", "language": "zh-CN", "avatar_path": "", "enabled": True,
        "aliases": ["He Lifeng"],
    }).json()
    created = admin_client.post("/api/v1/sources", json={
        "name": "政府网站", "type": "website", "entry_url": "https://example.com/", "organization": "",
        "language": "zh-CN", "trust_level": 5, "schedule_seconds": 7200, "enabled": True,
        "person_ids": [person["id"]], "discovery_enabled": True, "discovery_max_pages": 10,
        "discovery_max_depth": 1,
    })
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    listed = next(item for item in admin_client.get("/api/v1/sources").json()["items"] if item["id"] == source_id)
    assert listed["display_type"] == "website"
    assert listed["discovery_enabled"] is True
    assert listed["discovery_max_pages"] == 10

    updated = admin_client.put("/api/v1/sources/{}".format(source_id), json={
        "name": "更新政府网站", "type": "website", "entry_url": "https://example.com/news", "organization": "",
        "language": "zh-CN", "trust_level": 4, "schedule_seconds": 3600, "enabled": True,
        "person_ids": [person["id"]], "discovery_enabled": True, "discovery_max_pages": 20,
        "discovery_max_depth": 2,
    })
    assert updated.status_code == 200
    relisted = next(item for item in admin_client.get("/api/v1/sources").json()["items"] if item["id"] == source_id)
    assert relisted["name"] == "更新政府网站"
    assert relisted["discovery_max_depth"] == 2

    deleted = admin_client.delete("/api/v1/sources/{}".format(source_id))
    assert deleted.status_code == 200
    assert all(item["id"] != source_id for item in admin_client.get("/api/v1/sources").json()["items"])
    task = next(item for item in admin_client.get("/api/v1/tasks").json()["items"] if item["source_id"] == source_id)
    assert task["enabled"] == 0
