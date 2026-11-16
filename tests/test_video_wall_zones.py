"""
Video Wall saved-zone-definition tests (_vw_clean_zones/_vw_zone_cells/
_vw_zone_label) -- pure logic, same hardware fence pattern as
test_video_wall_regions.py: no test here can reach a real socket,
websocket, or HTTP transport.

Zones are a SEPARATE, deliberately distinct concept from wall.regions:
- wall.regions = live/active routing state (an in-progress source
  assignment spanning >1 cell); cleared by "Apply to Entire Wall";
  cells may never overlap between two regions.
- wall.zones = saved, reusable RECTANGLE DEFINITIONS an operator can route
  a source to later; they carry no source_id, trigger no device call by
  existing, and MAY overlap each other freely (e.g. "Top Row" and
  "Left 2x2" on the same wall).

Run directly: python tests/test_video_wall_zones.py
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


def _zone(zid, row1, column1, row2, column2, name=""):
    return {"id": zid, "name": name, "row1": row1, "column1": column1, "row2": row2, "column2": column2}


class ZoneCleaningTests(unittest.TestCase):
    def test_valid_multi_cell_zone_is_kept(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 1, 2, "Top Pair")], rows=2, columns=2)
        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["name"], "Top Pair")
        self.assertEqual((zones[0]["row1"], zones[0]["column1"], zones[0]["row2"], zones[0]["column2"]), (1, 1, 1, 2))

    def test_single_cell_zone_is_dropped(self):
        # A 1x1 "zone" is redundant -- every cell is already independently
        # routable without one.
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 1, 1)], rows=2, columns=2)
        self.assertEqual(zones, [])

    def test_out_of_bounds_zone_is_dropped(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 5, 5)], rows=3, columns=3)
        self.assertEqual(zones, [])

    def test_reversed_corners_are_normalized(self):
        # A zone drawn bottom-right-to-top-left must normalize the same as
        # top-left-to-bottom-right.
        zones = lumaserver._vw_clean_zones([_zone("z1", 2, 2, 1, 1)], rows=2, columns=2)
        self.assertEqual(len(zones), 1)
        self.assertEqual((zones[0]["row1"], zones[0]["column1"], zones[0]["row2"], zones[0]["column2"]), (1, 1, 2, 2))

    def test_overlapping_zones_are_BOTH_kept_unlike_regions(self):
        zones = lumaserver._vw_clean_zones([
            _zone("top", 1, 1, 1, 3, "Top Row"),
            _zone("left", 1, 1, 2, 2, "Left 2x2"),
            _zone("all", 1, 1, 3, 3, "Entire Wall"),
        ], rows=3, columns=3)
        self.assertEqual(len(zones), 3)
        self.assertEqual({z["id"] for z in zones}, {"top", "left", "all"})

    def test_duplicate_ids_get_a_fresh_id_not_dropped(self):
        zones = lumaserver._vw_clean_zones([
            _zone("dup", 1, 1, 1, 2),
            _zone("dup", 2, 1, 2, 2),
        ], rows=2, columns=2)
        self.assertEqual(len(zones), 2)
        self.assertEqual(len({z["id"] for z in zones}), 2)

    def test_not_a_list_returns_empty(self):
        self.assertEqual(lumaserver._vw_clean_zones(None, 2, 2), [])
        self.assertEqual(lumaserver._vw_clean_zones("garbage", 2, 2), [])
        self.assertEqual(lumaserver._vw_clean_zones([{"not": "a zone"}], 2, 2), [])

    def test_missing_name_defaults_to_empty_string_not_none(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 2, 2)], rows=2, columns=2)
        self.assertEqual(zones[0]["name"], "")


class ZoneDimensionChangeTests(unittest.TestCase):
    """On a wall resize, a zone whose rectangle still fits survives
    unchanged; one that no longer fits is dropped by the cleaning pipeline
    -- the UI layer is responsible for warning the operator BEFORE calling
    resize with dimensions that would do this (see ui/wall.js
    zonesInvalidatedByResize()), but the backend must never silently keep
    an out-of-bounds zone either way."""

    def test_zone_within_new_smaller_bounds_survives(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 2, 2)], rows=3, columns=3)
        # Simulate shrinking the wall from 3x3 to 2x2: re-clean at the new bounds.
        reclean = lumaserver._vw_clean_zones(zones, rows=2, columns=2)
        self.assertEqual(len(reclean), 1)

    def test_zone_outside_new_smaller_bounds_is_dropped(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 3, 3)], rows=3, columns=3)
        reclean = lumaserver._vw_clean_zones(zones, rows=2, columns=2)
        self.assertEqual(reclean, [])

    def test_zone_survives_wall_growth(self):
        zones = lumaserver._vw_clean_zones([_zone("z1", 1, 1, 2, 2)], rows=2, columns=2)
        reclean = lumaserver._vw_clean_zones(zones, rows=4, columns=4)
        self.assertEqual(len(reclean), 1)


class ZoneCellsAndLabelTests(unittest.TestCase):
    """Member cells and the automatic fallback label are always DERIVED
    from the canonical rectangle -- covering representative sizes from the
    task spec's 1x1-8x8 range."""

    def test_cells_derivation_various_sizes(self):
        cases = [
            (_zone("z", 1, 1, 1, 2), [(1, 1), (1, 2)]),                     # 1x2
            (_zone("z", 1, 1, 2, 2), [(1, 1), (1, 2), (2, 1), (2, 2)]),      # 2x2
            (_zone("z", 2, 2, 3, 3), [(2, 2), (2, 3), (3, 2), (3, 3)]),      # 2x2 not at origin
            (_zone("z", 1, 1, 1, 8), [(1, c) for c in range(1, 9)]),         # 1x8 edge case
        ]
        for zone, expected in cases:
            cells = [(c["row"], c["column"]) for c in lumaserver._vw_zone_cells(zone)]
            self.assertEqual(cells, expected)

    def test_label_uses_custom_name_when_present(self):
        zone = _zone("z", 1, 1, 1, 3, name="Top Row")
        self.assertEqual(lumaserver._vw_zone_label(zone), "Top Row")

    def test_label_falls_back_to_dimensions_and_range(self):
        zone = _zone("z", 1, 1, 1, 3)
        label = lumaserver._vw_zone_label(zone)
        self.assertIn("1x3", label)
        self.assertIn("R1C1", label)
        self.assertIn("R1C3", label)

    def test_label_fallback_for_bottom_right_zone(self):
        zone = _zone("z", 3, 3, 4, 4)
        label = lumaserver._vw_zone_label(zone)
        self.assertIn("2x2", label)
        self.assertIn("R3C3", label)
        self.assertIn("R4C4", label)

    def test_label_never_required_empty_name_still_produces_text(self):
        zone = _zone("z", 1, 1, 8, 8, name="")
        label = lumaserver._vw_zone_label(zone)
        self.assertTrue(label)
        self.assertIn("8x8", label)


