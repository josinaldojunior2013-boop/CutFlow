import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from parser import (
    RelinkCancelledError,
    TimelineSegment,
    extract_timeline,
    relink_media_in_folder,
)
from parser import link_panel_shadows


def make_segment(
    *,
    path: Path | None,
    track_index: int,
    track_name: str,
    media_id: str,
    media_name: str,
    media_kind: str,
    raw_path: str = "",
) -> TimelineSegment:
    return TimelineSegment(
        path=path,
        track_type="video",
        track_index=track_index,
        source_start=0,
        timeline_start=0,
        duration=2,
        volume=1,
        speed=1,
        media_id=media_id,
        media_name=media_name,
        media_kind=media_kind,
        track_name=track_name,
        raw_path=raw_path,
    )


class PanelShadowRelinkingTests(unittest.TestCase):
    def test_extract_links_source_less_shadow_to_matching_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_dir = Path(temporary_directory)
            panel_path = project_dir / "Resources" / "local" / "panel.jpg"
            panel_path.parent.mkdir(parents=True)
            panel_path.touch()
            draft_path = project_dir / "draft_content.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "duration": 2_000_000,
                        "fps": 30,
                        "materials": {
                            "videos": [
                                {
                                    "id": "shadow-id",
                                    "path": "",
                                    "material_name": "Painel sombra 1",
                                    "type": "video",
                                },
                                {
                                    "id": "panel-id",
                                    "path": (
                                        "##_draftpath_placeholder_project_##/"
                                        "Resources/local/panel.jpg"
                                    ),
                                    "material_name": "panel.jpg",
                                    "type": "photo",
                                },
                            ]
                        },
                        "tracks": [
                            {
                                "type": "video",
                                "name": "Panel Shadow",
                                "segments": [
                                    {
                                        "material_id": "shadow-id",
                                        "source_timerange": {
                                            "start": 0,
                                            "duration": 2_000_000,
                                        },
                                        "target_timerange": {
                                            "start": 0,
                                            "duration": 2_000_000,
                                        },
                                    }
                                ],
                            },
                            {
                                "type": "video",
                                "name": "Panels",
                                "segments": [
                                    {
                                        "material_id": "panel-id",
                                        "source_timerange": {
                                            "start": 0,
                                            "duration": 2_000_000,
                                        },
                                        "target_timerange": {
                                            "start": 0,
                                            "duration": 2_000_000,
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            project = extract_timeline(draft_path)

            self.assertEqual(project.segments[0].path, panel_path.resolve())
            self.assertEqual(project.segments[1].path, panel_path.resolve())

    def test_bulk_relink_propagates_panel_file_to_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            media_dir = Path(temporary_directory)
            panel_path = media_dir / "panel-01.jpg"
            panel_path.touch()
            segments = [
                make_segment(
                    path=media_dir / "stale-panel-link.jpg",
                    track_index=1,
                    track_name="Panel Shadow",
                    media_id="shadow-id",
                    media_name="Painel sombra 1",
                    media_kind="video",
                ),
                make_segment(
                    path=None,
                    track_index=2,
                    track_name="Panels",
                    media_id="panel-id",
                    media_name="panel-01.jpg",
                    media_kind="photo",
                    raw_path="##_draftpath_placeholder_project_##/Resources/local/hash.jpg",
                ),
            ]

            linked_segments, summary = relink_media_in_folder(segments, media_dir)

            self.assertEqual(linked_segments[0].path, panel_path.resolve())
            self.assertEqual(linked_segments[1].path, panel_path.resolve())
            self.assertEqual(summary.linked_materials, 1)
            self.assertEqual(summary.missing_materials, 0)

    def test_bulk_relink_reports_scan_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            media_dir = Path(temporary_directory)
            (media_dir / "clip.mp4").touch()
            progress: list[tuple[int, int]] = []
            segment = make_segment(
                path=None,
                track_index=0,
                track_name="Video",
                media_id="video-id",
                media_name="clip.mp4",
                media_kind="video",
            )

            linked, summary = relink_media_in_folder(
                [segment],
                media_dir,
                progress_callback=lambda dirs, files: progress.append((dirs, files)),
            )

            self.assertEqual(linked[0].path, (media_dir / "clip.mp4").resolve())
            self.assertEqual(summary.linked_materials, 1)
            self.assertEqual(progress, [(1, 1)])

    def test_bulk_relink_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cancel_event = Event()
            cancel_event.set()

            with self.assertRaises(RelinkCancelledError):
                relink_media_in_folder(
                    [],
                    temporary_directory,
                    cancel_event=cancel_event,
                )

    def test_manual_shadow_relink_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            media_dir = Path(temporary_directory)
            manually_selected_path = media_dir / "manual-shadow.png"
            manually_selected_path.touch()
            panel_path = media_dir / "panel.jpg"

            linked_segments = link_panel_shadows(
                [
                    make_segment(
                        path=manually_selected_path,
                        track_index=1,
                        track_name="Panel Shadow",
                        media_id="shadow-id",
                        media_name="Painel sombra 1",
                        media_kind="video",
                    ),
                    make_segment(
                        path=panel_path,
                        track_index=2,
                        track_name="Panels",
                        media_id="panel-id",
                        media_name="panel.jpg",
                        media_kind="photo",
                    ),
                ]
            )

            self.assertEqual(linked_segments[0].path, manually_selected_path)


if __name__ == "__main__":
    unittest.main()
