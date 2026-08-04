import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("push", ROOT / "src" / "push.py")
push = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["push"] = push
SPEC.loader.exec_module(push)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    def test_html_anchor_parsing(self):
        raw = """
        <ul><li><a href="/cp/a20240906main/newsdetail.html?id=123456">
        7月30日更新公告丨免费领取三角券</a><span>2026-07-29</span></li></ul>
        """
        items = push.parse_html_articles(raw, self.config["source_url"])
        self.assertEqual(items[0].article_id, "123456")
        self.assertIn("更新公告", items[0].title)
        self.assertEqual(items[0].published, "2026-07-29")

    def test_markdown_parsing(self):
        raw = """
        2026-07-29
        [7月30日更新公告丨2600限时三角券免费送](https://df.qq.com/cp/a20240906main/newsdetail.html?id=9988)
        """
        items = push.parse_markdown_articles(raw, self.config["source_url"])
        self.assertEqual(items[0].article_id, "9988")
        self.assertEqual(items[0].published, "2026-07-29")

    def test_cmc_payload(self):
        payload = {
            "data": {
                "list": [
                    {
                        "iDocID": "8877",
                        "sTitle": "异常问题修复公告",
                        "sCreated": "2026-08-01 10:00:00",
                    }
                ]
            }
        }
        items = push.parse_cmc_payload(payload, self.config["source_url"])
        self.assertEqual(items[0].article_id, "8877")
        self.assertEqual(items[0].published, "2026-08-01")

    def test_filter_esports(self):
        article = push.Article(
            article_id="1",
            title="2026烽火职业联赛夏季赛正式开赛",
            url="https://df.qq.com/cp/a20240906main/newsdetail.html?id=1",
        )
        should_push, category = push.classify_article(article, self.config)
        self.assertFalse(should_push)
        self.assertEqual(category, "赛事")

    def test_filter_event(self):
        article = push.Article(
            article_id="2",
            title="宝藏月活动开启，免费领取三角券",
            url="https://df.qq.com/cp/a20240906main/newsdetail.html?id=2",
        )
        should_push, category = push.classify_article(article, self.config)
        self.assertTrue(should_push)
        self.assertEqual(category, "活动资讯")

    def test_filter_collaboration_event(self):
        article = push.Article(
            article_id="3",
            title="三角洲行动 × RAZER 联名外设系列正式登场",
            url="https://df.qq.com/cp/a20240906main/newsdetail.html?id=3",
        )
        should_push, category = push.classify_article(article, self.config)
        self.assertTrue(should_push)
        self.assertEqual(category, "活动资讯")

    def test_filter_creator_event(self):
        article = push.Article(
            article_id="4",
            title="三角洲行动共创大赛正式开启",
            url="https://df.qq.com/cp/a20240906main/newsdetail.html?id=4",
        )
        should_push, category = push.classify_article(article, self.config)
        self.assertTrue(should_push)
        self.assertEqual(category, "活动资讯")

    def test_dynamic_detail_path_is_preserved(self):
        normalized = push.normalize_detail_url(
            "https://df.qq.com/cp/a20990101main/newsdetail.html?id=42",
            self.config["source_url"],
        )
        self.assertEqual(
            normalized,
            ("42", "https://df.qq.com/cp/a20990101main/newsdetail.html?id=42"),
        )
        self.assertIsNone(
            push.normalize_detail_url(
                "https://evil.example/newsdetail.html?id=42",
                self.config["source_url"],
            )
        )

    def test_short_date_uses_previous_year_near_new_year(self):
        reference = datetime(2026, 1, 2, 12, 0, tzinfo=push.BEIJING)
        self.assertEqual(
            push.normalize_date("12-31", reference=reference), "2025-12-31"
        )
        self.assertEqual(
            push.normalize_date("01-02", reference=reference), "2026-01-02"
        )

    def test_milo_payload_ignores_nested_generic_ids(self):
        payload = {
            "jData": {
                "data": {
                    "items": [
                        {
                            "iDocID": "998877",
                            "sTitle": "安全治理公告",
                            "sCreated": "2026-08-04 18:03:14",
                            "sChannelInfo": "6895|最新,6896|公告",
                            "sCoverList": [
                                {"id": 1},
                                {"id": 2, "iDocID": "fake-nested-id"},
                            ],
                        }
                    ]
                }
            }
        }
        items = push.parse_cmc_payload(payload, self.config["source_url"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].article_id, "998877")
        self.assertEqual(items[0].category, "公告")
        self.assertEqual(items[0].published, "2026-08-04")

    def test_source_category_can_promote_important_announcement(self):
        article = push.Article(
            article_id="5",
            title="重拳治挂，铁腕断链！",
            url="https://df.qq.com/cp/a20240906main/newsdetail.html?id=5",
            category="公告",
        )
        should_push, category = push.classify_article(article, self.config)
        self.assertTrue(should_push)
        self.assertEqual(category, "官方公告")

    def test_source_freshness_fails_closed(self):
        reference = datetime(2026, 8, 4, 12, 0, tzinfo=push.BEIJING)
        fresh = [
            push.Article("1", "公告", "https://df.qq.com/x", published="2026-08-04")
        ]
        push.assert_source_freshness(fresh, max_age_days=45, reference=reference)
        stale = [
            push.Article("2", "旧公告", "https://df.qq.com/y", published="2025-09-28")
        ]
        with self.assertRaisesRegex(RuntimeError, "静态缓存"):
            push.assert_source_freshness(stale, max_age_days=45, reference=reference)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    @staticmethod
    def response(payload, status=200):
        response = push.requests.Response()
        response.status_code = status
        response._content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response.headers["content-type"] = "application/json; charset=utf-8"
        return response

    def test_discover_milo_flow(self):
        description = {
            "alias": {"d46289": "1073310"},
            "flows": {"f_1073310": {"mapid": 329849}},
            "ide": {
                "sIdeUrl": "comm.ams.game.qq.com/ide/",
                "flows": {"329849": {"sIdeToken": "o3Iw53"}},
            },
        }
        session = mock.Mock()
        session.request.return_value = self.response(description)
        endpoint, chart_id, token = push.discover_milo_flow(session, self.config)
        self.assertEqual(endpoint, "https://comm.ams.game.qq.com/ide/")
        self.assertEqual(chart_id, "329849")
        self.assertEqual(token, "o3Iw53")

    def test_milo_fetch_paginates_at_fifty(self):
        def item(number):
            return {
                "iDocID": str(10_000 + number),
                "sTitle": f"第 {number} 条公告",
                "sCreated": "2026-08-04 12:00:00",
                "sChannelInfo": "6895|最新,6896|公告",
            }

        first = {
            "iRet": 0,
            "jData": {"data": {"total": 420, "items": [item(i) for i in range(50)]}},
        }
        second = {
            "iRet": 0,
            "jData": {
                "data": {"total": 420, "items": [item(i) for i in range(50, 60)]}
            },
        }
        session = mock.Mock()
        session.request.side_effect = [self.response(first), self.response(second)]
        with mock.patch.object(
            push,
            "discover_milo_flow",
            return_value=("https://comm.ams.game.qq.com/ide/", "329849", "o3Iw53"),
        ):
            items = push.fetch_via_milo(session, self.config)
        self.assertEqual(len(items), 60)
        starts = [
            call.kwargs["data"]["start"] for call in session.request.call_args_list
        ]
        self.assertEqual(starts, ["0", "50"])

    def test_milo_invalid_structure_fails(self):
        session = mock.Mock()
        session.request.return_value = self.response({"iRet": 0, "jData": {}})
        with (
            mock.patch.object(
                push,
                "discover_milo_flow",
                return_value=("https://comm.ams.game.qq.com/ide/", "329849", "o3Iw53"),
            ),
            self.assertRaisesRegex(RuntimeError, "结构无效"),
        ):
            push.fetch_via_milo(session, self.config)

    def test_webhook_network_error_never_exposes_token(self):
        webhook = "https://discord.com/api/webhooks/123456/super-secret-token"
        failure = push.requests.ConnectionError(
            f"connection failed for {push.webhook_wait_url(webhook)}"
        )
        with (
            mock.patch.object(push.requests, "post", side_effect=failure),
            mock.patch.object(push.time, "sleep"),
            self.assertRaises(RuntimeError) as raised,
        ):
            push.send_webhook(webhook, {"content": "test"})
        message = str(raised.exception)
        self.assertNotIn("super-secret-token", message)
        self.assertNotIn("123456", message)
        self.assertNotIn(
            "relative-token",
            push.redact_webhook_text("failed /api/webhooks/99/relative-token"),
        )

    def test_webhook_honors_retry_after(self):
        limited = self.response({"retry_after": 2.5}, status=429)
        accepted = self.response({}, status=204)
        with (
            mock.patch.object(push.requests, "post", side_effect=[limited, accepted]),
            mock.patch.object(push.time, "sleep") as sleeper,
        ):
            push.send_webhook(
                "https://discord.com/api/webhooks/123456/token", {"content": "test"}
            )
        sleeper.assert_called_once_with(2.75)

    def test_webhook_uses_reset_after_fallback_header(self):
        limited = self.response("not-json", status=429)
        limited._content = b"not-json"
        limited.headers["X-RateLimit-Reset-After"] = "9.0"
        accepted = self.response({}, status=204)
        with (
            mock.patch.object(push.requests, "post", side_effect=[limited, accepted]),
            mock.patch.object(push.time, "sleep") as sleeper,
        ):
            push.send_webhook(
                "https://discord.com/api/webhooks/123456/token", {"content": "test"}
            )
        sleeper.assert_called_once_with(9.25)

    def test_old_state_is_rebaselined_when_source_changes(self):
        articles = [
            push.Article(
                "new-1",
                "最新公告",
                "https://df.qq.com/cp/a20240906main/newsdetail.html?id=1001",
                published="2026-08-04",
            )
        ]
        state = {
            "version": 1,
            "initialized": True,
            "seen": ["old-static-id"],
            "last_keepalive_month": "2026-07",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            with (
                mock.patch.object(push, "STATE_PATH", state_path),
                mock.patch.object(push, "send_webhook") as sender,
            ):
                push.run_normal(
                    "https://discord.com/api/webhooks/1/token",
                    articles,
                    self.config,
                    state,
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["source_version"], "milo-v1")
        self.assertIn("new-1", saved["seen"])
        sender.assert_called_once()

    def test_runtime_budget_defers_unsent_items(self):
        articles = [
            push.Article(
                "new-2",
                "8月4日更新公告",
                "https://df.qq.com/cp/a20240906main/newsdetail.html?id=1002",
                published="2026-08-04",
            )
        ]
        state = {
            "version": 2,
            "initialized": True,
            "source_version": "milo-v1",
            "seen": [],
            "last_keepalive_month": "2026-08",
        }
        with (
            mock.patch.object(push, "send_webhook") as sender,
            mock.patch.object(push, "save_state") as saver,
            self.assertLogs(push.LOGGER, level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "时间预算不足"),
        ):
            push.run_normal(
                "https://discord.com/api/webhooks/1/token",
                articles,
                self.config,
                state,
                deadline=push.time.monotonic() + 1,
            )
        sender.assert_not_called()
        saver.assert_not_called()
        self.assertEqual(state["seen"], [])


if __name__ == "__main__":
    unittest.main()
