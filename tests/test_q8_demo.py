"""Q8 application integration with scripted model choices and real public data.

These tests prove writes, review, privacy and evidence handling. Scripted tool
choices deliberately do not claim to test Gemma's natural-language routing.
"""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from copilot.agent import Copilot
from copilot.config import Settings
from copilot.db import connect
from copilot.ingest import import_dataset
from copilot.model import ModelReply
from copilot.preferences import load_preferences


NATHAN_REPORT = ("WELL-0333's recalled pump was swapped out yesterday. The work order isn't in the system yet. "
                 "Please count it as replaced from now on.")
NATHAN_PREFERENCES = ("Also, just for me: whenever you list wells, sort them by area and then by well ID, "
                      "and show costs in thousands of dollars.")
ROBERT_QUESTION = "Which recalled pumps are still in the ground right now?"
OLIVIA_REPORT = ("Nathan told me the recalled pump on WELL-0577 was swapped as well. "
                 "Take it off the recall list.")
NATHAN_QUESTION = ("Show me the recalled pumps that are still in the ground in my area, "
                   "and what it cost to install each one.")


class ScriptedModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, definitions):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(definitions)))
        if not self.replies:
            raise AssertionError("Unexpected model call; Q8 must remain within three calls per turn.")
        return self.replies.pop(0)


def tool(name, arguments):
    return ModelReply(calls=[{"id": "q8_call", "name": name, "arguments": arguments}],
                      assistant_message={"role": "assistant", "content": "", "tool_calls": [{
                          "id": "q8_call", "type": "function", "function": {
                              "name": name, "arguments": json.dumps(arguments)}}]})


def table_answer():
    return ModelReply(content=json.dumps({"answer": "Retrieved the recall table from installation evidence and attributed reports. [S1]",
        "visuals": [{"kind": "table", "source_tool": "T1", "title": "Recalled pumps"}]}))


