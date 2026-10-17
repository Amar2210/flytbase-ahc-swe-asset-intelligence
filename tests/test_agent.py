"""Offline orchestration checks against an isolated copy of real challenge data."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from copilot.agent import Copilot, INSTRUCTIONS, MAX_RECENT_HISTORY_CHARS, MAX_TOOL_CONTEXT_CHARS, _record_fallback
from copilot.config import Settings
from copilot.db import connect
from copilot.ingest import import_dataset
from copilot.model import GemmaClient, ModelError, ModelReply


class ScriptedClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, definitions):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(definitions)))
        if not self.replies:
            raise AssertionError("Unexpected extra model request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class MeteredClient(ScriptedClient):
    """Supply controlled provider accounting without any network request."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.request_metrics = []
        self.metrics_callback = None

    def complete(self, messages, definitions):
        metric = {"provider": "google", "model": "gemma-test", "input_tokens": 1000 + len(self.request_metrics) * 500,
                  "input_token_source": "countTokens", "measured": True,
                  "output_tokens": None, "total_tokens": None, "status": "pending"}
        self.request_metrics.append(metric)
        if self.metrics_callback:
            self.metrics_callback(copy.deepcopy(self.request_metrics))
        try:
            reply = super().complete(messages, definitions)
        except Exception:
            metric["status"] = "failed"
            raise
        metric.update(status="completed", output_tokens=100, total_tokens=metric["input_tokens"] + 100,
                      input_token_source="usageMetadata")
        return reply


def call(name, arguments, call_id="call_1"):
    return ModelReply(calls=[{"id": call_id, "name": name, "arguments": arguments}],
                      assistant_message={"role": "assistant", "content": "", "tool_calls": [
                          {"id": call_id, "type": "function", "function": {
                              "name": name, "arguments": json.dumps(arguments)}}]})


