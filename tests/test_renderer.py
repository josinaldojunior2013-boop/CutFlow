import unittest

from parser import TimelineProject, TimelineSegment
from renderer import MAX_RENDER_CLIPS, _validate_render_clip_limit, render_timeline


def make_segment(enabled: bool = True) -> TimelineSegment:
    return TimelineSegment(
        path=None,
        track_type="video",
        track_index=0,
        source_start=0,
        timeline_start=0,
        duration=1,
        volume=1,
        speed=1,
        enabled=enabled,
    )


class RenderClipLimitTests(unittest.TestCase):
    def test_allows_exactly_two_thousand_active_clips(self) -> None:
        segments = [make_segment()] * MAX_RENDER_CLIPS

        _validate_render_clip_limit(segments)

    def test_rejects_more_than_two_thousand_active_clips(self) -> None:
        segments = [make_segment()] * (MAX_RENDER_CLIPS + 1)

        with self.assertRaisesRegex(ValueError, "até 2.000 clipes ativos"):
            _validate_render_clip_limit(segments)

    def test_render_entry_point_rejects_over_limit_before_starting_ffmpeg(self) -> None:
        segments = [make_segment()] * (MAX_RENDER_CLIPS + 1)
        project = TimelineProject(segments, 1, 1920, 1080, 30)

        with self.assertRaisesRegex(ValueError, "Esta timeline tem 2.001"):
            render_timeline(project, "unused.mp4")

    def test_ignores_disabled_clips_in_limit(self) -> None:
        segments = [make_segment()] * MAX_RENDER_CLIPS
        segments.append(make_segment(enabled=False))

        _validate_render_clip_limit(segments)


if __name__ == "__main__":
    unittest.main()
