"""
Video Wall detection/inference tests (_vw_detect_walls) -- pure grouping
and classification logic against synthetic decoder state. Same hardware
fence pattern as test_password_fallback.py / test_video_wall_regions.py:
no test here can reach a real socket, websocket, or HTTP transport.

The synthetic states below mirror the ACTUAL shape real hardware returned
during live verification (see PROJECT.md, "Video Wall: detecting an
existing configuration") -- vwmatrix as "rows_columns_row_column",
vmenable independent of coordinate correctness, streamname not
necessarily uniform across a wall's members.

Run directly: python tests/test_video_wall_detection.py
Or via discovery: python -m unittest discover -s tests -p "test_*.py"
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


def decoder(device_id, row, column, rows=2, columns=2, vmenable=True, streamname="wall-src",
            rotation=0, online=True):
    return {
        "device_id": device_id,
        "hostname": device_id,
        "online": online,
        "video_wall": {"rows": rows, "columns": columns, "row": row, "column": column, "vmenable": vmenable},
        "rotation": rotation,
        "source": streamname,
    }


class KnownGoodTwoByTwoTests(unittest.TestCase):
    """Mirrors the real hardware finding exactly: vmenable and streamname
    are NOT uniform across the wall, and detection must not care."""

    def test_complete_2x2_detected_despite_mixed_vmenable_and_source(self):
        states = [
            decoder("ul", 1, 1, vmenable=False, streamname="stream-main", rotation=180),
            decoder("ur", 1, 2, vmenable=True, streamname="stream-main", rotation=180),
            decoder("ll", 2, 1, vmenable=True, streamname="room-a", rotation=0),
            decoder("lr", 2, 2, vmenable=False, streamname="stream-main", rotation=0),
        ]
        result = L._vw_detect_walls(states)
        self.assertEqual(len(result["walls"]), 1)
        wall = result["walls"][0]
        self.assertEqual(wall["status"], "complete")
        self.assertEqual((wall["rows"], wall["columns"]), (2, 2))
        self.assertEqual(wall["member_count"], 4)
        self.assertEqual(wall["missing"], [])
        self.assertEqual(wall["conflicts"], [])
        self.assertEqual(wall["members"]["1:1"]["device_id"], "ul")
        self.assertEqual(wall["members"]["1:2"]["device_id"], "ur")
        self.assertEqual(wall["members"]["2:1"]["device_id"], "ll")
        self.assertEqual(wall["members"]["2:2"]["device_id"], "lr")
        self.assertEqual(result["unassigned"], [])
        self.assertEqual(result["invalid"], [])

    def test_discovery_order_does_not_affect_result(self):
        states = [
            decoder("lr", 2, 2), decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1),
        ]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["walls"][0]["status"], "complete")
        self.assertEqual(result["walls"][0]["members"]["1:1"]["device_id"], "ul")


class PartialWallTests(unittest.TestCase):
    def test_three_of_four_is_partial_with_correct_missing_coordinate(self):
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1)]
        result = L._vw_detect_walls(states)
        wall = result["walls"][0]
        self.assertEqual(wall["status"], "partial")
        self.assertEqual(wall["member_count"], 3)
        self.assertEqual(wall["expected_count"], 4)
        self.assertEqual(wall["missing"], [{"row": 2, "column": 2}])


class ConflictTests(unittest.TestCase):
    def test_duplicate_coordinate_is_a_conflict_naming_both_claimants(self):
        states = [
            decoder("ul", 1, 1), decoder("ur", 1, 2),
            decoder("ll", 2, 1), decoder("lr_evil_twin", 2, 1),  # duplicate of ll's (2,1)
        ]
        result = L._vw_detect_walls(states)
        wall = result["walls"][0]
        self.assertEqual(wall["status"], "conflict")
        self.assertEqual(len(wall["conflicts"]), 1)
        conflict = wall["conflicts"][0]
        self.assertEqual((conflict["row"], conflict["column"]), (2, 1))
        self.assertEqual(set(conflict["claimants"]), {"ll", "lr_evil_twin"})
        # The conflicting coordinate never silently resolves into "members".
        self.assertNotIn("2:1", wall["members"])

    def test_conflict_does_not_silently_pick_a_winner(self):
        states = [decoder("a", 1, 1), decoder("b", 1, 1)]
        result = L._vw_detect_walls(states)
        wall = result["walls"][0]
        self.assertEqual(wall["status"], "conflict")
        self.assertEqual(wall["member_count"], 0)


class InvalidCoordinateTests(unittest.TestCase):
    def test_out_of_range_row_is_invalid_not_forced_into_a_group(self):
        states = [decoder("ul", 1, 1), decoder("ur", 1, 2), decoder("ll", 2, 1),
                  decoder("bad", 3, 1, rows=2, columns=2)]  # row 3 of a claimed 2x2
        result = L._vw_detect_walls(states)
        self.assertEqual(len(result["invalid"]), 1)
        self.assertEqual(result["invalid"][0]["device_id"], "bad")
        # The other three still form a clean partial wall, unaffected.
        wall = result["walls"][0]
        self.assertEqual(wall["status"], "partial")
        self.assertEqual(wall["member_count"], 3)

    def test_zero_or_negative_coordinate_is_unassigned(self):
        states = [decoder("zero_row", 0, 1)]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["walls"], [])
        self.assertEqual(len(result["unassigned"]), 1)


class DimensionMismatchTests(unittest.TestCase):
    def test_devices_claiming_different_wall_sizes_never_merge(self):
        states = [
            decoder("ul", 1, 1, rows=2, columns=2), decoder("ur", 1, 2, rows=2, columns=2),
            decoder("ll", 2, 1, rows=2, columns=2), decoder("lr", 2, 2, rows=2, columns=2),
            decoder("odd_one_out", 1, 1, rows=1, columns=3),  # claims a 1x3, not the 2x2
        ]
        result = L._vw_detect_walls(states)
        self.assertEqual(len(result["walls"]), 2)
        sizes = sorted((w["rows"], w["columns"]) for w in result["walls"])
        self.assertEqual(sizes, [(1, 3), (2, 2)])
        two_by_two = next(w for w in result["walls"] if (w["rows"], w["columns"]) == (2, 2))
        self.assertEqual(two_by_two["status"], "complete")
        # The 1x3 claimant is alone in its own group -- a single member of a
        # multi-cell wall is legitimately "partial", not merged or dropped.
        one_by_three = next(w for w in result["walls"] if (w["rows"], w["columns"]) == (1, 3))
        self.assertEqual(one_by_three["status"], "partial")
        self.assertEqual(one_by_three["member_count"], 1)


class StandaloneDecoderTests(unittest.TestCase):
    def test_lone_1x1_wall_is_standalone_not_a_wall(self):
        states = [decoder("solo", 1, 1, rows=1, columns=1)]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["walls"], [])
        self.assertEqual(len(result["unassigned"]), 1)
        self.assertEqual(result["unassigned"][0]["device_id"], "solo")

    def test_decoder_with_no_video_wall_info_is_unassigned(self):
        states = [{"device_id": "never-configured", "online": True, "video_wall": {}}]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["walls"], [])
        self.assertEqual(len(result["unassigned"]), 1)

    def test_missing_video_wall_key_entirely_is_unassigned(self):
        states = [{"device_id": "offline-or-unreadable", "online": False}]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["unassigned"][0]["device_id"], "offline-or-unreadable")


class MultipleWallsTests(unittest.TestCase):
    def test_two_independent_walls_of_different_sizes_plus_a_standalone(self):
        states = [
            decoder("ul", 1, 1, rows=2, columns=2), decoder("ur", 1, 2, rows=2, columns=2),
            decoder("ll", 2, 1, rows=2, columns=2), decoder("lr", 2, 2, rows=2, columns=2),
            decoder("left", 1, 1, rows=1, columns=2), decoder("right", 1, 2, rows=1, columns=2),
            decoder("solo", 1, 1, rows=1, columns=1),
        ]
        result = L._vw_detect_walls(states)
        self.assertEqual(len(result["walls"]), 2)
        statuses = {(w["rows"], w["columns"]): w["status"] for w in result["walls"]}
        self.assertEqual(statuses, {(2, 2): "complete", (1, 2): "complete"})
        self.assertEqual(len(result["unassigned"]), 1)
        self.assertEqual(result["unassigned"][0]["device_id"], "solo")


class MalformedFieldTests(unittest.TestCase):
    def test_non_integer_coordinate_is_unassigned_not_a_crash(self):
        states = [{"device_id": "weird", "online": True,
                   "video_wall": {"rows": 2, "columns": 2, "row": "1", "column": 1, "vmenable": True}}]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["walls"], [])
        self.assertEqual(len(result["unassigned"]), 1)

    def test_boolean_coordinate_is_unassigned_not_treated_as_1(self):
        # Python's bool is an int subclass -- True must not silently pass as row=1.
        states = [{"device_id": "weird", "online": True,
                   "video_wall": {"rows": 2, "columns": 2, "row": True, "column": 1, "vmenable": True}}]
        result = L._vw_detect_walls(states)
        self.assertEqual(len(result["unassigned"]), 1)

    def test_empty_input_returns_empty_result(self):
        result = L._vw_detect_walls([])
        self.assertEqual(result, {"walls": [], "unassigned": [], "invalid": []})


class Cs31ExclusionFromDetectionTests(unittest.TestCase):
    """CS31 must never reach _vw_detect_walls at all -- enforced upstream
    by _vw_units_snapshot (see test_video_wall_regions.py); this proves
    the detection function itself has no special knowledge of device
    model and would happily group a CS31-shaped entry if one were ever
    (incorrectly) handed to it, which is exactly why the exclusion must
    happen before this function is ever called, at the single choke
    point -- not duplicated defensively here as a second safeguard that
    could quietly drift out of sync with the real one."""

    def test_detection_has_no_cs31_awareness_by_design(self):
        # A CS31-shaped entry with a plausible-looking video_wall block
        # would be grouped like any other device -- proving the real
        # protection is upstream, in _vw_units_snapshot's _vw_is_cs31
        # filter, not here.
        states = [decoder("would-be-cs31", 1, 1, rows=1, columns=1)]
        result = L._vw_detect_walls(states)
        self.assertEqual(result["unassigned"][0]["device_id"], "would-be-cs31")


if __name__ == "__main__":
    unittest.main(verbosity=2)
