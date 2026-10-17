"""Local correction drafts require an unambiguous explicit operator request."""
import unittest

from copilot.correction_drafts import parse_explicit_correction


MICHAEL_REQUEST = (
    "Please correct inspection INS-0164-0205 for WELL-0164, dated 15 October 2026. "
    "The recorded wellhead temperature of 24.0°C was wrong because the handheld thermal gun was faulty. "
    "The field crew re-measured it at 41.0°C. Save 41.0°C as the corrected wellhead temperature, "
    "preserving the original reading and attributing this correction to me. Ambient temperature remains 19.9°C. "
    "Keep Tyler Hughes’s “Pump jack not moving” note and flag its disagreement with the corrected reading."
)


class ExplicitCorrectionTests(unittest.TestCase):
    def test_michael_request_chooses_replacement_not_old_or_ambient_reading(self):
        self.assertEqual(parse_explicit_correction(MICHAEL_REQUEST), {
            "inspection_id": "INS-0164-0205", "field": "wellhead_temp_c", "value": 41.0,
            "reason": "the handheld thermal gun was faulty.",
        })

    def test_numeric_fields_are_generic_and_units_are_not_converted(self):
        examples = [
            ("Set ambient temperature to -5.25°C", "ambient_temp_c", -5.25),
            ("Set spill area to 12.5%", "spill_area_pct", 12.5),
            ("Set spill estimated volume to 5.4 bbl", "spill_est_volume_bbl", 5.4),
            ("Update spill_est_volume_bbl to 0", "spill_est_volume_bbl", 0),
            ("Change wellhead temperature from 24 C to 41 C", "wellhead_temp_c", 41),
        ]
        for command, field, value in examples:
            with self.subTest(command=command):
                result = parse_explicit_correction(f"{command} for inspection INS-0007-0021 because the original measurement was wrong.")
                self.assertIsNotNone(result)
                self.assertEqual((result["inspection_id"], result["field"], result["value"]), ("INS-0007-0021", field, value))

    def test_boolean_detection_is_explicit_not_inferred_from_notes(self):
        for boolean, expected in (("false", False), ("true", True), ("no", False), ("yes", True)):
            with self.subTest(boolean=boolean):
                result = parse_explicit_correction(
                    f"Correct inspection INS-0015-0200. Set spill_detected to {boolean} because a qualified inspector rechecked the detection.")
                self.assertIsNotNone(result)
                self.assertIs(result["value"], expected)
        self.assertIsNone(parse_explicit_correction(
            "Correct inspection INS-0015-0200 because a qualified inspector confirmed the dark area was water."))

    def test_repeat_same_target_is_allowed_and_explicit_reason_is_retained(self):
        result = parse_explicit_correction(
            "Correct inspection INS-0077-0002. Set wellhead_temp_c to 42.5°C. "
            "Save 42.5°C as the corrected wellhead temperature. Reason: Calibrated probe remeasurement.")
        self.assertEqual(result["value"], 42.5)
        self.assertEqual(result["reason"], "Calibrated probe remeasurement.")

    def test_questions_hypotheticals_and_reports_are_not_write_intent(self):
        examples = [
            "Can you correct inspection INS-0164-0205 and set wellhead temperature to 41 because it was faulty?",
            "Is the corrected wellhead temperature 41 for INS-0164-0205 because it was faulty?",
            "If the reading was faulty, correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty.",
            "Please correct inspection INS-0164-0205 as an example. Set wellhead temperature to 41 because the gun was faulty.",
            "Please correct inspection INS-0164-0205. Set wellhead temperature to 41 if the gun was faulty because it was wrong.",
            'Michael said "Please correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty."',
            '"Please correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty."',
            "FYI inspection INS-0164-0205 had a faulty gun. The corrected wellhead temperature was 41 because the gun was faulty.",
        ]
        for request in examples:
            with self.subTest(request=request):
                self.assertIsNone(parse_explicit_correction(request))

    def test_negations_do_not_propose_an_update(self):
        examples = [
            "Do not correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty.",
            "Please correct inspection INS-0164-0205. Don't save 41 as the corrected wellhead temperature because this is only a draft.",
            "Please correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty. Do not apply this yet.",
            "Please correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty. This is not a correction.",
            "Please correct inspection INS-0164-0205. Set wellhead temperature to 41 because the gun was faulty. Do not make any changes yet.",
        ]
        for request in examples:
            with self.subTest(request=request):
                self.assertIsNone(parse_explicit_correction(request))

    def test_incomplete_ambiguous_and_multiple_targets_fall_through(self):
        examples = [
            "Correct inspection INS-0164-0205. Set wellhead temperature to 41.",
            "Correct wellhead temperature to 41 because the gun was faulty.",
            "Correct inspections INS-0164-0205 and INS-0164-0204. Set wellhead temperature to 41 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Set wellhead temperature to 41. Set wellhead temperature to 42 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Set wellhead temperature to 41 and ambient temperature to 20 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Set wellhead temperature to 41 or 42 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Set wellhead temperature to 41-42 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Set wellhead temperature to 4.1e1 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Save 41 as the corrected wellhead temperature. Set spill_detected to 0 because the gun was faulty.",
            "Correct inspection INS-0164-0205. Save 41 as the corrected wellhead temperature. Set ambient temperature to the later reading because the gun was faulty.",
            "Correct inspection INS-0164-0205. Save 41 as the corrected wellhead temperature. Save either 42 or 43 as the corrected wellhead temperature because the gun was faulty.",
            "Correct inspection INS-0164-0205. Save 41 as the corrected wellhead temperature. Set wellhead temperature to 4.1e1 because the gun was faulty.",
            'Correct inspection INS-0164-0205. Michael said "Set wellhead temperature to 41 because the gun was faulty."',
        ]
        for request in examples:
            with self.subTest(request=request):
                self.assertIsNone(parse_explicit_correction(request))

    def test_unsupported_units_and_out_of_range_values_are_rejected(self):
        examples = [
            "Set wellhead temperature to 105 F", "Set wellhead temperature to 105°F",
            "Set wellhead temperature to 105 Fahrenheit", "Set wellhead temperature to 41 kelvin",
            "Change wellhead temperature from 105 F to 41 C",
            "Set wellhead temperature to 501 C", "Set ambient temperature to -101 C",
            "Set spill area to 101%", "Set spill volume to -1 bbl",
            "Set spill volume to 5 litres", "Set spill volume to 1000001 bbl",
            "Set spill_detected to false C", "Set wellhead_temp_c to true",
        ]
        for command in examples:
            with self.subTest(command=command):
                self.assertIsNone(parse_explicit_correction(f"{command} for INS-0001-0001 because the sensor was faulty."))

    def test_wrong_types_and_oversized_requests_are_rejected(self):
        for request in (None, {}, "", " " * 5, MICHAEL_REQUEST + "x" * 8000):
            with self.subTest(request_type=type(request).__name__):
                self.assertIsNone(parse_explicit_correction(request))


if __name__ == "__main__":
    unittest.main()
