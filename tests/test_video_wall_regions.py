"""
Video Wall spatial-routing region logic + CS31 eligibility tests.

Same hardware fence pattern as test_password_fallback.py (see that file's
module docstring for why the fence exists): no test here can reach a real
socket, websocket, or HTTP transport. These tests exercise pure logic
(_vw_clean_regions, _vw_effective_matrix_and_source, _vw_units_snapshot's
CS31 exclusion) against an in-memory cache_units, never a real device.

Run directly: python tests/test_video_wall_regions.py
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

_transport_attempts = []


def _hardware_fence_spy(name):
    def _fn(*args, **kwargs):
        _transport_attempts.append((name, args, kwargs))
        raise AssertionError(
            "HARDWARE FENCE TRIPPED: a test attempted a real '%s' call "
            "(args=%r kwargs=%r)." % (name, args, kwargs)
        )
    return _fn


_fence_patchers = [
    mock.patch("socket.create_connection", _hardware_fence_spy("socket.create_connection")),
    mock.patch("socket.socket", _hardware_fence_spy("socket.socket")),
]
for _p in _fence_patchers:
    _p.start()

try:
    import websocket as _websocket_module
    _ws_patcher = mock.patch.object(_websocket_module, "create_connection", _hardware_fence_spy("websocket.create_connection"))
    _ws_patcher.start()
    _fence_patchers.append(_ws_patcher)
except Exception:
    pass

try:
    import requests as _requests_module
    _req_post_patcher = mock.patch.object(_requests_module, "post", _hardware_fence_spy("requests.post"))
    _req_get_patcher = mock.patch.object(_requests_module, "get", _hardware_fence_spy("requests.get"))
    _req_post_patcher.start()
    _req_get_patcher.start()
    _fence_patchers.append(_req_post_patcher)
    _fence_patchers.append(_req_get_patcher)
except Exception:
    pass

import lumaserver  # noqa: E402


def _reset_cache():
    with lumaserver.cache_lock:
        lumaserver.cache_units.clear()


def _wall(rows=2, columns=2, source_id="", regions=None):
    cells = []
    for r in range(1, rows + 1):
        for c in range(1, columns + 1):
            cells.append({"row": r, "column": c, "decoder_id": f"dec-{r}-{c}",
                          "source_mode": "wall", "override_source_id": "", "rotation": 0})
    return {"id": "w1", "rows": rows, "columns": columns, "source_id": source_id,
            "regions": regions or [], "cells": cells}


class RegionCleaningTests(unittest.TestCase):
    """_vw_clean_regions: a region must be a genuine rectangle, in bounds,
    and no cell may belong to two regions."""

    def test_valid_two_cell_region_is_kept(self):
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": "enc1"}],
            rows=2, columns=2)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["source_id"], "enc1")
        self.assertEqual(regions[0]["cells"], [{"row": 1, "column": 1}, {"row": 1, "column": 2}])

    def test_single_cell_is_not_a_region(self):
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [{"row": 1, "column": 1}], "source_id": "enc1"}], rows=2, columns=2)
        self.assertEqual(regions, [])

    def test_non_rectangular_shape_is_rejected(self):
        # An L-shape: 3 cells that do not form a complete rows x columns rectangle.
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}, {"row": 2, "column": 1}],
              "source_id": "enc1"}], rows=2, columns=2)
        self.assertEqual(regions, [])

    def test_out_of_bounds_cell_is_dropped_from_consideration(self):
        # row 3 does not exist on a 2x2 wall -- the cell is discarded, leaving
        # only one cell, which is too few to be a region.
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 3, "column": 1}], "source_id": "enc1"}],
            rows=2, columns=2)
        self.assertEqual(regions, [])

    def test_center_region_is_the_full_2x2(self):
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2},
                                     {"row": 2, "column": 1}, {"row": 2, "column": 2}], "source_id": "enc1"}],
            rows=2, columns=2)
        self.assertEqual(len(regions), 1)
        self.assertEqual(len(regions[0]["cells"]), 4)

    def test_non_contiguous_rows_with_matching_cell_count_is_rejected(self):
        # Rows 1 and 3 (row 2 absent), 2 columns: 4 cells, rows_present=[1,3]
        # (len 2), cols_present=[1,2] (len 2), 2*2==4 -- the COUNT matches a
        # genuine 2x2 rectangle, but row 3 is not physically adjacent to row
        # 1. Must be rejected, not accepted as a valid rectangle spanning a
        # gap -- _vw_region_geometry() would otherwise tell row 3's decoder
        # it is "local row 2, directly below row 1" when it is not.
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [
                {"row": 1, "column": 1}, {"row": 1, "column": 2},
                {"row": 3, "column": 1}, {"row": 3, "column": 2}], "source_id": "enc1"}],
            rows=3, columns=2)
        self.assertEqual(regions, [])

    def test_non_contiguous_columns_with_matching_cell_count_is_rejected(self):
        # Same defect, transposed: columns 1 and 3 with column 2 absent.
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [
                {"row": 1, "column": 1}, {"row": 2, "column": 1},
                {"row": 1, "column": 3}, {"row": 2, "column": 3}], "source_id": "enc1"}],
            rows=2, columns=3)
        self.assertEqual(regions, [])

    def test_genuinely_contiguous_region_within_a_larger_wall_is_still_accepted(self):
        # Regression guard for the contiguity fix itself: a real, adjacent
        # rectangle elsewhere in a bigger wall must not be collaterally
        # rejected by the new check.
        regions = lumaserver._vw_clean_regions(
            [{"id": "r1", "cells": [
                {"row": 2, "column": 2}, {"row": 2, "column": 3},
                {"row": 3, "column": 2}, {"row": 3, "column": 3}], "source_id": "enc1"}],
            rows=4, columns=4)
        self.assertEqual(len(regions), 1)
        self.assertEqual(len(regions[0]["cells"]), 4)

    def test_overlapping_region_loses_the_shared_cell(self):
        # Two regions both claiming (1,1): the second, later one is rejected
        # wholesale rather than silently reassigning the cell.
        regions = lumaserver._vw_clean_regions([
            {"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": "enc1"},
            {"id": "r2", "cells": [{"row": 1, "column": 1}, {"row": 2, "column": 1}], "source_id": "enc2"},
        ], rows=2, columns=2)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["id"], "r1")

    def test_not_a_list_returns_empty(self):
        self.assertEqual(lumaserver._vw_clean_regions(None, 2, 2), [])
        self.assertEqual(lumaserver._vw_clean_regions("garbage", 2, 2), [])


class EffectiveGeometryTests(unittest.TestCase):
    """_vw_effective_matrix_and_source: region wins over source_mode; a
    region's own cell layout -- not the whole wall's -- decides the matrix."""

    def test_plain_wall_mode_uses_whole_wall_geometry(self):
        wall = _wall(source_id="wall-src")
        cell = wall["cells"][0]  # row 1, col 1
        rows, cols, row, col, enable, source_id = lumaserver._vw_effective_matrix_and_source(wall, cell)
        self.assertEqual((rows, cols, row, col, enable, source_id), (2, 2, 1, 1, True, "wall-src"))

    def test_override_mode_disables_wall_video(self):
        wall = _wall(source_id="wall-src")
        cell = wall["cells"][0]
        cell["source_mode"] = "override"
        cell["override_source_id"] = "override-src"
        rows, cols, row, col, enable, source_id = lumaserver._vw_effective_matrix_and_source(wall, cell)
        self.assertFalse(enable)
        self.assertEqual(source_id, "override-src")

    def test_region_wins_over_cells_own_wall_mode(self):
        # A "top boundary" region: R1C1 + R1C2, independent of the wall's
        # own 2x2 geometry -- each cell in it is a 1x2 sub-wall.
        region = {"id": "top", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": "region-src"}
        wall = _wall(source_id="wall-src", regions=[region])
        r1c1 = next(c for c in wall["cells"] if c["row"] == 1 and c["column"] == 1)
        r1c2 = next(c for c in wall["cells"] if c["row"] == 1 and c["column"] == 2)
        self.assertEqual(lumaserver._vw_effective_matrix_and_source(wall, r1c1), (1, 2, 1, 1, True, "region-src"))
        self.assertEqual(lumaserver._vw_effective_matrix_and_source(wall, r1c2), (1, 2, 1, 2, True, "region-src"))
        # Cells NOT in the region are unaffected.
        r2c1 = next(c for c in wall["cells"] if c["row"] == 2 and c["column"] == 1)
        self.assertEqual(lumaserver._vw_effective_matrix_and_source(wall, r2c1), (2, 2, 2, 1, True, "wall-src"))

    def test_left_boundary_region_geometry(self):
        region = {"id": "left", "cells": [{"row": 1, "column": 1}, {"row": 2, "column": 1}], "source_id": "region-src"}
        wall = _wall(regions=[region])
        r1c1 = next(c for c in wall["cells"] if c["row"] == 1 and c["column"] == 1)
        r2c1 = next(c for c in wall["cells"] if c["row"] == 2 and c["column"] == 1)
        self.assertEqual(lumaserver._vw_effective_matrix_and_source(wall, r1c1), (2, 1, 1, 1, True, "region-src"))
        self.assertEqual(lumaserver._vw_effective_matrix_and_source(wall, r2c1), (2, 1, 2, 1, True, "region-src"))

    def test_compare_cell_uses_region_geometry_not_whole_wall(self):
        region = {"id": "top", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": "region-src"}
        wall = _wall(regions=[region])
        r1c1 = next(c for c in wall["cells"] if c["row"] == 1 and c["column"] == 1)
        # The decoder reports the CORRECT region-relative matrix (1x2, pos 1) --
        # this must NOT be flagged as a diff even though it differs from the
        # wall's own 2x2.
        report = {"online": True, "video_wall": {"rows": 1, "columns": 2, "row": 1, "column": 1, "vmenable": True}, "rotation": 0}
        self.assertEqual(lumaserver._vw_compare_cell(wall, r1c1, report), [])
        # A decoder still reporting the OLD whole-wall matrix should show a diff.
        stale_report = {"online": True, "video_wall": {"rows": 2, "columns": 2, "row": 1, "column": 1, "vmenable": True}, "rotation": 0}
        self.assertTrue(lumaserver._vw_compare_cell(wall, r1c1, stale_report))


class Cs31EligibilityTests(unittest.TestCase):
    """CS31 switchers have no Video Wall capability and must never surface
    as a source, a decoder, or an apply target -- see PROJECT.md."""

    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def _seed(self, units):
        with lumaserver.cache_lock:
            for u in units:
                lumaserver.cache_units[u["ip"]] = dict(u)

    def test_cs31_excluded_from_both_encoder_and_decoder_lists(self):
        self._seed([
            {"ip": "10.0.0.1", "mac": "aa:1", "hostname": "enc1", "model": "HW-LUMA-E4211", "type": "encoder", "role": "Encoder"},
            {"ip": "10.0.0.2", "mac": "aa:2", "hostname": "dec1", "model": "HW-LUMA-D4211", "type": "decoder", "role": "Decoder"},
            {"ip": "10.0.0.3", "mac": "aa:3", "hostname": "switcher1", "model": "AT-OME-CS31", "type": "switcher", "role": "Decoder/Switcher"},
        ])
        snap = lumaserver._vw_units_snapshot()
        encoder_hosts = [e["hostname"] for e in snap["encoders"]]
        decoder_hosts = [d["hostname"] for d in snap["decoders"]]
        self.assertIn("enc1", encoder_hosts)
        self.assertIn("dec1", decoder_hosts)
        self.assertNotIn("switcher1", encoder_hosts)
        self.assertNotIn("switcher1", decoder_hosts)

    def test_cs31_reference_fails_safely_with_a_clear_reason(self):
        self._seed([{"ip": "10.0.0.3", "mac": "aa:3", "hostname": "switcher1", "model": "AT-OME-CS31",
                     "type": "switcher", "role": "Decoder/Switcher"}])
        device_id = lumaserver._vw_device_id({"ip": "10.0.0.3", "mac": "aa:3"})
        wall = _wall()
        wall["cells"][0]["decoder_id"] = device_id
        result = lumaserver._vw_apply_source_cell(wall, wall["cells"][0])
        self.assertFalse(result["ok"])
        self.assertIn("CS31", result["error"])
        # _vw_apply_source_cell returned before calling _vw_call at all (the
        # hardware fence installed at module scope would have raised
        # AssertionError immediately had it tried), so a CS31 reference is
        # refused without ever reaching a transport.

    def test_unknown_device_id_still_reads_as_not_discovered(self):
        reason = lumaserver._vw_cs31_reason("mac:doesnotexist")
        self.assertIsNone(reason)

    def test_non_cs31_decoder_is_unaffected(self):
        self._seed([{"ip": "10.0.0.2", "mac": "aa:2", "hostname": "dec1", "model": "HW-LUMA-D4211",
                     "type": "decoder", "role": "Decoder"}])
        device_id = lumaserver._vw_device_id({"ip": "10.0.0.2", "mac": "aa:2"})
        self.assertIsNone(lumaserver._vw_cs31_reason(device_id))


class WallInfoWriteTests(unittest.TestCase):
    """_vw_set_wall_info_if_needed: verified live against real hardware
    (PROJECT.md, 'StandardVideoWallInfo.Set write behavior') that a vwmatrix
    write made in the SAME call as vmenable False->True is not reliably
    applied -- the fix is a two-step write (prime at the decoder's current
    vwmatrix, then set the new one) for exactly that transition. Mocks
    lumaserver._vw_call directly (the one function that speaks to the
    transport for these calls), never anything closer to the socket."""

    def test_false_to_true_with_new_matrix_is_a_two_step_write(self):
        calls = []

        def fake_vw_call(ip, method, params=None, timeout=None):
            calls.append((method, dict(params or {})))
            if method == "StandardVideoWallInfo.Get":
                return {"ok": True, "result": {"vmenable": False, "vwmatrix": "2_2_1_1"}}
            return {"ok": True, "result": {"vmenable": params.get("vmenable"), "vwmatrix": params.get("vwmatrix")}}

        with mock.patch.object(lumaserver, "_vw_call", side_effect=fake_vw_call):
            result = lumaserver._vw_set_wall_info_if_needed("10.0.0.1", True, "2_2_1_2")

        self.assertTrue(result["ok"])
        set_calls = [c for c in calls if c[0] == "StandardVideoWallInfo.Set"]
        self.assertEqual(len(set_calls), 2, "expected a priming call plus the real write")
        self.assertEqual(set_calls[0][1], {"vmenable": True, "vwmatrix": "2_2_1_1"})  # prime at CURRENT matrix
        self.assertEqual(set_calls[1][1], {"vmenable": True, "vwmatrix": "2_2_1_2"})  # then the NEW matrix
        self.assertIn("primed", result)

    def test_already_enabled_is_a_single_write_no_priming(self):
        calls = []

        def fake_vw_call(ip, method, params=None, timeout=None):
            calls.append((method, dict(params or {})))
            if method == "StandardVideoWallInfo.Get":
                return {"ok": True, "result": {"vmenable": True, "vwmatrix": "2_2_1_1"}}
            return {"ok": True, "result": {"vmenable": params.get("vmenable"), "vwmatrix": params.get("vwmatrix")}}

        with mock.patch.object(lumaserver, "_vw_call", side_effect=fake_vw_call):
            result = lumaserver._vw_set_wall_info_if_needed("10.0.0.1", True, "2_2_1_2")

        set_calls = [c for c in calls if c[0] == "StandardVideoWallInfo.Set"]
        self.assertEqual(len(set_calls), 1)
        self.assertEqual(set_calls[0][1], {"vmenable": True, "vwmatrix": "2_2_1_2"})
        self.assertNotIn("primed", result)

    def test_disabling_is_a_single_write_no_priming(self):
        # The unreliable-write finding was specific to False->True; the
        # reverse direction is not primed.
        calls = []

        def fake_vw_call(ip, method, params=None, timeout=None):
            calls.append((method, dict(params or {})))
            if method == "StandardVideoWallInfo.Get":
                return {"ok": True, "result": {"vmenable": True, "vwmatrix": "2_2_1_1"}}
            return {"ok": True, "result": {"vmenable": params.get("vmenable"), "vwmatrix": params.get("vwmatrix")}}

        with mock.patch.object(lumaserver, "_vw_call", side_effect=fake_vw_call):
            result = lumaserver._vw_set_wall_info_if_needed("10.0.0.1", False, "2_2_1_1")

        set_calls = [c for c in calls if c[0] == "StandardVideoWallInfo.Set"]
        self.assertEqual(len(set_calls), 1)
        self.assertNotIn("primed", result)

    def test_false_to_true_with_same_matrix_needs_no_priming(self):
        # Nothing about the matrix is actually changing, so there is nothing
        # for the unreliable same-call write to lose -- a single call is
        # sufficient (and this case is also short-circuited earlier by the
        # already-correct "skipped" branch when vmenable ALSO already
        # matches; this covers the case where only vmenable differs).
        calls = []

        def fake_vw_call(ip, method, params=None, timeout=None):
            calls.append((method, dict(params or {})))
            if method == "StandardVideoWallInfo.Get":
                return {"ok": True, "result": {"vmenable": False, "vwmatrix": "2_2_1_1"}}
            return {"ok": True, "result": {"vmenable": params.get("vmenable"), "vwmatrix": params.get("vwmatrix")}}

        with mock.patch.object(lumaserver, "_vw_call", side_effect=fake_vw_call):
            result = lumaserver._vw_set_wall_info_if_needed("10.0.0.1", True, "2_2_1_1")

        set_calls = [c for c in calls if c[0] == "StandardVideoWallInfo.Set"]
        self.assertEqual(len(set_calls), 1)
        self.assertNotIn("primed", result)

    def test_get_failure_falls_back_to_single_write(self):
        # If the pre-read itself fails, we don't know the "current" matrix to
        # prime with -- fall back to the pre-existing single-call behavior
        # rather than guessing.
        calls = []

        def fake_vw_call(ip, method, params=None, timeout=None):
            calls.append((method, dict(params or {})))
            if method == "StandardVideoWallInfo.Get":
                return {"ok": False, "error": "no response"}
            return {"ok": True, "result": {"vmenable": params.get("vmenable"), "vwmatrix": params.get("vwmatrix")}}

        with mock.patch.object(lumaserver, "_vw_call", side_effect=fake_vw_call):
            result = lumaserver._vw_set_wall_info_if_needed("10.0.0.1", True, "2_2_1_2")

        set_calls = [c for c in calls if c[0] == "StandardVideoWallInfo.Set"]
        self.assertEqual(len(set_calls), 1)
        self.assertNotIn("primed", result)
        self.assertEqual(result.get("current_check_error"), "no response")


class ApplySourceCellSkipTests(unittest.TestCase):
    """_vw_apply_source_cell: a plain "wall"-mode cell whose WALL has no
    source_id yet is a normal, valid state (a freshly created wall with no
    Wall Source chosen) -- must be a benign skip, ok=True, not a failure,
    matching the behavior from before regions existed. An override cell
    with no override_source_id, or a region with no source_id, must remain
    a hard failure exactly as always -- both only ever get a source_id from
    an explicit operator action, so an empty one there means something is
    actually wrong, not merely "not configured yet"."""

    def setUp(self):
        _reset_cache()
        with lumaserver.cache_lock:
            lumaserver.cache_units["10.0.0.5"] = {
                "ip": "10.0.0.5", "mac": "02:00:5e:30:00:05", "hostname": "dec1",
                "model": "HW-LUMA-D4211", "type": "decoder", "role": "Decoder",
            }
        self.decoder_id = lumaserver._vw_device_id({"ip": "10.0.0.5", "mac": "02:00:5e:30:00:05"})
        # The unit at that address says who it is, as a real one does; the hard-failure tests
        # below must fail for the reason they name, not because the identity was unproven.
        patcher = mock.patch.object(lumaserver, "_identify_for_route", lambda ip: "02:00:5e:30:00:05")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        _reset_cache()

    @staticmethod
    def _fake_vw_call(ip, method, params=None, timeout=None):
        if method == "StandardVideoWallInfo.Get":
            return {"ok": True, "result": {"vmenable": True, "vwmatrix": "2_2_1_1"}}
        return {"ok": True, "result": {}}

    def test_plain_wall_mode_with_no_wall_source_is_a_benign_skip(self):
        wall = _wall(source_id="")  # no Wall Source chosen yet
        cell = wall["cells"][0]
        cell["decoder_id"] = self.decoder_id
        with mock.patch.object(lumaserver, "_vw_call", side_effect=self._fake_vw_call):
            result = lumaserver._vw_apply_source_cell(wall, cell)
        self.assertTrue(result["ok"], "a wall with no source chosen yet must not report failure")
        source_step = next(s for s in result["steps"] if s["step"] == "route wall source")
        self.assertTrue(source_step.get("skipped"))

    def test_override_mode_with_no_override_source_is_still_a_hard_failure(self):
        wall = _wall(source_id="wall-src")
        cell = wall["cells"][0]
        cell["decoder_id"] = self.decoder_id
        cell["source_mode"] = "override"
        cell["override_source_id"] = ""  # never explicitly set
        with mock.patch.object(lumaserver, "_vw_call", side_effect=self._fake_vw_call):
            result = lumaserver._vw_apply_source_cell(wall, cell)
        self.assertFalse(result["ok"], "an override cell with no override source must still fail")

    def test_region_with_no_source_is_still_a_hard_failure(self):
        region = {"id": "top", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": ""}
        wall = _wall(source_id="wall-src", regions=[region])
        cell = wall["cells"][0]
        cell["decoder_id"] = self.decoder_id
        with mock.patch.object(lumaserver, "_vw_call", side_effect=self._fake_vw_call):
            result = lumaserver._vw_apply_source_cell(wall, cell)
        self.assertFalse(result["ok"],
                          "a region with no source configured must still fail, unlike a plain unset wall source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