class ZoneWallIntegrationTests(unittest.TestCase):
    """Zones ride through the existing _vw_clean_wall/_vw_enrich_wall
    pipeline exactly like regions do -- no new persistence mechanism, and
    a wall saved before zones existed must still load cleanly."""

    def test_clean_wall_includes_zones_field(self):
        wall = {"id": "w1", "rows": 3, "columns": 3,
                "zones": [_zone("z1", 1, 1, 1, 3, "Top Row")]}
        cleaned = lumaserver._vw_clean_wall(wall)
        self.assertIn("zones", cleaned)
        self.assertEqual(len(cleaned["zones"]), 1)

    def test_wall_with_no_zones_key_loads_with_empty_zones(self):
        wall = {"id": "w1", "rows": 2, "columns": 2}
        cleaned = lumaserver._vw_clean_wall(wall)
        self.assertEqual(cleaned["zones"], [])

    def test_zones_and_regions_are_independent_fields(self):
        wall = {
            "id": "w1", "rows": 2, "columns": 2,
            "regions": [{"id": "r1", "cells": [{"row": 1, "column": 1}, {"row": 1, "column": 2}], "source_id": "enc1"}],
            "zones": [_zone("z1", 1, 1, 2, 1, "Left Pair")],
        }
        cleaned = lumaserver._vw_clean_wall(wall)
        self.assertEqual(len(cleaned["regions"]), 1)
        self.assertEqual(len(cleaned["zones"]), 1)
        self.assertNotEqual(cleaned["regions"][0]["id"], cleaned["zones"][0]["id"])

    def test_enrich_wall_preserves_zones(self):
        wall = {"id": "w1", "rows": 2, "columns": 2, "zones": [_zone("z1", 1, 1, 1, 2, "Top Pair")], "cells": []}
        enriched = lumaserver._vw_enrich_wall(wall)
        self.assertEqual(len(enriched["zones"]), 1)
        self.assertEqual(enriched["zones"][0]["name"], "Top Pair")


if __name__ == "__main__":
    unittest.main(verbosity=2)
