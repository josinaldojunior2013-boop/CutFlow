from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from parser import extract_timeline
from project_io import save_capcut_draft


class CapCutDraftSaveTests(unittest.TestCase):
    def test_saves_edits_preserves_unknown_fields_and_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            media_path = root / "Resources" / "local" / "video.mp4"
            media_path.parent.mkdir(parents=True)
            media_path.touch()
            draft_path = root / "draft_content.json"
            original_draft = {
                "duration": 5_000_000,
                "fps": 30,
                "custom_project_setting": {"keep": True},
                "materials": {
                    "videos": [
                        {
                            "id": "video-id",
                            "path": (
                                "##_draftpath_placeholder_project_##/"
                                "Resources/local/video.mp4"
                            ),
                            "material_name": "video.mp4",
                            "type": "video",
                            "custom_material_data": "preserve",
                        }
                    ]
                },
                "tracks": [
                    {
                        "type": "video",
                        "name": "Main",
                        "custom_track_data": 42,
                        "segments": [
                            {
                                "material_id": "video-id",
                                "source_timerange": {"start": 0, "duration": 2_000_000},
                                "target_timerange": {"start": 0, "duration": 2_000_000},
                                "volume": 1,
                                "extra_material_refs": ["effect-id"],
                                "custom_segment_data": "keep-me",
                            },
                            {
                                "material_id": "video-id",
                                "source_timerange": {
                                    "start": 2_000_000,
                                    "duration": 3_000_000,
                                },
                                "target_timerange": {
                                    "start": 2_000_000,
                                    "duration": 3_000_000,
                                },
                                "volume": 1,
                            },
                        ],
                    }
                ],
            }
            original_bytes = json.dumps(
                original_draft, ensure_ascii=False
            ).encode("utf-8")
            draft_path.write_bytes(original_bytes)

            project = extract_timeline(draft_path)
            self.assertEqual(
                [item.source_segment_index for item in project.segments], [0, 1]
            )
            project.segments[0] = replace(
                project.segments[0],
                source_start=0.25,
                timeline_start=0.5,
                duration=1,
                volume=0.5,
                enabled=False,
            )

            backup_path = save_capcut_draft(draft_path, project)

            self.assertEqual(backup_path.read_bytes(), original_bytes)
            self.assertTrue(backup_path.name.startswith("draft_content.json."))
            saved = json.loads(draft_path.read_text(encoding="utf-8"))
            edited_clip = saved["tracks"][0]["segments"][0]
            untouched_clip = saved["tracks"][0]["segments"][1]
            self.assertEqual(edited_clip["source_timerange"]["start"], 250_000)
            self.assertEqual(edited_clip["source_timerange"]["duration"], 1_000_000)
            self.assertEqual(edited_clip["target_timerange"]["start"], 500_000)
            self.assertEqual(edited_clip["target_timerange"]["duration"], 1_000_000)
            self.assertEqual(edited_clip["volume"], 0.5)
            self.assertEqual(
                saved["materials"]["videos"][0]["path"], str(media_path.resolve())
            )
            self.assertFalse(edited_clip["visible"])
            self.assertEqual(edited_clip["custom_segment_data"], "keep-me")
            self.assertEqual(edited_clip["extra_material_refs"], ["effect-id"])
            self.assertEqual(
                untouched_clip["source_timerange"],
                original_draft["tracks"][0]["segments"][1]["source_timerange"],
            )
            self.assertEqual(
                untouched_clip["target_timerange"],
                original_draft["tracks"][0]["segments"][1]["target_timerange"],
            )
            self.assertEqual(untouched_clip["volume"], 1)
            self.assertNotIn("visible", untouched_clip)
            self.assertTrue(saved["custom_project_setting"]["keep"])
            self.assertEqual(saved["tracks"][0]["custom_track_data"], 42)

    def test_refuses_to_save_if_original_clip_identity_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            media_path = root / "video.mp4"
            media_path.touch()
            draft_path = root / "draft_content.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "materials": {
                            "videos": [
                                {
                                    "id": "video-id",
                                    "path": str(media_path),
                                    "material_name": "video.mp4",
                                    "type": "video",
                                }
                            ]
                        },
                        "tracks": [
                            {
                                "type": "video",
                                "segments": [
                                    {
                                        "material_id": "video-id",
                                        "source_timerange": {
                                            "start": 0,
                                            "duration": 1_000_000,
                                        },
                                        "target_timerange": {
                                            "start": 0,
                                            "duration": 1_000_000,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            project = extract_timeline(draft_path)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["tracks"][0]["segments"][0]["material_id"] = "changed-id"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "alterado externamente"):
                save_capcut_draft(draft_path, project)

            self.assertEqual(list(root.glob("*.bak")), [])


if __name__ == "__main__":
    unittest.main()
