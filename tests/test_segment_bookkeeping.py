"""Segment bounds from splitmuxsink's reports (docs/REPLAY_SEAM_GLITCH.md §7.4 step 1).

`SegmentBookkeeper` has no GStreamer dependency; these tests drive it the
way `PipelineManager` does: `open_fragment` from `format-location-full`,
`note_buffer` from the encoder probe, `fragment_closed` from the bus.
"""

from __future__ import annotations

import os
import unittest

from app.media.segment_bookkeeping import FragmentRecord, SegmentBookkeeper

FRAME_NS = 33_366_700  # 29.97 fps


def _record(index: int, start_ns: int, *, folder: str = "C:/rec") -> FragmentRecord:
    return FragmentRecord(
        session_id="session_001",
        feed_id="ndi_1",
        fragment_index=index,
        file_path=f"{folder}/segment_{index:05d}.mkv",
        created_at="2026-11-11T00:00:00+00:00",
        start_running_time_ns=start_ns,
        start_pts_ns=start_ns,
        running_time_to_monotonic_ns=1_000_000_000,
    )


def _feed(keeper: SegmentBookkeeper, first_frame: int, last_frame: int) -> None:
    for k in range(first_frame, last_frame + 1):
        keeper.note_buffer(k * FRAME_NS)


class SegmentBookkeeperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keeper = SegmentBookkeeper()

    def test_segments_meet_exactly_and_count_frames_by_timestamp(self) -> None:
        # The encoder probe runs ahead of splitmuxsink's cut: frames 119
        # and 120 have passed it before segment 1 opens at frame 119.
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 120)
        self.assertIsNone(self.keeper.open_fragment(_record(1, 119 * FRAME_NS)))
        _feed(self.keeper, 121, 240)
        finished = self.keeper.fragment_closed("C:/rec/segment_00000.mkv", 119 * FRAME_NS)
        assert finished is not None
        self.assertEqual(finished.record.start_running_time_ns, 0)
        self.assertEqual(finished.end_running_time_ns, 119 * FRAME_NS)
        self.assertEqual(finished.frame_count, 119)
        self.assertEqual(finished.duration_ns, 119 * FRAME_NS)

        self.keeper.open_fragment(_record(2, 238 * FRAME_NS))
        second = self.keeper.fragment_closed("C:/rec/segment_00001.mkv", 238 * FRAME_NS)
        assert second is not None
        self.assertEqual(second.record.start_running_time_ns, finished.end_running_time_ns)
        self.assertEqual(second.frame_count, 119)

    def test_segment_waits_for_its_close_message(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 5)
        self.keeper.open_fragment(_record(1, 4 * FRAME_NS))
        self.assertIsNone(self.keeper.fragment_closed("C:/rec/segment_00001.mkv"))
        finished = self.keeper.fragment_closed("C:/rec/segment_00000.mkv")
        assert finished is not None
        self.assertEqual(finished.frame_count, 4)

    def test_close_message_before_rotation_finishes_on_open(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 5)
        self.assertIsNone(self.keeper.fragment_closed("C:/rec/segment_00000.mkv"))
        finished = self.keeper.open_fragment(_record(1, 4 * FRAME_NS))
        assert finished is not None
        self.assertEqual(finished.end_running_time_ns, 4 * FRAME_NS)
        self.assertEqual(finished.frame_count, 4)

    def test_duplicate_and_unknown_close_messages_are_ignored(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 5)
        self.keeper.open_fragment(_record(1, 4 * FRAME_NS))
        self.assertIsNotNone(self.keeper.fragment_closed("C:/rec/segment_00000.mkv"))
        self.assertIsNone(self.keeper.fragment_closed("C:/rec/segment_00000.mkv"))
        self.assertIsNone(self.keeper.fragment_closed("C:/elsewhere/unrouted.mkv"))

    def test_location_spelling_differences_still_match(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 5)
        self.keeper.open_fragment(_record(1, 4 * FRAME_NS))
        location = os.path.join("C:/rec", ".", "segment_00000.mkv")
        self.assertIsNotNone(self.keeper.fragment_closed(location))

    def test_mismatching_close_running_time_keeps_the_next_start(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 5)
        self.keeper.open_fragment(_record(1, 4 * FRAME_NS))
        with self.assertLogs("app.media.segment_bookkeeping", level="WARNING"):
            finished = self.keeper.fragment_closed(
                "C:/rec/segment_00000.mkv", 4 * FRAME_NS + 5_000_000
            )
        assert finished is not None
        self.assertEqual(finished.end_running_time_ns, 4 * FRAME_NS)

    def test_stop_after_split_drops_the_throwaway_and_drains_the_last(self) -> None:
        # disable_file_recording: split-now opens a throwaway segment;
        # splitmuxsink goes to NULL before the last close message is read.
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 50)
        self.keeper.open_fragment(_record(1, 48 * FRAME_NS))
        dropped = self.keeper.drop_current()
        assert dropped is not None
        self.assertEqual(dropped.fragment_index, 1)
        drained = self.keeper.finish_closing()
        self.assertEqual([f.record.fragment_index for f in drained], [0])
        self.assertEqual(drained[0].frame_count, 48)
        # The throwaway's own close message arrives later and is ignored.
        self.assertIsNone(self.keeper.fragment_closed("C:/rec/segment_00001.mkv"))
        self.assertEqual(self.keeper.finish_closing(), [])

    def test_finish_current_ends_one_frame_after_its_last_buffer(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 89)
        finished = self.keeper.finish_current(nominal_frame_duration_ns=40_000_000)
        assert finished is not None
        self.assertEqual(finished.frame_count, 90)
        self.assertEqual(finished.end_running_time_ns, 90 * FRAME_NS)

    def test_finish_current_with_one_buffer_uses_the_nominal_frame(self) -> None:
        self.keeper.open_fragment(_record(0, 1_000))
        self.keeper.note_buffer(1_000)
        finished = self.keeper.finish_current(nominal_frame_duration_ns=33_333_333)
        assert finished is not None
        self.assertEqual(finished.end_running_time_ns, 1_000 + 33_333_333)
        self.assertEqual(finished.frame_count, 1)

    def test_finish_current_without_buffers_returns_none(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        self.assertIsNone(self.keeper.finish_current(nominal_frame_duration_ns=33_333_333))
        self.assertIsNone(self.keeper.current())

    def test_old_buffer_timestamps_are_pruned(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 300)
        for i in range(1, 3):
            self.keeper.open_fragment(_record(i, i * 100 * FRAME_NS))
            self.keeper.fragment_closed(f"C:/rec/segment_{i - 1:05d}.mkv")
        self.assertLessEqual(len(self.keeper._buffer_running_times), 101)

    def test_running_time_going_backwards_starts_a_new_timeline(self) -> None:
        _feed(self.keeper, 100, 110)
        self.keeper.note_buffer(0)
        self.assertEqual(list(self.keeper._buffer_running_times), [0])

    def test_reset_forgets_everything(self) -> None:
        self.keeper.open_fragment(_record(0, 0))
        _feed(self.keeper, 0, 10)
        self.keeper.open_fragment(_record(1, 5 * FRAME_NS))
        self.keeper.reset()
        self.assertIsNone(self.keeper.current())
        self.assertEqual(self.keeper.finish_closing(), [])
        self.assertIsNone(self.keeper.fragment_closed("C:/rec/segment_00000.mkv"))


if __name__ == "__main__":
    unittest.main()
