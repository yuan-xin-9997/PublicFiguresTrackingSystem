import json
import urllib.error

import pytest

from app.backend.collectors import _article_rejection_reason, _same_site_link, canonicalize_url, clean_article_content, collect_source, ensure_safe_url, infer_published_at, parse_feed, strip_html


def test_canonicalize_url_removes_tracking_parameters():
    value = canonicalize_url("HTTPS://Example.COM/news?id=3&utm_source=x#part")
    assert value == "https://example.com/news?id=3"


def test_canonicalize_url_collapses_gov_traditional_gateway():
    value = canonicalize_url("http://big5.www.gov.cn/gate/big5/www.gov.cn/yaowen/content_1.htm")
    assert value == "https://www.gov.cn/yaowen/content_1.htm"


def test_infer_published_at_from_chinese_body_then_url():
    assert infer_published_at("https://example.com/a", "2026年06月17日12:17 | 来源：新华社") == "2026-06-17T12:17:00+08:00"
    assert infer_published_at("http://people.com.cn/n1/2026/0513/c1.html", "无日期") == "2026-05-13T00:00:00+08:00"


def test_people_article_content_removes_navigation_and_footer():
    text = "首页 党政 时政 " * 30 + "何立峰会见代表 2026年05月13日 来源：新华社 订阅 小字号 新华社北京5月13日电 正文内容。 (责编：甲、乙) 分享让更多人看到"
    clean = clean_article_content("http://politics.people.com.cn/n1/2026/0513/a.html", "何立峰会见代表", text)
    assert clean.startswith("新华社北京5月13日电")
    assert "首页 党政" not in clean
    assert "责编" not in clean


def test_generic_article_content_uses_title_boundary_and_common_footer():
    text = "网站首页 产品 新闻 联系我们 " * 20 + "正式文章标题 正文第一段。正文第二段。责任编辑：某某 相关阅读"
    clean = clean_article_content("https://news.example.org/a.html", "正式文章标题", text)
    assert clean == "正式文章标题 正文第一段。正文第二段。"


