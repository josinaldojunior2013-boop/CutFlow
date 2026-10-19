import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from parser import TimelineProject, TimelineSegment
from renderer import (
    MAX_RENDER_CLIPS,
    _resolve_render_temp_directory,
    _validate_render_clip_limit,
    render_timeline,
)


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

    def test_resolves_user_selected_temporary_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                _resolve_render_temp_directory(directory),
                Path(directory).resolve(),
            )

    def test_rejects_missing_temporary_directory(self) -> None:
        with self.assertRaisesRegex(NotADirectoryError, "pasta temporária"):
            _resolve_render_temp_directory("missing-temp-path")

    def test_render_uses_selected_directory_for_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            temp_directory = root_path / "scratch"
            temp_directory.mkdir()
            media_path = root_path / "source.mp4"
            media_path.touch()
            segment = TimelineSegment(
                path=media_path,
                track_type="video",
                track_index=0,
                source_start=0,
                timeline_start=0,
                duration=1,
                volume=1,
                speed=1,
            )
            project = TimelineProject([segment], 1, 1920, 1080, 30)
            temp_roots: list[Path] = []

            def render_tracks(_project, temp_path, _ffmpeg, _progress):
                temp_roots.append(temp_path.parent)
                return [temp_path / "track.mov"]

            with (
                patch("renderer.resolve_ffmpeg", return_value="ffmpeg"),
                patch("renderer._render_video_tracks", side_effect=render_tracks),
                patch("renderer._run_stage"),
            ):
                render_timeline(
                    project,
                    root_path / "output.mp4",
                    temp_directory=temp_directory,
                )

            self.assertEqual(temp_roots, [temp_directory.resolve()])


if __name__ == "__main__":
    unittest.main()
