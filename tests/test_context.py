"""Offline request-size and tool-catalogue checks; no provider calls."""

import json
import unittest

from copilot.agent import INSTRUCTIONS, _prompt_data
from copilot.context import context_profile
from copilot.tools import ToolService


class ContextTests(unittest.TestCase):
    def profile(self, prompt, write=False, history=None):
        return context_profile(prompt, history or [{"role": "user", "content": prompt}], write_requested=write)

    def test_recall_compound_tool_avoids_full_schema_and_unrelated_tools(self):
        prompt = "Which recalled pumps are still in the ground right now?"
        profile = self.profile(prompt)
        self.assertEqual(profile.names, {"get_recalled_pumps"})
        self.assertFalse(profile.sql_schema)
        service = ToolService("not-opened.sqlite3", "ds_ff655bf9044589c9e5b89701", "U-601", "2027-01-15")
        system = INSTRUCTIONS + "\nPrivate-history TODAY: 2026-10-17. Operational report TODAY: 2027-01-15. Field timezone: UTC.\n" + service.schema_context(include_sql=profile.sql_schema)
        request = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                   "tools": service.tool_definitions(profile.names)}
        size = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        # Captured from the prior all-13-tools/full-schema request, same prompt/dates.
        # Serialized characters are an offline size metric, not provider tokens.
        self.assertLessEqual(size, int(14674 * .75))

    def test_replacement_write_keeps_read_and_review_tool(self):
        profile = self.profile("WELL-0333's recalled pump was swapped out yesterday. Please count it as replaced from now on.", True)
        self.assertIn("get_recalled_pumps", profile.names)
        self.assertIn("record_equipment_replacement", profile.names)
        self.assertFalse(profile.sql_schema)

    def test_preference_write_and_dated_private_recall_remain_available(self):
        prompt = "Also, just for me: whenever you list wells, sort by area then well ID, costs in thousands of dollars."
        preference = self.profile(prompt, True, [{"role": "user", "content": "WELL-0333's recalled pump was swapped yesterday. Please count it as replaced."},
                                                {"role": "assistant", "content": "Saved."},
                                                {"role": "user", "content": prompt}])
        self.assertIn("save_my_preferences", preference.names)
        self.assertNotIn("query_records", preference.names)
        private = self.profile("What did I ask last week?")
        self.assertIn("search_my_conversations", private.names)

    def test_mixed_recall_question_retains_general_retrieval(self):
        profile = self.profile("List recalled pumps and compare spill costs in 2026.")
        self.assertTrue(profile.sql_schema)
        self.assertTrue({"query_records", "search_documents", "get_document", "get_well", "get_recalled_pumps"} <= profile.names)

    def test_containment_compound_tool_covers_sop_and_reports(self):
        profile = self.profile("In Southwest Block 24, which spills in the last 90 days were not contained within the SOP time limit?")
        self.assertEqual(profile.names, {"get_spill_compliance"})
        self.assertFalse(profile.sql_schema)

    def test_recall_repairs_and_work_order_questions_keep_general_tools(self):
        for prompt in ["List recalled pumps with overdue maintenance and repairs.",
                       "Which recalled pumps have a scheduled follow-up with a technician?",
                       "Show recalled pumps and contractor work order history."]:
            with self.subTest(prompt=prompt):
                profile = self.profile(prompt)
                self.assertTrue(profile.sql_schema)
                self.assertTrue({"get_recalled_pumps", "query_records", "search_documents"} <= profile.names)

    def test_historical_recall_state_keeps_general_tools(self):
        for prompt in ["Which recalled pumps were still in the ground as of 2026-09-01?",
                       "List recalled pumps that were installed last year.",
                       "What was the recalled pump status yesterday?"]:
            with self.subTest(prompt=prompt):
                profile = self.profile(prompt)
                self.assertTrue(profile.sql_schema)
                self.assertTrue({"get_recalled_pumps", "query_records", "search_documents"} <= profile.names)

    def test_containment_and_follow_up_request_keeps_general_tools(self):
        profile = self.profile("In Southwest Block 24, which spills were not contained within the SOP time limit and have overdue repairs?")
        self.assertTrue(profile.sql_schema)
        self.assertTrue({"get_spill_compliance", "query_records", "search_documents"} <= profile.names)

    def test_general_sop_question_retains_document_search(self):
        profile = self.profile("What does the SOP say about spills?")
        self.assertIn("search_documents", profile.names)

    def test_short_follow_up_retains_recall_tool(self):
        history = [{"role": "user", "content": "Which recalled pumps remain?"},
                   {"role": "assistant", "content": "..."},
                   {"role": "user", "content": "Show those in my area with installation costs."}]
        self.assertIn("get_recalled_pumps", self.profile(history[-1]["content"], history=history).names)

    def test_omitted_history_notice_keeps_search_tool(self):
        history = [{"role": "system", "content": "This is not complete history."},
                   {"role": "user", "content": "Continue."}]
        self.assertIn("search_my_conversations", self.profile("Continue.", history=history).names)

    def test_row_citation_reuses_marker_without_repeated_path(self):
        source = {"table": "inspections", "record_id": "INS-1", "source_path": "assets/WELL-0001/inspections.jsonl"}
        citations = [{"id": "S1", "_identity": source}]
        row = _prompt_data({"asset_id": "WELL-0001", "citation": source}, citations=citations)
        self.assertEqual(row["citation"], {"id": "S1"})


if __name__ == "__main__":
    unittest.main()