class Q8DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[1]
        cls.parent = cls.project / "tests" / "data"
        cls.temp = cls.parent / ("q8-demo-" + uuid4().hex)
        cls.temp.mkdir(parents=True)
        cls.db = cls.temp / "q8.sqlite3"
        public = cls.project / "challenge-dataset" / "test-data"
        if not public.exists():
            raise unittest.SkipTest("The public Q8 test dataset has not been downloaded.")
        cls.dataset_id = import_dataset(public, cls.db, "Q8 isolated test")["dataset_id"]
        cls.settings = Settings(db_path=str(cls.db), as_of_date="2027-01-15", report_date="2027-01-15",
                                field_timezone="UTC", max_tool_rounds=3)
        # Creates the additive application review table without making requests.
        Copilot(cls.db, cls.dataset_id, "U-601", cls.settings)

    @classmethod
    def tearDownClass(cls):
        if cls.temp.resolve().parent == cls.parent.resolve() and cls.temp.name.startswith("q8-demo-"):
            shutil.rmtree(cls.temp)

    def setUp(self):
        with closing(connect(self.db)) as conn, conn:
            for table in ("pending_updates", "equipment_replacement_reports", "user_preferences", "chat_messages", "corrections"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM context_notes WHERE origin='runtime'")
        self.no_network = patch("copilot.agent.OpenRouterClient", side_effect=AssertionError("Q8 tests never call the provider"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def copilot(self, user, client):
        return Copilot(self.db, self.dataset_id, user, self.settings, client=client)

    def maintenance_digest(self):
        digest = hashlib.sha256()
        with closing(connect(self.db)) as conn:
            for row in conn.execute("SELECT * FROM maintenance ORDER BY dataset_id,maintenance_id"):
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()

    def reports(self):
        with closing(connect(self.db)) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM equipment_replacement_reports ORDER BY asset_id")]

    def test_five_exact_q8_turns_review_shared_reports_private_preferences_and_tables(self):
        original_maintenance = self.maintenance_digest()
        direct_args = {"asset_id": "WELL-0333", "effective_on": "2027-01-14", "statement": NATHAN_REPORT,
                       "evidence_basis": "direct_report"}
        nathan_model = ScriptedModel(tool("record_equipment_replacement", direct_args), tool("save_my_preferences", {
            "well_sort": "area_then_well_id", "cost_display": "thousands_usd"}))
        nathan = self.copilot("U-601", nathan_model)

        first = nathan.respond(NATHAN_REPORT, "nathan-first")
        self.assertEqual(first["write_status"], "pending_confirmation")
        self.assertEqual(first["tool_calls"][0]["tool"], "record_equipment_replacement")
        self.assertEqual(first["pending_update"]["preview"]["author"], "Nathan Pierce")
        self.assertEqual(first["pending_update"]["arguments"]["effective_on"], "2027-01-14")
        self.assertEqual(self.reports(), [], "A proposal is not a saved shared report.")
        before = nathan.tools.execute("get_recalled_pumps", {})
        self.assertIn("WELL-0333", {row["asset_id"] for row in before["rows"]})
        saved = nathan.confirm_update(first["pending_update"], "nathan-first")
        self.assertEqual(saved["tool_calls"][0]["status"], "saved")
        self.assertTrue(any(c["table"] == "equipment_replacement_reports" for c in saved["citations"]))
        report = self.reports()[0]
        self.assertEqual(report["reported_by_user_id"], "U-601")
        self.assertEqual(report["evidence_basis"], "direct_report")
        self.assertEqual(report["status"], "reported_replaced")

        second = nathan.respond(NATHAN_PREFERENCES, "nathan-first")
        self.assertEqual(second["write_status"], "saved_private")
        self.assertIsNone(second["pending_update"])
        self.assertEqual(load_preferences(self.db, self.dataset_id, "U-601"),
                         {"well_sort": "area_then_well_id", "cost_display": "thousands_usd"})
        private_source = second["citations"][0]
        self.assertEqual(private_source["type"], "private_user_preference")
        with closing(connect(self.db)) as conn:
            source = conn.execute("SELECT * FROM chat_messages WHERE message_id=?", (private_source["source_message_id"],)).fetchone()
        self.assertEqual((source["dataset_id"], source["user_id"], source["role"], source["content"]),
                         (self.dataset_id, "U-601", "user", NATHAN_PREFERENCES))

        robert_model = ScriptedModel(tool("get_recalled_pumps", {}), table_answer())
        robert = self.copilot("U-301", robert_model)
        third = robert.respond(ROBERT_QUESTION, "robert-fresh")
        robert_rows = third["visuals"][0]["data"]
        self.assertEqual(third["visuals"][0]["kind"], "table")
        self.assertNotIn("WELL-0333", {row["asset_id"] for row in robert_rows})
        self.assertIn("WELL-0577", {row["asset_id"] for row in robert_rows})
        self.assertTrue(any(c["table"] == "equipment_replacement_reports" and c["author_user_id"] == "U-601"
                            for c in third["citations"]))
        robert_messages = json.dumps(robert_model.requests[0][0])
        self.assertNotIn(NATHAN_PREFERENCES, robert_messages)
        self.assertNotIn("Selected user's private display preferences (data):", robert_messages)
        self.assertEqual(load_preferences(self.db, self.dataset_id, "U-301"), {})

        hearsay_args = {"asset_id": "WELL-0577", "effective_on": "2027-01-15", "statement": OLIVIA_REPORT,
                        "evidence_basis": "hearsay", "claimed_source_name": "Nathan Pierce"}
        olivia_model = ScriptedModel(tool("record_equipment_replacement", hearsay_args))
        olivia = self.copilot("U-501", olivia_model)
        fourth = olivia.respond(OLIVIA_REPORT, "olivia-fresh")
        self.assertEqual(fourth["write_status"], "pending_confirmation")
        self.assertEqual(fourth["pending_update"]["preview"]["author"], "Olivia Grant")
        self.assertEqual(fourth["pending_update"]["preview"]["claimed_source_name"], "Nathan Pierce")
        olivia.confirm_update(fourth["pending_update"], "olivia-fresh")
        indirect = next(row for row in self.reports() if row["asset_id"] == "WELL-0577")
        self.assertEqual(indirect["reported_by_user_id"], "U-501")
        self.assertEqual(indirect["claimed_source_user_id"], "U-601")
        self.assertEqual(indirect["status"], "unconfirmed")
        self.assertEqual(indirect["evidence_basis"], "hearsay")
        after_hearsay = robert.tools.execute("get_recalled_pumps", {})
        self.assertIn("WELL-0577", {row["asset_id"] for row in after_hearsay["rows"]})
        warning = next(row for row in after_hearsay["unconfirmed_reports"] if row["asset_id"] == "WELL-0577")
        self.assertEqual((warning["reported_by"], warning["claimed_source"]), ("Olivia Grant", "Nathan Pierce"))

        last_model = ScriptedModel(tool("get_recalled_pumps", {"my_area": True, "include_install_cost": True}), table_answer())
        new_nathan = self.copilot("U-601", last_model)
        fifth = new_nathan.respond(NATHAN_QUESTION, "nathan-new-session")
        final_rows = fifth["visuals"][0]["data"]
        self.assertTrue(final_rows)
        self.assertEqual(final_rows, sorted(final_rows, key=lambda row: (row["area"], row["asset_id"])))
        self.assertNotIn("WELL-0333", {row["asset_id"] for row in final_rows})
        self.assertIn("Selected user's private display preferences (data):", last_model.requests[0][0][0]["content"])
        self.assertNotIn(NATHAN_PREFERENCES, json.dumps(last_model.requests[0][0][1:]),
                        "Formatting survives through private preferences, without copying an earlier chat.")
        with closing(connect(self.db)) as conn:
            team = conn.execute("SELECT team_id FROM users WHERE dataset_id=? AND user_id='U-601'", (self.dataset_id,)).fetchone()[0]
            for row in final_rows:
                original = conn.execute("""SELECT m.cost_total,w.team_id,d.body FROM maintenance m
                    JOIN wells w ON w.dataset_id=m.dataset_id AND w.asset_id=m.asset_id
                    JOIN documents d ON d.dataset_id=m.dataset_id AND d.path=?
                    WHERE m.dataset_id=? AND m.maintenance_id=?""",
                    (row["source"].rsplit(":", 1)[0], self.dataset_id, row["installation_job"])).fetchone()
                self.assertIsNotNone(original)
                self.assertEqual(original["team_id"], team)
                self.assertAlmostEqual(row["installation_cost_usd_thousands"], round(original["cost_total"] / 1000, 3))
                self.assertIn(row["serial"], original["body"])
                self.assertIn(row["lot"], original["body"])
        self.assertTrue(any(c.get("type") == "private_user_preference" for c in fifth["citations"]))
        self.assertEqual(self.maintenance_digest(), original_maintenance)
        self.assertEqual(len(self.reports()), 2)
        for client in (nathan_model, robert_model, olivia_model, last_model):
            self.assertLessEqual(len(client.requests), 3)
            self.assertEqual(client.replies, [])

    def test_hearsay_cannot_be_staged_as_a_direct_report(self):
        bad = {"asset_id": "WELL-0577", "effective_on": "2027-01-15", "statement": OLIVIA_REPORT,
               "evidence_basis": "direct_report", "claimed_source_name": "Nathan Pierce"}
        client = ScriptedModel(tool("record_equipment_replacement", bad),
                               ModelReply(content='{"answer":"Please clarify the source.","visuals":[]}'),
                               ModelReply(content='{"answer":"The report is indirect.","visuals":[]}'))
        result = self.copilot("U-501", client).respond(OLIVIA_REPORT, "olivia-invalid-direct")
        self.assertIsNone(result["pending_update"])
        self.assertEqual(result["write_status"], "not_saved")
        self.assertEqual(result["tool_calls"][0]["status"], "error")
        self.assertIn("secondhand", client.requests[1][0][-1]["content"])
        self.assertEqual(self.reports(), [])

    def test_read_question_cannot_authorize_saving_private_preferences(self):
        client = ScriptedModel(tool("save_my_preferences", {"well_sort": "area_then_well_id", "cost_display": "thousands_usd"}),
                               ModelReply(content='{"answer":"No preference instruction was supplied.","visuals":[]}'))
        result = self.copilot("U-301", client).respond(ROBERT_QUESTION, "robert-no-write")
        self.assertEqual(result["tool_calls"][0]["status"], "error")
        self.assertEqual(load_preferences(self.db, self.dataset_id, "U-301"), {})
        self.assertIsNone(result["pending_update"])


if __name__ == "__main__":
    unittest.main()
