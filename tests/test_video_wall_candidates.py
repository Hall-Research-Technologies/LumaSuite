"""
Video Wall site-discovery candidate classification tests
(_vw_classify_candidates / _vw_candidate_fingerprint /
_vw_candidate_device_set_fingerprint / _vw_ambiguous_reason /
_vw_saved_wall_diff) -- pure functions layered on top of the existing,
already-tested _vw_detect_walls() (see test_video_wall_detection.py).

Same hardware fence pattern as the other Python test files here: no test
can reach a real socket, websocket, or HTTP transport.

Run directly: python tests/test_video_wall_candidates.py
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="lumasuite_test_data_")
os.environ["LUMA_DATA_DIR"] = _TMP_DATA_DIR

_fence_patchers = [
    mock.patch("socket.create_connection", mock.Mock(side_effect=AssertionError("HARDWARE FENCE"))),
    mock.patch("socket.socket", mock.Mock(side_effect=AssertionError("HARDWARE FENCE"))),
]
for _p in _fence_patchers:
    _p.start()
try:
    import websocket as _websocket_module
    _fence_patchers.append(mock.patch.object(_websocket_module, "create_connection",
                                              mock.Mock(side_effect=AssertionError("HARDWARE FENCE"))))
    _fence_patchers[-1].start()
except Exception:
    pass
try:
    import requests as _requests_module
    _fence_patchers.append(mock.patch.object(_requests_module, "post", mock.Mock(side_effect=AssertionError("HARDWARE FENCE"))))
    _fence_patchers[-1].start()
    _fence_patchers.append(mock.patch.object(_requests_module, "get", mock.Mock(side_effect=AssertionError("HARDWARE FENCE"))))
    _fence_patchers[-1].start()
except Exception:
    pass

import lumaserver as L  # noqa: E402


def decoder(device_id, row, column, rows=2, columns=2, vmenable=True, streamname="wall-src", rotation=0):
    return {
        "device_id": device_id, "hostname": device_id, "online": True,
        "video_wall": {"rows": rows, "columns": columns, "row": row, "column": column, "vmenable": vmenable},
        "rotation": rotation, "source": streamname,
    }


def saved_wall(wall_id, name, rows, columns, cell_map):
    """cell_map: {device_id: (row, column)}"""
    return {
        "id": wall_id, "name": name, "rows": rows, "columns": columns,
        "cells": [{"decoder_id": did, "row": r, "column": c} for did, (r, c) in cell_map.items()],
    }


TWO_BY_TWO = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1), decoder("lr", 2, 2)]


class FingerprintIdentityTests(unittest.TestCase):
    def test_fingerprint_independent_of_discovery_order(self):
        d1 = L._vw_detect_walls(TWO_BY_TWO)["walls"][0]
        d2 = L._vw_detect_walls(list(reversed(TWO_BY_TWO)))["walls"][0]
        self.assertEqual(L._vw_candidate_fingerprint(d1), L._vw_candidate_fingerprint(d2))

    def test_fingerprint_changes_when_a_member_moves(self):
        base = L._vw_detect_walls(TWO_BY_TWO)["walls"][0]
        moved_states = [decoder("ul", 1, 2), decoder("ur", 1, 1), decoder("ll", 2, 1), decoder("lr", 2, 2)]
        moved = L._vw_detect_walls(moved_states)["walls"][0]
        self.assertNotEqual(L._vw_candidate_fingerprint(base), L._vw_candidate_fingerprint(moved))

    def test_fingerprint_stable_across_vmenable_and_streamname_drift(self):
        # Same positions, same devices, only vmenable/streamname/rotation
        # differ -- fingerprint must not move, since those are supporting-
        # only / prohibited-alone signals, never membership evidence.
        d1 = L._vw_detect_walls(TWO_BY_TWO)["walls"][0]
        drifted_states = [
            decoder("ul", 1, 1, vmenable=False, streamname="stream-main", rotation=180),
            decoder("ur", 1, 2, vmenable=True, streamname="room-a", rotation=180),
            decoder("ll", 2, 1, vmenable=True, streamname="stream-main", rotation=0),
            decoder("lr", 2, 2, vmenable=False, streamname="other", rotation=0),
        ]
        d2 = L._vw_detect_walls(drifted_states)["walls"][0]
        self.assertEqual(L._vw_candidate_fingerprint(d1), L._vw_candidate_fingerprint(d2))

    def test_device_set_fingerprint_ignores_position(self):
        fp1 = L._vw_candidate_device_set_fingerprint(2, 2, ["ul", "ur", "ll", "lr"])
        fp2 = L._vw_candidate_device_set_fingerprint(2, 2, ["lr", "ll", "ur", "ul"])
        self.assertEqual(fp1, fp2)
        fp3 = L._vw_candidate_device_set_fingerprint(2, 2, ["ul", "ur", "ll", "xx"])
        self.assertNotEqual(fp1, fp3)


class StatusClassificationTests(unittest.TestCase):
    def test_complete_unsaved_wall_is_possible(self):
        detected = L._vw_detect_walls(TWO_BY_TWO)
        candidates = L._vw_classify_candidates(detected, saved_walls=[], ignored_candidates=[])
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["candidate_status"], "POSSIBLE")
        self.assertIsNone(c["matched_saved_wall"])
        self.assertIsNone(c["known_wall_changed"])
        self.assertTrue(c["needs_review"])

    def test_matched_unchanged_saved_wall_is_detected_and_not_reviewable(self):
        detected = L._vw_detect_walls(TWO_BY_TWO)
        sw = saved_wall("wall-1", "2x2", 2, 2, {"ul": (1, 1), "ur": (1, 2), "ll": (2, 1), "lr": (2, 2)})
        candidates = L._vw_classify_candidates(detected, saved_walls=[sw], ignored_candidates=[])
        c = candidates[0]
        self.assertEqual(c["candidate_status"], "DETECTED")
        self.assertEqual(c["matched_saved_wall"]["id"], "wall-1")
        self.assertIsNone(c["known_wall_changed"])
        self.assertFalse(c["needs_review"])

    def test_partial_is_reviewable_even_if_no_saved_match(self):
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1)]
        detected = L._vw_detect_walls(states)
        candidates = L._vw_classify_candidates(detected, saved_walls=[], ignored_candidates=[])
        c = candidates[0]
        self.assertEqual(c["candidate_status"], "PARTIAL")
        self.assertTrue(c["needs_review"])

    def test_ordinary_duplicate_coordinate_is_conflict_not_ambiguous(self):
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1), decoder("evil_twin", 2, 1)]
        detected = L._vw_detect_walls(states)
        candidates = L._vw_classify_candidates(detected, saved_walls=[], ignored_candidates=[])
        c = candidates[0]
        self.assertEqual(c["candidate_status"], "CONFLICT")
        self.assertIsNone(c["ambiguous_reason"])
        self.assertTrue(c["needs_review"])

    def test_two_full_same_size_walls_overlapping_in_coordinate_space_is_ambiguous(self):
        # SIMULATED -- not exercised on real hardware: 8 distinct decoders,
        # every one of a 2x2's four coordinates double-claimed. This is the
        # "genuinely ambiguous same-size multi-wall" case the spec calls
        # for LumaSuite to admit rather than guess.
        states = [
            decoder("a1", 1, 1), decoder("a2", 1, 2), decoder("a3", 2, 1), decoder("a4", 2, 2),
            decoder("b1", 1, 1), decoder("b2", 1, 2), decoder("b3", 2, 1), decoder("b4", 2, 2),
        ]
        detected = L._vw_detect_walls(states)
        candidates = L._vw_classify_candidates(detected, saved_walls=[], ignored_candidates=[])
        c = candidates[0]
        self.assertEqual(c["candidate_status"], "AMBIGUOUS")
        self.assertIn("cannot reliably determine", c["ambiguous_reason"])
        self.assertTrue(c["needs_review"])


class KnownWallChangedTests(unittest.TestCase):
    def test_member_missing_is_a_reviewable_diff_not_a_silent_rewrite(self):
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1)]  # lr now gone
        detected = L._vw_detect_walls(states)
        sw = saved_wall("wall-1", "2x2", 2, 2, {"ul": (1, 1), "ur": (1, 2), "ll": (2, 1), "lr": (2, 2)})
        candidates = L._vw_classify_candidates(detected, saved_walls=[sw], ignored_candidates=[])
        c = candidates[0]
        # No exact device-set match anymore (lr missing) -> no matched_saved_wall,
        # so this is honestly POSSIBLE/PARTIAL rather than falsely "detected".
        self.assertIsNone(c["matched_saved_wall"])
        self.assertEqual(c["candidate_status"], "PARTIAL")

    def test_member_at_new_coordinate_is_flagged_as_changed(self):
        moved_states = [decoder("ul", 1, 2), decoder("ur", 1, 1), decoder("ll", 2, 1), decoder("lr", 2, 2)]
        detected = L._vw_detect_walls(moved_states)
        sw = saved_wall("wall-1", "2x2", 2, 2, {"ul": (1, 1), "ur": (1, 2), "ll": (2, 1), "lr": (2, 2)})
        candidates = L._vw_classify_candidates(detected, saved_walls=[sw], ignored_candidates=[])
        c = candidates[0]
        self.assertIsNotNone(c["matched_saved_wall"])
        self.assertIsNotNone(c["known_wall_changed"])
        self.assertTrue(any("moved from" in d for d in c["known_wall_changed"]["diff"]))
        self.assertTrue(c["needs_review"])


class IgnoreLifecycleTests(unittest.TestCase):
    def test_ignored_fingerprint_suppresses_needs_review(self):
        detected = L._vw_detect_walls(TWO_BY_TWO)
        fp = L._vw_candidate_fingerprint(detected["walls"][0])
        ignored = [{"fingerprint": fp}]
        candidates = L._vw_classify_candidates(detected, saved_walls=[], ignored_candidates=ignored)
        c = candidates[0]
        self.assertTrue(c["ignored"])
        self.assertFalse(c["needs_review"])

    def test_material_change_after_ignore_is_reviewable_again(self):
        detected = L._vw_detect_walls(TWO_BY_TWO)
        fp = L._vw_candidate_fingerprint(detected["walls"][0])
        ignored = [{"fingerprint": fp}]
        # Now one member goes missing -- a materially different candidate.
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1)]
        detected2 = L._vw_detect_walls(states)
        candidates = L._vw_classify_candidates(detected2, saved_walls=[], ignored_candidates=ignored)
        c = candidates[0]
        self.assertFalse(c["ignored"])
        self.assertTrue(c["needs_review"])

    def test_known_wall_changed_is_reviewable_even_if_previously_ignored(self):
        # An ignored fingerprint from BEFORE a wall was ever confirmed
        # must not suppress a genuine "known wall changed" review once
        # that grouping later matches a saved wall with a real diff.
        moved_states = [decoder("ul", 1, 2), decoder("ur", 1, 1), decoder("ll", 2, 1), decoder("lr", 2, 2)]
        detected = L._vw_detect_walls(moved_states)
        fp = L._vw_candidate_fingerprint(detected["walls"][0])
        sw = saved_wall("wall-1", "2x2", 2, 2, {"ul": (1, 1), "ur": (1, 2), "ll": (2, 1), "lr": (2, 2)})
        candidates = L._vw_classify_candidates(detected, saved_walls=[sw], ignored_candidates=[{"fingerprint": fp}])
        c = candidates[0]
        self.assertIsNotNone(c["known_wall_changed"])
        self.assertTrue(c["needs_review"])


class PersistenceBackwardCompatibilityTests(unittest.TestCase):
    def test_clean_wall_defaults_origin_manual_for_old_data(self):
        cleaned = L._vw_clean_wall({"name": "Old Wall", "rows": 2, "columns": 2})
        self.assertEqual(cleaned["origin"], "manual")
        self.assertIsNone(cleaned["confirmed_at"])
        self.assertEqual(cleaned["confirmed_fingerprint"], "")

    def test_clean_wall_preserves_detected_origin(self):
        cleaned = L._vw_clean_wall({
            "name": "Detected Wall", "rows": 2, "columns": 2,
            "origin": "detected", "confirmed_at": 1234, "confirmed_fingerprint": "abc123",
        })
        self.assertEqual(cleaned["origin"], "detected")
        self.assertEqual(cleaned["confirmed_at"], 1234)
        self.assertEqual(cleaned["confirmed_fingerprint"], "abc123")

    def test_load_video_wall_state_defaults_ignored_candidates(self):
        data = L._load_video_wall_state()
        self.assertIn("ignored_candidates", data)
        self.assertIsInstance(data["ignored_candidates"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