def test_parse_rss_and_strip_html():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>公开活动</title><link>https://example.com/a?utm_medium=rss</link>
      <description><![CDATA[<p>黄仁勋出席大会。</p><script>bad()</script>]]></description>
      <pubDate>Thu, 02 Jul 2026 08:00:00 GMT</pubDate>
    </item></channel></rss>"""
    items = parse_feed(feed, "https://example.com/rss")
    assert len(items) == 1
    assert items[0]["canonical_url"] == "https://example.com/a"
    assert "黄仁勋出席大会" in items[0]["content_text"]
    assert "bad" not in strip_html(items[0]["content_text"])


def test_private_url_is_rejected():
    with pytest.raises(ValueError, match="私有或保留网络"):
        ensure_safe_url("http://127.0.0.1/internal")


def test_same_organization_subdomains_and_alias_domains_are_allowed():
    allowed = {"people.com.cn", "xinhuanet.com", "news.cn"}
    assert _same_site_link("https://politics.people.com.cn/n1/2026/a.html", "https://www.people.com.cn", allowed)
    assert _same_site_link("https://www.news.cn/politics/a.htm", "https://www.xinhuanet.com", allowed)
    assert not _same_site_link("https://example.com/a.html", "https://www.people.com.cn", allowed)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return self.payload


def test_webfetch_page_uses_central_fetch_and_extract(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append({"url": request.full_url, "auth": request.headers.get("Authorization"), "body": json.loads(request.data)})
        if request.full_url.endswith("/v1/fetch"):
            return FakeResponse({
                "request_id": "req_1", "success": True, "final_url": "https://example.com/final",
                "status_code": 200, "strategy": "browser", "from_cache": False,
                "content_type": "text/html; charset=utf-8", "body": "<html><title>fallback</title></html>",
                "artifact_id": "art_1", "fetched_at": "2026-07-04T00:00:00Z", "attempts": [],
            })
        return FakeResponse({
            "request_id": "req_2", "adapter": "generic.article", "adapter_version": "1", "artifact_id": "art_1",
            "data": {"title": "集中提取标题", "content": "集中服务提取的正文，包含足够长度的公开事实内容。", "author": "记者", "date": "2026-07-04T08:00:00+08:00"},
        })

    monkeypatch.setenv("TEST_WEBFETCH_KEY", "secret-value")
    monkeypatch.setattr("app.backend.collectors.urllib.request.urlopen", fake_urlopen)
    source = {"type": "web_page", "entry_url": "https://example.com/start", "name": "示例"}
    config = {
        "provider": "webfetch", "webfetch_base_url": "http://webfetch.internal:33333",
        "webfetch_api_key_env": "TEST_WEBFETCH_KEY", "timeout_seconds": 10,
    }
    documents = collect_source(source, config, 5)
    assert documents[0]["title"] == "集中提取标题"
    assert documents[0]["content_text"] == "集中服务提取的正文，包含足够长度的公开事实内容。"
    assert documents[0]["canonical_url"] == "https://example.com/final"
    assert documents[0]["fetch_metadata"]["artifact_id"] == "art_1"
    assert calls[0]["body"]["mode"] == "auto"
    assert calls[0]["body"]["save_artifact"] is True
    assert calls[0]["auth"] == "Bearer secret-value"
    assert calls[1]["body"]["artifact_id"] == "art_1"


def test_webfetch_rss_fetches_centrally_and_parses_locally(monkeypatch):
    feed = "<rss><channel><item><title>新闻</title><link>https://example.com/n</link><description>黄仁勋出席活动</description></item></channel></rss>"
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data))
        return FakeResponse({
            "request_id": "req_rss", "success": True, "final_url": "https://example.com/rss",
            "status_code": 200, "strategy": "http", "content_type": "application/rss+xml", "body": feed,
            "artifact_id": None, "fetched_at": "2026-07-04T00:00:00Z", "attempts": [],
        })

    monkeypatch.setenv("TEST_WEBFETCH_KEY", "secret-value")
    monkeypatch.setattr("app.backend.collectors.urllib.request.urlopen", fake_urlopen)
    documents = collect_source(
        {"type": "rss", "entry_url": "https://example.com/rss", "name": "RSS"},
        {"provider": "webfetch", "webfetch_base_url": "http://service", "webfetch_api_key_env": "TEST_WEBFETCH_KEY"},
        10,
    )
    assert captured["mode"] == "http"
    assert captured["save_artifact"] is False
    assert documents[0]["title"] == "新闻"
    assert documents[0]["fetch_metadata"]["provider"] == "webfetch"


def test_webfetch_does_not_silently_direct_fallback(monkeypatch):
    monkeypatch.setenv("TEST_WEBFETCH_KEY", "secret-value")
    monkeypatch.setattr(
        "app.backend.collectors.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    with pytest.raises(ValueError, match="集中抓取服务不可用"):
        collect_source(
            {"type": "web_page", "entry_url": "https://example.com", "name": "示例"},
            {
                "provider": "webfetch", "webfetch_base_url": "http://service",
                "webfetch_api_key_env": "TEST_WEBFETCH_KEY", "direct_fallback": False,
            },
            1,
        )


def test_website_discovery_filters_same_site_links_by_person(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data)
        calls.append((request.full_url, payload))
        if request.full_url.endswith("/v1/fetch"):
            if payload["url"].endswith("/news/he-lifeng.html"):
                return FakeResponse({
                    "request_id": "req_article", "success": True, "final_url": payload["url"],
                    "status_code": 200, "strategy": "http", "content_type": "text/html",
                    "body": "<article>何立峰出席会议</article>", "artifact_id": "art_article",
                    "fetched_at": "2026-07-04T00:00:00Z", "attempts": [],
                })
            return FakeResponse({
                "request_id": "req_root", "success": True, "final_url": "https://gov.example/",
                "status_code": 200, "strategy": "http", "content_type": "text/html", "body": "<html></html>",
                "artifact_id": "art_root", "fetched_at": "2026-07-04T00:00:00Z", "attempts": [],
            })
        if payload.get("adapter") == "generic.links":
            return FakeResponse({"data": {"links": [
                {"text": "何立峰出席经济会议", "href": "/news/he-lifeng.html"},
                {"text": "无关新闻", "href": "/news/other.html"},
                {"text": "何立峰外站转载", "href": "https://other.example/he.html"},
            ]}})
        return FakeResponse({"data": {
            "title": "何立峰出席经济会议", "content": "7月4日，何立峰出席经济会议并讲话。",
            "author": "记者", "date": "2026-07-04T08:00:00+08:00",
        }})

    monkeypatch.setenv("TEST_WEBFETCH_KEY", "secret-value")
    monkeypatch.setattr("app.backend.collectors.urllib.request.urlopen", fake_urlopen)
    documents = collect_source(
        {
            "type": "web_page", "entry_url": "https://gov.example/", "name": "政府网站",
            "parser_config": json.dumps({"discovery_enabled": True, "discovery_max_pages": 5, "discovery_max_depth": 1}),
            "discovery_terms": ["何立峰", "He Lifeng"],
        },
        {"provider": "webfetch", "webfetch_base_url": "http://service", "webfetch_api_key_env": "TEST_WEBFETCH_KEY"},
        10,
    )
    assert len(documents) == 1
    assert documents[0]["title"] == "何立峰出席经济会议"
    assert documents[0]["fetch_metadata"]["discovered_from"] == "https://gov.example/"
    fetched_urls = [payload["url"] for path, payload in calls if path.endswith("/v1/fetch")]
    assert "https://gov.example/news/he-lifeng.html" in fetched_urls
    assert "https://gov.example/news/other.html" not in fetched_urls
    assert all("other.example" not in url for url in fetched_urls)


def test_discovery_does_not_fetch_navigation_link_that_mentions_person(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data)
        calls.append((request.full_url, payload))
        if request.full_url.endswith("/v1/fetch"):
            return FakeResponse({
                "request_id": "req_root", "success": True, "final_url": payload["url"],
                "status_code": 200, "strategy": "http", "content_type": "text/html",
                "body": "<html></html>", "artifact_id": "art_root",
                "fetched_at": "2026-07-08T00:00:00Z", "attempts": [],
            })
        if payload.get("adapter") == "generic.links":
            return FakeResponse({"data": {"links": [
                {"text": "习近平党建思想专题首页", "href": "/politics/xxjxs/"},
            ]}})
        raise AssertionError("导航页不应进入文章正文提取")

    monkeypatch.setenv("TEST_WEBFETCH_KEY", "secret-value")
    monkeypatch.setattr("app.backend.collectors.urllib.request.urlopen", fake_urlopen)
    source = {
        "type": "web_page", "entry_url": "https://gov.example/", "name": "政府网站",
        "parser_config": json.dumps({"discovery_enabled": True, "discovery_max_pages": 1, "discovery_max_depth": 0}),
        "discovery_terms": ["习近平"],
    }
    documents = collect_source(
        source,
        {"provider": "webfetch", "webfetch_base_url": "http://service", "webfetch_api_key_env": "TEST_WEBFETCH_KEY"},
        10,
    )

    assert documents == []
    fetched_urls = [payload["url"] for path, payload in calls if path.endswith("/v1/fetch")]
    assert fetched_urls == ["https://gov.example/"]


def test_rejects_flattened_portal_page_returned_as_article():
    title = (
        "新华网首页 专题首页 最新播报 评论解读 智库报告 "
        "习近平党建思想的时代特质与世界意义 智库报告发布"
    )
    document = {
        "canonical_url": "https://www.news.cn/politics/xxjxs/",
        "title": title,
        "published_at": None,
        "content_text": (title + " 学习进行时 权威速览 要点海报 学习快评 ") * 5,
    }

    assert _article_rejection_reason(document)


def test_chinadaily_article_removes_homepage_prefix_and_recommendations():
    title = "万山磅礴看主峰｜习近平谈亚太合作"
    content = (
        "中国日报网首页 China Daily 首页 时政 资讯 国际 财经 "
        + title + " " + title
        + " 2026年7月20日，习近平表示，亚太各方应加强开放合作。这是文章的第二段公开内容。"
        + " 推荐阅读 王毅会见某国外长 编辑：测试 版权声明"
    )
    cleaned = clean_article_content(
        "https://cn.chinadaily.com.cn/a/202607/20/WS123456.html", title, content,
    )
    assert cleaned.startswith(title)
    assert cleaned.count(title) == 1
    assert "中国日报网首页" not in cleaned
    assert "推荐阅读" not in cleaned
    assert "习近平表示" in cleaned


def test_chinadaily_article_removes_flattened_toolbar_between_deck_and_body():
    title = "万山磅礴看主峰｜习近平谈亚太合作"
    deck = "多年来，习近平主席提出一系列倡议与主张。"
    content = (
        title + " " + deck
        + " 站内搜索 搜狗搜索 中国搜索 订阅 移动 English 首页 时政 时政要闻 "
        + "评论频道 理论频道 学习时代 两岸频道 资讯 独家 中国日报专稿 "
        + "传媒动态 每日一词 法院公告 C财经 科技 视频 专栏 漫画 观天下 "
        + "地方 文创 文旅 文化知识库 中国有约 China Daily Homepage 中文网首页 "
        + "关闭 中国日报网 > 要闻 > " + title
        + " 来源：公开来源 2026-07-25 09:18 分享到微信 "
        + deck + "这是文章正文第二段，介绍亚太合作的公开信息。 【"
    )

    cleaned = clean_article_content(
        "https://cn.chinadaily.com.cn/a/202607/25/WS123456.html", title, content,
    )

    assert cleaned == title + " " + deck + " " + deck + "这是文章正文第二段，介绍亚太合作的公开信息。"
    assert "站内搜索" not in cleaned
    assert "中国日报网 >" not in cleaned
    assert not _article_rejection_reason({
        "canonical_url": "https://cn.chinadaily.com.cn/a/202607/25/WS123456.html",
        "title": title,
        "published_at": "2026-07-25T09:18:00+08:00",
        "content_text": cleaned,
    })


def test_chinadaily_quality_rejects_channels_but_not_other_site_reprints():
    channel = {
        "canonical_url": "https://cn.chinadaily.com.cn/china/",
        "title": "中国日报网首页 China Daily 首页 时政 资讯 国际",
        "published_at": None,
        "content_text": "中国日报网首页 China Daily 首页 时政 资讯 国际 财经 文化 图片 视频 " * 4,
    }
    assert _article_rejection_reason(channel)
    reprint = clean_article_content(
        "https://example.com/reprint.html",
        "转载中国日报：公开合作消息",
        "转载中国日报：公开合作消息 正文第一段内容完整。正文第二段继续介绍公开事实。",
    )
    assert reprint.startswith("转载中国日报")


def test_chinadaily_photo_story_allows_title_phrase_in_multiple_captions():
    title = "习近平在上海考察"
    content = " ".join([
        title,
        "7月15日，习近平在上海考察。这是习近平在居民区考察时同工作人员交流。新华社记者甲摄。",
        "7月15日，习近平在上海考察。这是习近平来到居民家中了解情况。新华社记者乙摄。",
        "7月15日，习近平在上海考察。这是习近平同居民代表亲切交流。新华社记者丙摄。",
    ])
    assert not _article_rejection_reason({
        "canonical_url": "https://cn.chinadaily.com.cn/a/202607/15/WS123456.html",
        "title": title,
        "published_at": "2026-07-15T18:00:00+08:00",
        "content_text": content,
    })
