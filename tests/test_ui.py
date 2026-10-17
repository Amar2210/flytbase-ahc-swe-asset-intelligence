"""Exercise the real UI and copilot against an isolated copy; no network calls."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def _app_test_temp_directory(*_args, **_kwargs):
    # AppTest allocates a module-level TemporaryDirectory during import. Use a
    # normal inherited-ACL mkdir for its scratch path on this Windows workspace;
    # the existing TemporaryDirectory finalizer can then clean it normally.
    path = ROOT / "tests" / "data" / ("streamlit-test-" + uuid4().hex)
    path.mkdir(parents=True)
    return str(path)


with patch("tempfile.mkdtemp", side_effect=_app_test_temp_directory):
    from streamlit.testing.v1 import AppTest

from copilot.config import Settings
from copilot.db import connect
from copilot.ingest import import_dataset
from copilot.model import ModelError, ModelReply
from copilot.previews import PREVIEW_LABELS, build_preview


NOTE_TEXT = "UI integration check: emergency maintenance reported by crew."
MICHAEL_TEMPERATURE_CORRECTION = (
    "Please correct inspection INS-0164-0205 for WELL-0164, dated 15 October 2026. "
    "The recorded wellhead temperature of 24.0°C was wrong because the handheld thermal gun was faulty. "
    "The field crew re-measured it at 41.0°C. Save 41.0°C as the corrected wellhead temperature, "
    "preserving the original reading and attributing this correction to me. Ambient temperature remains 19.9°C. "
    "Keep Tyler Hughes’s “Pump jack not moving” note and flag its disagreement with the corrected reading."
)


class ScriptedGemma:
    """Only the provider boundary is fake; tools, memory and persistence are real."""

    connection_calls = []

    def __init__(self, *args, **kwargs):
        self.round = 0
        self.connection_calls.append(args[:3])

    def complete(self, messages, definitions):
        self.round += 1
        prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        if self.round == 1:
            if "FYI" in prompt:
                name = "add_shared_note"
                arguments = {
                    "asset_id": "WELL-0001", "text": NOTE_TEXT,
                    "valid_from": "2026-10-17", "valid_to": "2026-10-20",
                    "note_type": "operator_note",
                }
            elif "Please correct" in prompt:
                name = "correct_inspection"
                arguments = {
                    "inspection_id": "INS-0001-0070", "field": "spill_detected",
                    "value": False, "reason": "Qualified crew verified that this detection was rainwater.",
                }
            elif "discussed yesterday" in prompt:
                name, arguments = "search_my_conversations", {"period": "yesterday", "role": "user"}
            else:
                name, arguments = "get_well", {"asset_id": "WELL-0164" if "WELL-0164" in prompt else "WELL-0001"}
            call = {"id": "ui-test-call", "name": name, "arguments": arguments}
            return ModelReply(calls=[call], assistant_message={"role": "assistant", "content": ""})
        tool_messages = [item for item in messages if item["role"] == "tool"]
        if tool_messages and tool_messages[-1].get("name") == "search_my_conversations":
            result = json.loads(tool_messages[-1]["content"])["result"]
            recalled = "; ".join(item["content"] for item in result["messages"])
            return ModelReply(content=json.dumps({"answer": recalled + (" [S1]" if recalled else "No earlier private discussion found."), "visuals": []}))
        status_well = "WELL-0164" if "WELL-0164" in prompt else "WELL-0001"
        return ModelReply(content=json.dumps({
            "answer": f"{status_well} status is based on its latest available inspection. [S1]",
            "visuals": [{"kind": "status", "source_tool": "T1", "title": "Inspection status"}],
        }))


class TelemetryGemma(ScriptedGemma):
    """Explicitly synthetic telemetry to test UI rendering, never token accuracy."""

    preflight_notifications = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_metrics = []
        self.metrics_callback = None

    def complete(self, messages, definitions):
        record = {"provider": "google", "model": "gemma-test-fixture", "input_tokens": 1000 + 500 * len(self.request_metrics),
                  "input_token_source": "countTokens", "measured": True, "output_tokens": None,
                  "total_tokens": None, "status": "pending"}
        self.request_metrics.append(record)
        if callable(self.metrics_callback):
            self.preflight_notifications.append(record["input_tokens"])
            self.metrics_callback(self.request_metrics)
        reply = super().complete(messages, definitions)
        record.update(status="completed", output_tokens=20, total_tokens=record["input_tokens"] + 20)
        if callable(self.metrics_callback):
            self.metrics_callback(self.request_metrics)
        return reply


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = ROOT / "data" / ("ui-test-" + uuid4().hex + ".sqlite3")
        cls.database.parent.mkdir(parents=True, exist_ok=True)
        existing = ROOT / "data/copilot.sqlite3"
        if existing.is_file():
            # SQLite's backup API also handles a source with an active WAL safely.
            with closing(connect(existing)) as source, closing(connect(cls.database)) as destination:
                source.backup(destination)
        else:
            import_dataset(ROOT / "challenge-dataset/sample-data", cls.database, "Sample")
        with closing(connect(cls.database)) as connection:
            cls.dataset_id = connection.execute("SELECT dataset_id FROM datasets ORDER BY imported_at LIMIT 1").fetchone()[0]
            cls.users = [row[0] for row in connection.execute("SELECT user_id FROM users WHERE dataset_id=? ORDER BY name", (cls.dataset_id,))]

    @classmethod
    def tearDownClass(cls):
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(cls.database) + suffix).unlink(missing_ok=True)

    def setUp(self):
        with closing(connect(self.database)) as connection:
            connection.execute("DELETE FROM chat_messages")
            connection.execute("DELETE FROM corrections")
            connection.execute("DELETE FROM context_notes WHERE origin='runtime'")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='pending_updates'").fetchone():
                connection.execute("DELETE FROM pending_updates")
            connection.commit()
        self.settings = Settings(db_path=str(self.database), api_key="ui-test-placeholder")
        ScriptedGemma.connection_calls = []
        self.configuration = patch("copilot.config.Settings.from_env", return_value=self.settings)
        self.provider = patch("copilot.agent.GemmaClient", ScriptedGemma)
        self.network = patch("copilot.model.urlopen", side_effect=AssertionError("UI tests must never access the network"))
        self.config_mock = self.configuration.start()
        self.provider.start()
        self.network_mock = self.network.start()
        self.addCleanup(self.configuration.stop)
        self.addCleanup(self.provider.stop)
        self.addCleanup(self.network.stop)

    def start_app(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
        self.assertEqual(list(app.exception), [])
        return app

    @staticmethod
    def select(app, label):
        return next(item for item in app.selectbox if item.label == label)

    @staticmethod
    def text(app, label):
        return next(item for item in app.text_input if item.label == label)

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    def test_data_browser_import_only_sidebar_and_no_key_state(self):
        self.config_mock.return_value = replace(self.settings, api_key="")
        app = self.start_app()
        self.assertEqual(len(self.select(app, "User / crew member").options), 15)
        self.assertTrue(any("Active user:" in item.value for item in app.caption))
        self.assertEqual(next(item.value for item in app.metric if item.label == "Wells"), "200")
        self.assertEqual([item.label for item in app.sidebar.expander], ["Import dataset"])
        self.assertEqual([item.label for item in app.sidebar.selectbox], ["Dataset"])
        self.assertEqual([item.label for item in app.sidebar.text_input], ["Dataset label", "Local dataset folder or ZIP"])
        self.assertEqual([item.label for item in app.sidebar.button], ["Import ZIP", "Import local data"])
        self.assertFalse(any(item.label in {"API key", "Model ID", "Field timezone"} for item in app.text_input))
        self.assertFalse(any(item.label == "Provider" for item in app.selectbox))
        self.assertEqual(len(app.date_input), 0)
        self.select(app, "Records").select("context_notes").run()
        self.text(app, "Filter by well, record ID, or name").input("WELL-0001").run()
        self.assertEqual(len(app.dataframe[0].value), 3)
        app.chat_input[0].set_value("Show WELL-0001").run()
        self.assertTrue(any(".env" in item.value for item in app.warning))
        self.assertEqual(ScriptedGemma.connection_calls, [])
        self.assertEqual(list(app.exception), [])

    def test_status_cards_and_private_conversation_switching(self):
        self.config_mock.return_value = replace(self.settings, provider="openrouter", model="google/gemma-4-31b-it:free")
        app = self.start_app()
        self.select(app, "User / crew member").select(self.users[0]).run()
        app.chat_input[0].set_value("Show WELL-0001 status").run()
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(ScriptedGemma.connection_calls[-1], ("ui-test-placeholder", "openrouter", "google/gemma-4-31b-it:free"))
        self.assertTrue(any(item.label == "Inspection-based status" for item in app.metric))
        self.select(app, "User / crew member").select(self.users[1]).run()
        self.assertEqual(len(app.chat_message), 0)
        self.select(app, "User / crew member").select(self.users[0]).run()
        self.assertEqual(len(app.chat_message), 2)
        with closing(connect(self.database)) as connection:
            session_id = connection.execute("SELECT session_id FROM chat_messages WHERE user_id=? AND role='user'", (self.users[0],)).fetchone()[0]
        self.button(app, "＋ New conversation").click().run()
        self.assertEqual(len(app.chat_message), 0)
        self.select(app, "Conversation").select(session_id).run()
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(list(app.exception), [])

    def test_visual_previews_render_real_data_without_model_or_key(self):
        self.config_mock.return_value = replace(self.settings, api_key="")
        with patch("copilot.previews.build_preview", wraps=build_preview) as preview_query:
            app = self.start_app()
            self.assertIn("Visual preview", [tab.label for tab in app.tabs])
            preview_query.assert_not_called()
            for index, kind in enumerate(PREVIEW_LABELS):
                self.select(app, "Preview").select(kind).run()
                self.assertEqual(preview_query.call_count, index)
                self.button(app, "Show preview").click().run()
                self.assertEqual(preview_query.call_count, index + 1)
                self.assertEqual(list(app.exception), [])
                self.assertFalse(any("could not be displayed" in item.value for item in app.caption))
                self.assertTrue(any("Database preview: no model API calls" in item.value for item in app.markdown))
                app.run()
                self.assertEqual(preview_query.call_count, index + 1)
            self.assertTrue(any(item.label == "User / crew member" for item in app.selectbox))
            self.assertEqual([item.label for item in app.sidebar.expander], ["Import dataset"])
            self.assertEqual(ScriptedGemma.connection_calls, [])
            self.network_mock.assert_not_called()
            with closing(connect(self.database)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM chat_messages").fetchone()[0], 0)

    def test_context_token_metrics_persist_and_display_each_inference_round(self):
        # Counts here are deliberately synthetic fixtures, not live measurements.
        TelemetryGemma.preflight_notifications = []
        with patch("copilot.agent.GemmaClient", TelemetryGemma):
            app = self.start_app()
            self.assertFalse(any(item.label == "Latest input context" for item in app.metric))
            app.chat_input[0].set_value("Show WELL-0001 status").run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(TelemetryGemma.preflight_notifications, [1000, 1500])
            self.assertEqual(next(item.value for item in app.metric if item.label == "Latest input context"), "1,500 tokens")
            self.assertEqual(next(item.value for item in app.metric if item.label == "Peak measured context"), "1,500 tokens")
            self.assertEqual(next(item.value for item in app.metric if item.label == "Model rounds"), "2")
            self.assertEqual(next(item.value for item in app.metric if item.label == "Input across rounds"), "2,500 tokens")
            self.assertTrue(any(item.label == "Context token details" for item in app.expander))
            self.assertTrue(any("SOP text is included when retrieved" in item.value for item in app.caption))
            with closing(connect(self.database)) as connection:
                payload = json.loads(connection.execute("SELECT payload_json FROM chat_messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
            self.assertEqual([item["input_tokens"] for item in payload["token_usage"]], [1000, 1500])
            app.run()
            self.assertEqual(next(item.value for item in app.metric if item.label == "Latest input context"), "1,500 tokens")
            self.network_mock.assert_not_called()

    def test_shared_token_budget_wait_is_visible_without_provider_call(self):
        class QueuedTelemetry(TelemetryGemma):
            def complete(self, messages, definitions):
                self.request_metrics.append({
                    "provider": "google", "model": "gemma-test-fixture", "input_tokens": 6000,
                    "input_token_source": "countTokens", "status": "queued",
                    "rate_limit_status": "queued", "rate_limit_wait_seconds": 12,
                    "rate_limit_reserved_tokens": 6664, "rate_limit_window_reserved_tokens": 10000,
                    "rate_limit_budget_tokens": 14500, "rate_limit_limit_tokens": 16000,
                    "inference_http_attempts": 0,
                })
                if callable(self.metrics_callback):
                    self.metrics_callback(self.request_metrics)
                raise ModelError("Synthetic local queue timeout; no provider request was sent.")

        with patch("copilot.agent.GemmaClient", QueuedTelemetry):
            app = self.start_app()
            app.chat_input[0].set_value("Show WELL-0001 status").run()
            self.assertEqual(list(app.exception), [])
            self.assertTrue(any("Waiting for the local Google TPM budget" in item.value for item in app.info))
            self.assertTrue(any("14,500 of 16,000" in item.value for item in app.caption))
            self.assertTrue(any("cannot see AI Studio" in item.value for item in app.caption))
            self.network_mock.assert_not_called()

    def test_unavailable_token_count_is_not_displayed_as_zero(self):
        class UnavailableTelemetry(TelemetryGemma):
            def complete(self, messages, definitions):
                self.request_metrics.append({"provider": "google", "model": "gemma-test-fixture",
                                             "input_tokens": None, "input_token_source": "unavailable", "measured": False,
                                             "output_tokens": None, "total_tokens": None, "status": "failed",
                                             "count_error": "Token counting unavailable (HTTP 429)."})
                if callable(self.metrics_callback):
                    self.metrics_callback(self.request_metrics)
                raise ModelError("Synthetic quota failure", status_code=429)

        with patch("copilot.agent.GemmaClient", UnavailableTelemetry):
            app = self.start_app()
            app.chat_input[0].set_value("Show WELL-0001 status").run()
            self.assertEqual(list(app.exception), [])
            self.assertEqual(next(item.value for item in app.metric if item.label == "Latest input context"), "Unavailable")
            self.assertEqual(next(item.value for item in app.metric if item.label == "Peak measured context"), "Unavailable")
            self.assertTrue(any("Token counting unavailable (HTTP 429)" in item.value for item in app.caption))
            self.network_mock.assert_not_called()

    def test_review_confirm_shared_note_and_cancel_correction(self):
        app = self.start_app()
        app.chat_input[0].set_value("FYI WELL-0001 is under emergency maintenance from 2026-10-17 through 2026-10-20.").run()
        self.assertTrue(any("Ends (inclusive)" in item.value for item in app.markdown))
        self.assertTrue(any(NOTE_TEXT in item.value for item in app.markdown))
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM context_notes WHERE origin='runtime'").fetchone()[0], 0)
        self.button(app, "Confirm shared update").click().run()
        with closing(connect(self.database)) as connection:
            saved = connection.execute("SELECT text, valid_to FROM context_notes WHERE origin='runtime'").fetchall()
            self.assertEqual([(row[0], row[1]) for row in saved], [(NOTE_TEXT, "2026-10-20")])
        self.select(app, "User / crew member").select(self.users[1]).run()
        self.assertEqual(len(app.chat_message), 0)
        self.assertEqual(next(item.value for item in app.metric if item.label == "Shared notes"), "142")
        self.select(app, "Records").select("context_notes").run()
        self.text(app, "Filter by well, record ID, or name").input(NOTE_TEXT).run()
        self.assertEqual(len(app.dataframe[0].value), 1)
        app.chat_input[0].set_value("Please correct inspection INS-0001-0070: qualified crew verified rainwater, spill_detected false.").run()
        self.assertTrue(any(item.label == "Original recorded value" and item.value == "1" for item in app.metric))
        self.assertTrue(any(item.label == "Proposed value" and item.value == "No" for item in app.metric))
        self.assertTrue(any("Feedback type: AI detection feedback" in item.value for item in app.caption))
        self.assertTrue(any("qualified" in item.value for item in app.info))
        self.button(app, "Cancel update").click().run()
        app.run()
        self.assertFalse(any(item.label == "Confirm shared update" for item in app.button))
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM corrections").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT status FROM pending_updates ORDER BY created_at DESC LIMIT 1").fetchone()[0], "cancelled")
        self.assertEqual(list(app.exception), [])

    def test_identity_switching_keeps_dated_history_private_across_sessions(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=12).isoformat()
        with closing(connect(self.database)) as connection, connection:
            for uid, content in ((self.users[0], "ALICE-PRIVATE discussed WELL-0001 and WELL-0002"),
                                 (self.users[1], "BOB-PRIVATE discussed WELL-0003")):
                connection.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,'user',?,?)",
                                   (self.dataset_id, uid, "older-" + uid, content, yesterday))
        app = self.start_app()
        self.select(app, "User / crew member").select(self.users[0]).run()
        self.button(app, "＋ New conversation").click().run()
        app.chat_input[0].set_value("Which were the wells we discussed yesterday?").run()
        displayed = " ".join(item.value for item in app.markdown)
        self.assertIn("ALICE-PRIVATE", displayed)
        self.assertNotIn("BOB-PRIVATE", displayed)
        self.assertTrue(any("runtime:private-chat" in item.value for item in app.caption))
        self.select(app, "User / crew member").select(self.users[1]).run()
        self.button(app, "＋ New conversation").click().run()
        app.chat_input[0].set_value("Which were the wells we discussed yesterday?").run()
        displayed = " ".join(item.value for item in app.markdown)
        self.assertIn("BOB-PRIVATE", displayed)
        self.assertNotIn("ALICE-PRIVATE", displayed)
        self.assertEqual(list(app.exception), [])

    def test_michael_temperature_correction_review_then_jessica_reads_shared_status(self):
        """The reported user workflow stages locally, writes only on confirm, then shares facts."""
        with closing(connect(self.database)) as connection:
            identities = dict(connection.execute("SELECT name,user_id FROM users WHERE dataset_id=?", (self.dataset_id,)))
        michael, jessica = identities["Michael Turner"], identities["Jessica Reed"]
        self.assertEqual(michael, "U-201")
        app = self.start_app()
        self.select(app, "User / crew member").select(michael).run()
        self.assertEqual(len(app.chat_message), 0)
        app.chat_input[0].set_value(MICHAEL_TEMPERATURE_CORRECTION).run()

        self.assertEqual(list(app.exception), [])
        self.assertTrue(any("Review shared update" in item.value for item in app.markdown))
        self.assertTrue(any("0 model calls" in item.value and "awaiting your confirmation" in item.value for item in app.caption))
        self.assertEqual(float(next(item.value for item in app.metric if item.label == "Original recorded value")), 24.0)
        self.assertEqual(float(next(item.value for item in app.metric if item.label == "Proposed value")), 41.0)
        self.assertEqual(ScriptedGemma.connection_calls, [], "Fully specified corrections must not instantiate a model client.")
        self.network_mock.assert_not_called()
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM corrections").fetchone()[0], 0)
            pending = connection.execute("SELECT tool_name,arguments_json,user_id,status FROM pending_updates WHERE status='pending'").fetchone()
            self.assertEqual((pending["tool_name"], pending["user_id"], pending["status"]), ("correct_inspection", michael, "pending"))
            arguments = json.loads(pending["arguments_json"])
            self.assertEqual((arguments["inspection_id"], arguments["field"], arguments["value"]),
                             ("INS-0164-0205", "wellhead_temp_c", 41.0))

        self.button(app, "Confirm shared update").click().run()
        app.run()
        self.assertEqual(list(app.exception), [])
        self.assertFalse(any(item.label == "Confirm shared update" for item in app.button),
                         [item.value for item in app.error])
        with closing(connect(self.database)) as connection:
            correction = connection.execute("SELECT * FROM corrections WHERE inspection_id='INS-0164-0205'").fetchone()
            self.assertEqual((correction["author_user_id"], correction["field"], correction["status"]),
                             (michael, "wellhead_temp_c", "accepted"))
            self.assertEqual(json.loads(correction["value_json"]), 41.0)
            original = connection.execute("SELECT wellhead_temp_c,ambient_temp_c,notes FROM inspections WHERE dataset_id=? AND inspection_id='INS-0164-0205'", (self.dataset_id,)).fetchone()
            self.assertEqual((original["wellhead_temp_c"], original["ambient_temp_c"]), (24.0, 19.9))
            self.assertIn("Pump jack not moving", original["notes"])
            note = connection.execute("SELECT author_user_id,inspection_id FROM context_notes WHERE note_id=?", (correction["source_note_id"],)).fetchone()
            self.assertEqual((note["author_user_id"], note["inspection_id"]), (michael, "INS-0164-0205"))
        self.assertEqual(ScriptedGemma.connection_calls, [])

        self.select(app, "User / crew member").select(jessica).run()
        self.assertEqual(len(app.chat_message), 0, "Jessica must not see Michael's private chat.")
        app.chat_input[0].set_value("Is WELL-0164 producing oil right now?").run()
        self.assertEqual(list(app.exception), [])
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(next(item.value for item in app.metric if item.label == "Inspection-based status"), "Producing")
        self.assertEqual(next(item.value for item in app.metric if item.label == "Wellhead − ambient"), "21.1 °C")
        self.assertEqual(len(ScriptedGemma.connection_calls), 1)
        self.assertFalse(any(MICHAEL_TEMPERATURE_CORRECTION in item.value for item in app.markdown))
        with closing(connect(self.database)) as connection:
            messages = connection.execute("SELECT user_id,role,content,payload_json FROM chat_messages ORDER BY message_id").fetchall()
            michael_prompts = [row["content"] for row in messages if row["user_id"] == michael and row["role"] == "user"]
            self.assertEqual(michael_prompts, [MICHAEL_TEMPERATURE_CORRECTION])
            jessica_messages = [row for row in messages if row["user_id"] == jessica]
            self.assertEqual([row["role"] for row in jessica_messages], ["user", "assistant"])
            payload = json.loads(jessica_messages[-1]["payload_json"])
            card = next(item for item in payload["visuals"] if item["kind"] == "status")["data"][0]
            self.assertEqual((card["asset_id"], card["is_producing"], card["temperature_delta_c"]), ("WELL-0164", 1, 21.1))
        self.network_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