def answer(text="Done.", visuals=None):
    return ModelReply(content=json.dumps({"answer": text, "visuals": visuals or []}))


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = Path(__file__).resolve().parent / ("agent-test-" + uuid4().hex)
        cls.temp.mkdir()
        cls.db = cls.temp / "copilot.sqlite3"
        project = Path(__file__).resolve().parents[1]
        existing = project / "data/copilot.sqlite3"
        if existing.exists():
            with closing(connect(existing)) as source, closing(sqlite3.connect(cls.db)) as destination:
                source.backup(destination)
            with closing(connect(cls.db)) as conn:
                cls.dataset_id = conn.execute("SELECT dataset_id FROM wells WHERE asset_id='WELL-0001' LIMIT 1").fetchone()[0]
        else:
            cls.dataset_id = import_dataset(project / "challenge-dataset/sample-data", cls.db)["dataset_id"]
        cls.settings = Settings(db_path=str(cls.db), field_timezone="UTC", max_tool_rounds=4)
        Copilot(cls.db, cls.dataset_id, "U-201", cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.temp.resolve().parent == Path(__file__).resolve().parent and cls.temp.name.startswith("agent-test-"):
            shutil.rmtree(cls.temp)

    def setUp(self):
        with closing(connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM pending_updates")
            conn.execute("DELETE FROM context_notes WHERE origin='runtime'")
            conn.execute("DELETE FROM corrections")

    def copilot(self, client=None, user="U-201"):
        return Copilot(self.db, self.dataset_id, user, self.settings, client=client)

    def runtime_count(self):
        with closing(connect(self.db)) as conn:
            return conn.execute("SELECT count(*) FROM context_notes WHERE origin='runtime'").fetchone()[0]

    def stage_note(self, user="U-201", session="session-1"):
        args = {"asset_id": "WELL-0001", "text": "Emergency maintenance through 20 October; restart unconfirmed.",
                "valid_from": "2026-10-17", "valid_to": "2026-10-20"}
        client = ScriptedClient(call("add_shared_note", args))
        copilot = self.copilot(client, user)
        result = copilot.respond("FYI WELL-0001 is under emergency maintenance from 17 October through 20 October. Save this shared update.", session)
        self.assertIsNotNone(result["pending_update"])
        return copilot, result["pending_update"]

    def test_tool_loop_uses_backend_visual_rows_and_persists_response(self):
        client = ScriptedClient(
            call("get_well", {"asset_id": "WELL-0001"}),
            answer("The latest inspection finds the well not producing. [S1] [S999]", [{
                "kind": "status", "source_tool": "T1", "title": "Latest observation",
                "data": [{"asset_id": "INVENTED", "is_producing": True}]}]),
        )
        result = self.copilot(client).respond("Show the latest status of WELL-0001.", "visuals")
        self.assertEqual(len(client.requests), 2)
        self.assertTrue(result["citations"])
        self.assertIn("[unverified source]", result["answer"])
        visual = result["visuals"][0]
        self.assertEqual(visual["kind"], "status")
        self.assertEqual(visual["data"][0]["asset_id"], "WELL-0001")
        self.assertEqual(visual["data"][0]["is_producing"], 0)
        tools = [m for m in client.requests[1][0] if m["role"] == "tool"]
        self.assertEqual(tools[0]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tools[0]["content"])["tool_result_id"], "T1")
        with closing(connect(self.db)) as conn:
            rows = conn.execute("SELECT role,content,payload_json FROM chat_messages ORDER BY message_id").fetchall()
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual(json.loads(rows[-1]["payload_json"])["visuals"], result["visuals"])

    def test_all_model_round_token_counts_are_persisted_and_live_callback_precedes_reply(self):
        client = MeteredClient(call("get_well", {"asset_id": "WELL-0001"}), answer("Inspection evidence retrieved. [S1]"))
        notifications = []
        result = self.copilot(client).respond("Show WELL-0001 status.", "metered", usage_callback=notifications.append)
        self.assertEqual([m["input_tokens"] for m in result["token_usage"]], [1000, 1500])
        self.assertEqual([m["status"] for m in result["token_usage"]], ["completed", "completed"])
        self.assertEqual(notifications[0][0]["status"], "pending")
        self.assertEqual(notifications[0][0]["input_token_source"], "countTokens")
        with closing(connect(self.db)) as conn:
            saved = json.loads(conn.execute("SELECT payload_json FROM chat_messages WHERE role='assistant' ORDER BY message_id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(saved["token_usage"], result["token_usage"])

    def test_counted_input_survives_generation_failure_without_fabricated_output(self):
        client = MeteredClient(ModelError("Provider failed."))
        result = self.copilot(client).respond("Hello.", "metered-failure")
        self.assertTrue(result["error"])
        self.assertEqual(result["token_usage"][0]["input_tokens"], 1000)
        self.assertEqual(result["token_usage"][0]["status"], "failed")
        self.assertIsNone(result["token_usage"][0]["output_tokens"])

    def test_unsupported_oil_volume_question_uses_no_model_client(self):
        with patch("copilot.agent.GemmaClient", side_effect=AssertionError("Unsupported measured volume needs no model")) as constructor:
            result = self.copilot().respond("Which well produced the most oil in 2026?", "missing-production-volume")
        constructor.assert_not_called()
        self.assertEqual(result["capability_limit"], "measured_oil_production_volume_unavailable")
        self.assertEqual(result["token_usage"], [])
        self.assertEqual(result["visuals"], [])
        self.assertIn("no measured oil-production volume", result["answer"])
        self.assertEqual(result["tool_calls"][0]["mode"], "local dataset capability check")

    def test_fully_specified_correction_prepares_review_without_model_and_shares_after_confirmation(self):
        prompt = ("Please correct inspection INS-0164-0205 for WELL-0164, dated 15 October 2026. "
                  "The recorded wellhead temperature of 24.0°C was wrong because the handheld thermal gun was faulty. "
                  "The field crew re-measured it at 41.0°C. Save 41.0°C as the corrected wellhead temperature, "
                  "preserving the original reading and attributing this correction to me. "
                  "Ambient temperature remains 19.9°C. Keep Tyler Hughes’s “Pump jack not moving” note "
                  "and flag its disagreement with the corrected reading.")
        copilot = self.copilot()
        with patch("copilot.agent.GemmaClient", side_effect=AssertionError("No model is needed for an explicit draft")):
            result = copilot.respond(prompt, "explicit-draft")
        self.assertEqual(result["write_status"], "pending_confirmation")
        self.assertEqual(result["token_usage"], [])
        self.assertEqual(result["tool_calls"][0]["tool"], "correct_inspection")
        self.assertEqual(result["tool_calls"][0]["status"], "pending_confirmation")
        pending = result["pending_update"]
        self.assertEqual(pending["preview"]["current_value"], 24)
        self.assertEqual(pending["preview"]["new_value"], 41)
        self.assertEqual(pending["preview"]["author"], "Michael Turner")
        self.assertEqual(self.runtime_count(), 0)
        before = self.copilot(user="U-202").tools.execute("get_well", {"asset_id": "WELL-0164"})
        self.assertEqual(before["latest_inspection"]["is_producing"], 0)
        copilot.confirm_update(pending, "explicit-draft")
        after = self.copilot(user="U-202").tools.execute("get_well", {"asset_id": "WELL-0164"})
        self.assertEqual(after["latest_inspection"]["wellhead_temp_c"], 24)
        self.assertEqual(after["latest_inspection"]["effective_wellhead_temp_c"], 41)
        self.assertAlmostEqual(after["latest_inspection"]["temperature_delta_c"], 21.1)
        self.assertEqual(after["latest_inspection"]["is_producing"], 1)

    def test_write_failure_after_reads_reports_unsaved_instead_of_status_fallback(self):
        client = ScriptedClient(call("get_well", {"asset_id": "WELL-0164"}),
                                ModelError("Google HTTP 500 INTERNAL", status_code=500, retryable=True))
        result = self.copilot(client).respond("Please correct the latest WELL-0164 reading; the thermal gun was faulty.", "write-read-failure")
        self.assertEqual(result["write_status"], "not_saved")
        self.assertIn("No shared update was saved", result["answer"])
        self.assertNotIn("Inspection-based production status", result["answer"])
        self.assertEqual(result["visuals"], [])
        self.assertIsNone(result["pending_update"])
        self.assertIn("500", result["provider_error"])
        self.assertEqual(self.runtime_count(), 0)

    def test_read_only_model_reply_cannot_claim_a_requested_update_was_saved(self):
        client = ScriptedClient(call("get_well", {"asset_id": "WELL-0164"}),
                                answer("I saved your new reading."), answer("I saved your new reading."))
        result = self.copilot(client).respond("Please correct the latest WELL-0164 temperature reading.", "no-fake-write")
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(result["write_status"], "not_saved")
        self.assertNotIn("I saved your new reading", result["answer"])
        self.assertIsNone(result["pending_update"])
        self.assertEqual(self.runtime_count(), 0)

    def test_explicit_correction_rejects_a_different_named_well(self):
        prompt = ("Please correct inspection INS-0164-0205 for WELL-0001. "
                  "Set wellhead temperature to 41.0°C because the thermal gun was faulty.")
        with patch("copilot.agent.GemmaClient", side_effect=AssertionError("Invalid draft must not reach a provider")):
            result = self.copilot().respond(prompt, "mismatched-well")
        self.assertEqual(result["write_status"], "not_saved")
        self.assertIn("does not match", result["answer"])
        self.assertEqual(self.runtime_count(), 0)

    def test_request_to_show_a_correction_is_still_read_only(self):
        client = ScriptedClient(call("get_well", {"asset_id": "WELL-0164"}), answer("No saved temperature correction exists."))
        result = self.copilot(client).respond("Show the correction history for WELL-0164.", "read-corrections")
        self.assertNotEqual(result.get("write_status"), "not_saved")
        self.assertIn("No saved temperature correction", result["answer"])

    def test_factual_remeasurement_report_authorizes_reviewed_correction(self):
        client = ScriptedClient(call("correct_inspection", {"inspection_id": "INS-0164-0205",
                                "field": "wellhead_temp_c", "value": 41.0, "reason": "Faulty thermal gun; field crew remeasured."}))
        result = self.copilot(client).respond("The 15 Oct reading for WELL-0164 is wrong. The handheld thermal gun used in that inspection was faulty; the field crew re-measured the wellhead at 41.0°C.", "natural-correction")
        self.assertEqual(result["write_status"], "pending_confirmation")
        self.assertEqual(self.runtime_count(), 0)

    def test_proposal_and_evidence_refusal_keep_token_telemetry(self):
        client = MeteredClient(call("add_shared_note", {
            "asset_id": "WELL-0001", "text": "Planned maintenance reported.", "valid_from": "2026-10-17"}))
        result = self.copilot(client).respond("FYI WELL-0001 is under planned maintenance.", "metered-proposal")
        self.assertIsNotNone(result["pending_update"])
        self.assertEqual(len(result["token_usage"]), 1)
        refusal = self.copilot(MeteredClient(answer("Unverified."), answer("Still unverified."))).respond("Is WELL-0001 producing?", "metered-refusal")
        self.assertIn("could not retrieve", refusal["answer"])
        self.assertEqual(len(refusal["token_usage"]), 2)

    def test_provider_failure_after_query_keeps_requested_facts_and_sources(self):
        sql = ("SELECT li.is_producing, li.inspected_at, u.name AS inspector_name "
               "FROM latest_inspections li JOIN users u ON li.inspector_user_id=u.user_id "
               "WHERE li.asset_id='WELL-0001'")
        client = ScriptedClient(call("query_records", {"sql": sql}), ModelError("Provider HTTP 503 after retries."))
        result = self.copilot(client).respond("Is WELL-0001 producing? When was it inspected, and by whom?", "provider-failed")
        self.assertTrue(result["partial"])
        self.assertIn("Not producing", result["answer"])
        self.assertIn("2026-10-15", result["answer"])
        self.assertIn("Ryan Brooks", result["answer"])
        self.assertIn("live production is unconfirmed", result["answer"])
        self.assertIn("model could not finish", result["answer"])
        self.assertIn("[S1]", result["answer"])
        self.assertEqual(result["visuals"][0]["data"][0]["is_producing"], 0)
        self.assertTrue(result["citations"])
        self.assertNotIn("_identity", result["citations"][0])
        with closing(connect(self.db)) as conn:
            saved = json.loads(conn.execute("SELECT payload_json FROM chat_messages WHERE role='assistant' ORDER BY message_id DESC LIMIT 1").fetchone()[0])
        self.assertTrue(saved["partial"])
        self.assertEqual(saved["provider_error"], "Provider HTTP 503 after retries.")

    def test_provider_failure_preserves_well_context_as_attributed_report(self):
        client = ScriptedClient(call("get_well", {"asset_id": "WELL-0001"}), ModelError("Temporary provider error."))
        result = self.copilot(client).respond("Show WELL-0001 status and crew reports.", "well-failed")
        self.assertTrue(result["partial"])
        self.assertIn("Crew report", result["answer"])
        self.assertIn("Jake Morales", result["answer"])
        self.assertIn("2026-10-20", result["answer"])
        self.assertEqual(result["visuals"][0]["kind"], "status")

    def test_partial_record_notes_show_author_expiry_and_dispute_status(self):
        # The fallback must retain metadata even when model synthesis fails.
        result = {"well": {"asset_id": "WELL-0001"}, "latest_inspection": None,
                  "shared_notes": [
                      {"note_id": "EXPIRED", "author_name": "Jake Morales",
                       "applicability": "expired", "correction_status": "disputed",
                       "valid_from": "2026-10-01", "valid_to": "2026-10-03",
                       "text": "Proposed correction: spill_detected=true"},
                      {"note_id": "ACCEPTED", "author_user_id": "U-401",
                       "applicability": "current", "correction_status": "accepted",
                       "valid_from": "2026-10-01", "valid_to": None,
                       "text": "Confirmed corrected reading"}], "citations": []}
        citations = [{"id": "S1", "record_id": "EXPIRED"}, {"id": "S2", "record_id": "ACCEPTED"}]
        fallback = _record_fallback({"T1": result}, citations)
        self.assertIn("Jake Morales", fallback["answer"])
        self.assertIn("expired validity; does not establish current conditions", fallback["answer"])
        self.assertIn("disputed correction — not applied", fallback["answer"])
        self.assertIn("U-401", fallback["answer"])
        self.assertIn("accepted correction", fallback["answer"])
        self.assertIn("[S1]", fallback["answer"])
        self.assertIn("[S2]", fallback["answer"])

    def test_partial_many_results_explicitly_identifies_visual_omission(self):
        results = {f"T{i}": {"rows": [{"asset_id": f"WELL-{i:04d}", "cost_total": i * 100},
                                      {"asset_id": f"WELL-{i + 10:04d}", "cost_total": i * 200}],
                                "citations": []} for i in range(1, 5)}
        fallback = _record_fallback(results, [])
        self.assertEqual(len(fallback["visuals"]), 3)
        self.assertEqual(fallback["answer"].count("shown below"), 3)
        self.assertIn("detailed table is omitted", fallback["answer"])
        self.assertIn("at most three visuals", fallback["answer"])

    def test_provider_failure_before_retrieval_does_not_invent_fallback(self):
        result = self.copilot(ScriptedClient(ModelError("Provider unavailable."))).respond("Is WELL-0001 producing?", "first-failed")
        self.assertFalse(result["partial"])
        self.assertEqual(result["answer"], "Provider unavailable.")
        self.assertFalse(result["citations"])
        self.assertFalse(result["visuals"])

    def test_transient_first_request_failure_can_read_exact_well_locally(self):
        client = ScriptedClient(ModelError("Provider HTTP 500.", status_code=500, retryable=True, attempts=3))
        result = self.copilot(client).respond("Is well-001 producing? When was it inspected and by whom?", "local-fallback")
        self.assertTrue(result["partial"])
        self.assertIn("Not producing", result["answer"])
        self.assertIn("2026-10-15", result["answer"])
        self.assertIn("Ryan Brooks", result["answer"])
        self.assertEqual(result["tool_calls"][0]["tool"], "get_well")
        self.assertEqual(result["tool_calls"][0]["arguments"]["asset_id"], "WELL-0001")
        self.assertIn("after provider failure", result["tool_calls"][0]["mode"])
        self.assertEqual(len(client.requests), 1)

    def test_rate_limit_still_allows_local_well_read_without_another_api_call(self):
        client = ScriptedClient(ModelError("OpenRouter HTTP 429.", status_code=429))
        result = self.copilot(client).respond("Show WELL-0001 status.", "rate-limit-read")
        self.assertTrue(result["partial"])
        self.assertIn("Not producing", result["answer"])
        self.assertEqual(result["tool_calls"][0]["tool"], "get_well")
        self.assertEqual(len(client.requests), 1)

    def test_transient_failure_does_not_turn_report_into_local_status_answer(self):
        client = ScriptedClient(ModelError("Provider HTTP 500.", status_code=500, retryable=True))
        result = self.copilot(client).respond("FYI WELL-0001 is down. Save this shared update.", "write-outage")
        self.assertFalse(result["partial"])
        self.assertFalse(result["tool_calls"])
        self.assertEqual(self.runtime_count(), 0)
        self.assertIsNone(result["pending_update"])

    def test_empty_query_stays_empty_in_provider_failure_fallback(self):
        client = ScriptedClient(call("query_records", {"sql": "SELECT asset_id FROM wells WHERE 1=0"}), ModelError("Provider unavailable."))
        result = self.copilot(client).respond("Find absent wells.", "empty-failed")
        self.assertTrue(result["partial"])
        self.assertIn("no matching records", result["answer"])
        self.assertNotIn("Not producing", result["answer"])
        self.assertFalse(result["visuals"])

    def test_staging_does_not_write_and_confirmation_is_idempotent(self):
        copilot, pending = self.stage_note()
        self.assertEqual(self.runtime_count(), 0)
        tampered = copy.deepcopy(pending)
        tampered["arguments"] = {"asset_id": "WELL-0002", "text": "Tampered browser values"}
        saved = copilot.confirm_update(tampered, "session-1")
        repeated = copilot.confirm_update(pending, "session-1")
        self.assertEqual(saved, repeated)
        self.assertEqual(self.runtime_count(), 1)
        with closing(connect(self.db)) as conn:
            note = conn.execute("SELECT * FROM context_notes WHERE origin='runtime'").fetchone()
        self.assertEqual(note["asset_id"], "WELL-0001")
        self.assertNotIn("Tampered", note["text"])
        other = self.copilot(user="U-202").tools.execute("get_well", {"asset_id": "WELL-0001"})
        self.assertTrue(any(n["note_id"] == note["note_id"] for n in other["shared_notes"]))

    def test_pending_scope_is_user_and_conversation_bound(self):
        copilot, pending = self.stage_note()
        with self.assertRaises(ValueError):
            self.copilot(user="U-202").confirm_update(pending, "session-1")
        with self.assertRaises(ValueError):
            copilot.confirm_update(pending, "another-session")
        self.assertEqual(self.runtime_count(), 0)
        # A new service instance can confirm the persistent server-side proposal.
        self.copilot().confirm_update(pending, "session-1")
        self.assertEqual(self.runtime_count(), 1)

    def test_cancel_is_durable_and_prevents_later_confirmation(self):
        copilot, pending = self.stage_note()
        copilot.cancel_update(pending, "session-1")
        with self.assertRaises(ValueError):
            self.copilot().confirm_update(pending, "session-1")
        self.assertEqual(self.runtime_count(), 0)
        with closing(connect(self.db)) as conn:
            pending_status = conn.execute("SELECT status FROM pending_updates WHERE pending_id=?", (pending["pending_id"],)).fetchone()[0]
            last = conn.execute("SELECT content,payload_json FROM chat_messages ORDER BY message_id DESC LIMIT 1").fetchone()
        self.assertEqual(pending_status, "cancelled")
        self.assertIn("cancelled", last["content"])
        self.assertIsNone(json.loads(last["payload_json"])["pending_update"])

    def test_hypothetical_cannot_stage_a_shared_update(self):
        client = ScriptedClient(call("add_shared_note", {
            "asset_id": "WELL-0001", "text": "Hypothetical", "valid_from": "2026-10-17"}), answer("No update was saved."))
        result = self.copilot(client).respond("What if WELL-0001 is under maintenance?", "hypothesis")
        self.assertIsNone(result["pending_update"])
        self.assertEqual(self.runtime_count(), 0)
        self.assertEqual(result["tool_calls"][0]["status"], "error")

    def test_private_history_never_crosses_user_or_conversation(self):
        first = self.copilot(ScriptedClient(answer("Noted privately.")))
        first.respond("Private phrase ALPHA-SECRET.", "reused-session")
        same_client = ScriptedClient(answer("I can see your earlier private message."))
        self.copilot(same_client).respond("What did I just say?", "reused-session")
        self.assertTrue(any("ALPHA-SECRET" in m.get("content", "") for m in same_client.requests[0][0]))
        other_client = ScriptedClient(answer("No relevant private history."))
        self.copilot(other_client, "U-202").respond("What did we discuss?", "reused-session")
        self.assertFalse(any("ALPHA-SECRET" in m.get("content", "") for m in other_client.requests[0][0]))
        new_session = ScriptedClient(answer("New conversation."))
        self.copilot(new_session).respond("Hello.", "new-session")
        self.assertFalse(any("ALPHA-SECRET" in m.get("content", "") for m in new_session.requests[0][0]))
        own_history = first.tools.execute("recent_my_conversations", {"limit": 30})
        self.assertTrue(any("ALPHA-SECRET" in r["content"] for r in own_history["messages"]))
        other_history = self.copilot(user="U-202").tools.execute("recent_my_conversations", {"limit": 30})
        self.assertFalse(any("ALPHA-SECRET" in r["content"] for r in other_history["messages"]))

    def test_recent_private_context_prefers_newest_messages_and_discloses_omission(self):
        with closing(connect(self.db)) as conn, conn:
            for index in range(24):
                role = "user" if index % 2 == 0 else "assistant"
                conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?,?)",
                             (self.dataset_id, "U-201", "bounded-private", role,
                              f"HISTORY-{index:02d}: " + "Prior conversation detail. " * 65, "2026-10-17T09:00:00Z"))
        client = ScriptedClient(answer("I retained recent context."))
        current = "Continue our most recent discussion."
        self.copilot(client).respond(current, "bounded-private")
        history = client.requests[0][0][1:]
        self.assertLessEqual(sum(len(item["content"]) for item in history), MAX_RECENT_HISTORY_CHARS)
        self.assertEqual(history[-1], {"role": "user", "content": current})
        self.assertLessEqual(sum(item["role"] != "system" for item in history), 16)
        serialized = json.dumps(history)
        self.assertIn("HISTORY-23", serialized)
        self.assertNotIn("HISTORY-00", serialized)
        self.assertIn("not complete history", serialized)
        self.assertIn("search_my_conversations", serialized)
        with closing(connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM chat_messages WHERE session_id='bounded-private'").fetchone()[0], 26)

    def test_long_current_prompt_is_preserved_entirely_without_private_history_leak(self):
        with closing(connect(self.db)) as conn, conn:
            for user, session, content in [
                ("U-201", "long-prompt", "OLDER-OWN-CONTEXT"),
                ("U-202", "long-prompt", "OTHER-USER-SECRET"),
                ("U-201", "different-session", "OTHER-SESSION-SECRET"),
            ]:
                conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,'user',?,?)",
                             (self.dataset_id, user, session, content, "2026-10-17T09:00:00Z"))
        current = "CURRENT-START " + "x" * 19960 + " CURRENT-END"
        self.assertGreater(len(current), MAX_RECENT_HISTORY_CHARS)
        client = ScriptedClient(answer("Received your full message."))
        self.copilot(client).respond(current, "long-prompt")
        history = client.requests[0][0][1:]
        self.assertEqual(history[-1], {"role": "user", "content": current})
        self.assertEqual(sum(item["role"] != "system" for item in history), 1)
        serialized = json.dumps(history)
        self.assertNotIn("OLDER-OWN-CONTEXT", serialized)
        self.assertNotIn("OTHER-USER-SECRET", serialized)
        self.assertNotIn("OTHER-SESSION-SECRET", serialized)
        self.assertIn("not complete history", serialized)

    def test_dated_private_recall_tool_retrieves_old_sessions_with_sources(self):
        with closing(connect(self.db)) as conn, conn:
            for uid, session, text, at in [
                ("U-201", "yesterday-old", "Discussed WELL-0001, WELL-0002 and WELL-0003.", "2026-10-16T10:00:00Z"),
                ("U-202", "yesterday-other-user", "PRIVATE-OTHER-USER WELL-0100", "2026-10-16T10:00:00Z"),
            ]:
                conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,'user',?,?)",
                             (self.dataset_id, uid, session, text, at))
            for index in range(35):
                conn.execute("INSERT INTO chat_messages(dataset_id,user_id,session_id,role,content,created_at) VALUES (?,?,?,'user',?,'2026-10-17T09:00:00Z')",
                             (self.dataset_id, "U-201", f"new-{index}", "Newer unrelated message"))
        client = ScriptedClient(call("search_my_conversations", {"period": "yesterday"}),
                                answer("Yesterday you discussed WELL-0001, WELL-0002 and WELL-0003. [S1]"))
        settings = replace(self.settings, as_of_date="2001-01-01", field_timezone="Asia/Kolkata")
        with patch("copilot.agent.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
            result = Copilot(self.db, self.dataset_id, "U-201", settings, client=client).respond(
                "What were the three wells we discussed yesterday?", "new-recall")
        self.assertEqual(result["tool_calls"][0]["status"], "completed")
        payload = json.loads(next(message["content"] for message in client.requests[1][0] if message["role"] == "tool"))
        recalled = payload["result"]["messages"]
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["session_id"], "yesterday-old")
        self.assertNotIn("PRIVATE-OTHER-USER", json.dumps(client.requests))
        self.assertEqual(result["citations"][0]["source_path"], "runtime:private-chat")
        self.assertEqual(result["citations"][0]["created_at"], "2026-10-16T10:00:00Z")
        self.assertIn("[S1]", result["answer"])
        instructions = client.requests[0][0][0]["content"]
        self.assertIn("Private-history TODAY: 2026-10-17", instructions)
        self.assertIn("Operational report TODAY: 2026-10-17", instructions)
        self.assertIn("Evaluation date: 2001-01-01", instructions)

    def test_quoted_fyi_question_does_not_authorize_a_write(self):
        client = ScriptedClient(call("add_shared_note", {
            "asset_id": "WELL-0001", "text": "Quoted statement", "valid_from": "2026-10-17"}),
            answer("That quoted statement was not saved as an update."))
        result = self.copilot(client).respond("What did Laura mean by 'FYI WELL-0001 is under maintenance'?", "quoted")
        self.assertIsNone(result["pending_update"])
        self.assertEqual(self.runtime_count(), 0)

    def test_field_answer_without_fresh_evidence_is_withheld(self):
        client = ScriptedClient(answer("Invented production claim."), answer("Another invented claim."))
        result = self.copilot(client).respond("Is WELL-0001 producing oil?", "no-evidence")
        self.assertIn("could not retrieve", result["answer"])
        self.assertNotIn("Invented", result["answer"])
        self.assertFalse(result["citations"])

    def test_excessive_parallel_calls_fail_without_unmatched_tool_responses(self):
        calls = [{"id": str(i), "name": "get_well", "arguments": {"asset_id": "WELL-0001"}} for i in range(9)]
        client = ScriptedClient(ModelReply(calls=calls, assistant_message={"role": "assistant", "content": ""}))
        result = self.copilot(client).respond("Show this well", "too-many")
        self.assertTrue(result["error"])
        self.assertIn("too many tools", result["answer"])
        self.assertEqual(len(client.requests), 1)

    def test_three_call_limit_reserves_last_call_for_synthesis(self):
        client = ScriptedClient(
            call("get_well", {"asset_id": "WELL-0001"}, "first"),
            call("get_document", {"path": "fleet/well-types/T200.md"}, "second"),
            answer("The inspection and applicable manual are retrieved. [S1]"),
        )
        copilot = Copilot(self.db, self.dataset_id, "U-201", replace(self.settings, max_tool_rounds=8), client=client)
        result = copilot.respond("Explain WELL-0001 production using its manual.", "three-calls")
        self.assertEqual(len(client.requests), 3)
        self.assertTrue(client.requests[0][1])
        self.assertTrue(client.requests[1][1])
        self.assertEqual(client.requests[2][1], [])
        self.assertIn("final model call", client.requests[2][0][-1]["content"])
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertFalse(result.get("partial", False))

    def test_containment_question_retrieves_rule_and_reports_in_one_tool_batch(self):
        client = ScriptedClient(call("get_spill_compliance", {"area": "Southwest Block 24", "days": 90}),
                                answer("Two spills exceeded the SOP deadline: WELL-0082 and WELL-0100. [S1]"))
        result = self.copilot(client).respond(
            "In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?",
            "containment-workflow")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual([d["name"] for d in client.requests[0][1]], ["get_spill_compliance"])
        self.assertEqual(client.requests[1][1], [])
        self.assertEqual(result["tool_calls"][0]["status"], "completed")
        tool_message = next(item for item in client.requests[1][0] if item["role"] == "tool")
        payload = json.loads(tool_message["content"])
        self.assertEqual(payload["result"]["total_spills"], 10)
        self.assertEqual(len(payload["result"]["rows"]), 10)
        self.assertIn("spill-response-sop.md", tool_message["content"])
        self.assertIn("MNT-0082-028", tool_message["content"])
        self.assertIn("MNT-0100-034", tool_message["content"])
        self.assertLessEqual(len(tool_message["content"]), 12000)
        self.assertEqual(len(result["visuals"][0]["data"]), 10)

    def test_unexpected_final_function_call_never_executes_or_calls_model_again(self):
        client = ScriptedClient(
            call("query_records", {"sql": "SELECT asset_id FROM wells LIMIT 1"}, "first"),
            call("get_document", {"path": "fleet/well-types/T200.md"}, "second"),
            call("add_shared_note", {"asset_id": "WELL-0001", "text": "Must not execute"}, "third"),
        )
        result = self.copilot(client).respond("Show a well and its inspection manual.", "final-tool-refused")
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(len(result["tool_calls"]), 2)
        self.assertTrue(result["partial"])
        self.assertIn("further retrieval was not performed", result["answer"])
        self.assertIsNone(result["pending_update"])
        self.assertEqual(self.runtime_count(), 0)

    def test_complete_compound_read_finishes_on_second_call_without_another_lookup(self):
        client = ScriptedClient(
            call("get_spill_compliance", {"area": "Southwest Block 24", "days": 90}, "compound"),
            call("get_document", {"path": "fleet/sops/spill-response-sop.md"}, "unnecessary"),
        )
        result = self.copilot(client).respond(
            "In Southwest Block 24, which spills were not contained within the SOP time limit?", "compound-second-final")
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[1][1], [])
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertTrue(result["partial"])
        self.assertIn("stopped after 2 model calls", result["answer"])

    def test_single_call_setting_does_not_spend_an_extra_call_requesting_evidence(self):
        client = ScriptedClient(answer("An unsupported spill claim."))
        copilot = Copilot(self.db, self.dataset_id, "U-201", replace(self.settings, max_tool_rounds=1), client=client)
        result = copilot.respond("Which spills breached the SOP?", "one-call")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0][1], [])
        self.assertIn("could not retrieve", result["answer"])
        self.assertNotIn("unsupported spill claim", result["answer"])

    def test_model_evidence_is_compact_and_discloses_omissions_while_ui_keeps_rows(self):
        rows = [{"inspection_id": f"INS-{index:04d}", "notes": "Detailed evidence. " * 100} for index in range(200)]
        raw_source = {"table": "inspections", "record_id": "INS-0000",
                      "content": "FULL-SERVER-EVIDENCE-ONLY " * 2000}
        client = ScriptedClient(call("query_records", {"sql": "SELECT inspection_id,notes FROM inspections"}),
                                answer("A shortened set is available; full records appear below. [S1]"))
        copilot = self.copilot(client)
        with patch.object(copilot.tools, "execute", return_value={"rows": rows, "returned_rows": 200,
                                                                  "total_rows": 200, "truncated": False,
                                                                  "citations": [raw_source]}):
            result = copilot.respond("List inspection evidence.", "compact-evidence")
        tool_message = next(item for item in client.requests[1][0] if item["role"] == "tool")
        payload = json.loads(tool_message["content"])
        self.assertLessEqual(len(tool_message["content"]), 12000)
        self.assertNotIn("FULL-SERVER-EVIDENCE-ONLY", tool_message["content"])
        self.assertNotIn("citations", payload["result"])
        self.assertEqual(payload["available_citations"], [{"id": "S1", "table": "inspections", "record_id": "INS-0000"}])
        self.assertIn("omitted_from_model_context", tool_message["content"])
        self.assertEqual(payload["result"]["total_rows"], 200)
        visible_rows = [row for row in payload["result"]["rows"] if "inspection_id" in row]
        omitted = sum(row.get("omitted_from_model_context", 0) for row in payload["result"]["rows"])
        self.assertEqual(len(visible_rows) + omitted, 200)
        self.assertEqual(result["visuals"][0]["data"], rows)
        self.assertEqual(result["citations"][0]["content"], raw_source["content"])

    def test_parallel_tool_batches_share_a_bounded_total_model_context(self):
        def batch(start):
            calls = [{"id": f"parallel-{index}", "name": "get_document", "arguments": {"path": f"report-{index}.md"}}
                     for index in range(start, start + 8)]
            return ModelReply(calls=calls, assistant_message={"role": "assistant", "content": "", "tool_calls": [
                {"id": item["id"], "type": "function", "function": {
                    "name": item["name"], "arguments": json.dumps(item["arguments"])}} for item in calls]})
        client = ScriptedClient(batch(0), batch(8), answer("Several report passages were shortened; coverage is incomplete."))
        copilot = self.copilot(client)
        with patch.object(copilot.tools, "execute", return_value={"body": "Substantial report evidence. " * 1000, "citations": []}):
            copilot.respond("Review the spill reports.", "parallel-budget")
        messages = [item for item in client.requests[2][0] if item["role"] == "tool"]
        self.assertEqual(len(messages), 16)
        self.assertLessEqual(sum(len(item["content"]) for item in messages), MAX_TOOL_CONTEXT_CHARS)
        self.assertTrue(all("truncated" in item["content"] for item in messages))

    def test_composed_system_prompt_is_at_least_a_quarter_smaller(self):
        # Original system text at the same schema/date was 9,319 characters.
        client = ScriptedClient(answer("Hello."))
        self.copilot(client).respond("Hello.", "short-system")
        system = client.requests[0][0][0]["content"]
        self.assertLessEqual(len(system), int(9319 * .75))
        self.assertIn("get_spill_compliance", INSTRUCTIONS)
        self.assertIn("qualified-person verification", INSTRUCTIONS)
        self.assertIn("Never guess/expose others' history", INSTRUCTIONS)

    def test_google_parallel_function_responses_pair_ids_and_names(self):
        messages = [{"role": "system", "content": "Policy"}, {"role": "user", "content": "Two tools"},
                    {"role": "assistant", "google_parts": [
                        {"functionCall": {"id": "g1", "name": "get_well", "args": {"asset_id": "WELL-0001"}}},
                        {"functionCall": {"id": "g2", "name": "get_document", "args": {"path": "fleet/well-types/T200.md"}}}]},
                    {"role": "tool", "name": "get_well", "provider_id": "g1", "content": "{}"},
                    {"role": "tool", "name": "get_document", "provider_id": "g2", "_consecutive_tool": True, "content": "{}"}]
        instructions, contents = GemmaClient._google_contents(messages)
        self.assertEqual(instructions, "Policy")
        self.assertEqual([part["functionResponse"]["id"] for part in contents[-1]["parts"]], ["g1", "g2"])
        self.assertEqual([part["functionResponse"]["name"] for part in contents[-1]["parts"]], ["get_well", "get_document"])
        self.assertEqual(contents[-1]["role"], "user")

    def test_google_uses_json_schema_for_nullable_tool_parameters(self):
        client = GemmaClient("offline-test-key", provider="google")
        definitions = self.copilot().tools.tool_definitions()
        with patch.object(client, "_post_json", return_value={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}) as post, \
                patch.object(client, "_count_google_tokens", return_value=(None, "disabled in offline test")):
            reply = client.complete([{"role": "system", "content": "Policy"}, {"role": "user", "content": "Hello"}], definitions)
        self.assertEqual(reply.content, "ok")
        payload = post.call_args.args[2]
        google_definitions = payload["tools"][0]["functionDeclarations"]
        by_name = {definition["name"]: definition for definition in google_definitions}
        self.assertNotIn("parameters", by_name["correct_inspection"])
        self.assertEqual(by_name["correct_inspection"]["parametersJsonSchema"]["properties"]["value"]["type"], ["number", "boolean", "null"])
        self.assertEqual(by_name["add_shared_note"]["parametersJsonSchema"]["properties"]["valid_to"]["type"], ["string", "null"])


if __name__ == "__main__":
    unittest.main()
