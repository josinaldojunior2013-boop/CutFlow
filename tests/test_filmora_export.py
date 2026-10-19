import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from filmora_export import export_filmora13
from parser import extract_timeline


TEMPLATE = Path(__file__).resolve().parents[1] / "MODELO EDIÇÃO FILMORA.wfp"


class FilmoraExportTests(unittest.TestCase):
    def test_exports_native_filmora_project_with_tracks_and_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            video_path = root / "source.mp4"
            image_path = root / "panel.jpg"
            audio_path = root / "music.mp3"
            for path in (video_path, image_path, audio_path):
                path.write_bytes(b"test media")

            transition_id = "transition-id"
            draft = {
                "duration": 8_000_000,
                "fps": 30,
                "canvas_config": {"width": 1920, "height": 1080},
                "materials": {
                    "videos": [
                        {
                            "id": "video-id",
                            "path": str(video_path),
                            "material_name": "source.mp4",
                            "type": "video",
                            "duration": 10_000_000,
                            "width": 1920,
                            "height": 1080,
                            "has_audio": True,
                        },
                        {
                            "id": "image-id",
                            "path": str(image_path),
                            "material_name": "panel.jpg",
                            "type": "photo",
                            "duration": 10_000_000,
                            "width": 640,
                            "height": 480,
                        },
                    ],
                    "audios": [
                        {
                            "id": "audio-id",
                            "path": str(audio_path),
                            "material_name": "music.mp3",
                            "type": "extract_music",
                            "duration": 10_000_000,
                        }
                    ],
                    "transitions": [
                        {
                            "id": transition_id,
                            "name": "Esmaecimento preto",
                            "duration": 200_000,
                        }
                    ],
                },
                "tracks": [
                    {
                        "type": "video",
                        "name": "Main",
                        "segments": [
                            {
                                "material_id": "video-id",
                                "source_timerange": {"start": 0, "duration": 5_000_000},
                                "target_timerange": {"start": 0, "duration": 5_000_000},
                            },
                            {
                                "material_id": "video-id",
                                "source_timerange": {"start": 4_000_000, "duration": 2_000_000},
                                "target_timerange": {"start": 6_000_000, "duration": 2_000_000},
                            },
                        ],
                    },
                    {
                        "type": "video",
                        "name": "Panels",
                        "segments": [
                            {
                                "material_id": "image-id",
                                "source_timerange": {"start": 0, "duration": 2_000_000},
                                "target_timerange": {"start": 1_000_000, "duration": 2_000_000},
                                "extra_material_refs": [transition_id],
                            }
                        ],
                    },
                    {
                        "type": "audio",
                        "name": "Music",
                        "segments": [
                            {
                                "material_id": "audio-id",
                                "source_timerange": {"start": 500_000, "duration": 2_000_000},
                                "target_timerange": {"start": 1_500_000, "duration": 2_000_000},
                                "volume": 0.5,
                            }
                        ],
                    },
                ],
            }
            draft_path = root / "draft_content.json"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            output_path = root / "Converted.wfp"
            project = extract_timeline(draft_path)
            project.segments[0] = replace(project.segments[0], enabled=False)

            summary = export_filmora13(
                draft_path,
                output_path,
                TEMPLATE,
                project,
            )

            self.assertEqual(summary.video_clips, 1)
            self.assertEqual(summary.image_clips, 1)
            self.assertEqual(summary.audio_clips, 1)
            self.assertEqual(summary.transitions, 1)
            with ZipFile(output_path) as archive:
                self.assertIsNone(archive.testzip())
                info = json.loads(archive.read("ProjectFolder/project_info.json"))
                timeline_root = f"ProjectFolder/Medias/{info['timeline_mediaId']}/"
                timeline = json.loads(
                    archive.read(timeline_root + "timeline.wesproj")
                )
                extra = json.loads(archive.read(timeline_root + "extra.json"))
                tracks = timeline["timelineInfos"][0]["trackInfos"]
                video_clip = tracks[1]["clipList"][0]
                main_clip = tracks[0]["clipList"][0]
                audio_clip = tracks[2]["clipList"][0]

                self.assertEqual(info["project_editor_create_version"], "13.6.6.8453")
                self.assertEqual(main_clip["tlBegin"], 60_000_000)
                self.assertEqual(main_clip["tlEnd"], 80_000_000)
                self.assertEqual(main_clip["inPoint"], 40_000_000)
                self.assertEqual(video_clip["tlBegin"], 10_000_000)
                self.assertEqual(video_clip["tlEnd"], 30_000_000)
                self.assertEqual(
                    video_clip["postTransition"]["tlEnd"]
                    - video_clip["postTransition"]["tlBegin"],
                    2_000_000,
                )
                self.assertEqual(audio_clip["tlBegin"], 15_000_000)
                self.assertEqual(audio_clip["inPoint"], 5_000_000)
                self.assertEqual(
                    len(extra["mediaClipsMapInfo"]),
                    3,
                )
                self.assertEqual(
                    {resource["streamType"] for resource in timeline["resources"]},
                    {2, 3, 5},
                )
                media_info_raw = archive.read(
                    "ProjectFolder/Medias/medias_info.json"
                ).decode("utf-8")
                self.assertEqual(media_info_raw.count('"media_item":'), 4)

    def test_rejects_missing_project_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            draft_path = root / "draft_content.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "duration": 1_000_000,
                        "fps": 30,
                        "materials": {"videos": [], "audios": []},
                        "tracks": [
                            {
                                "type": "video",
                                "segments": [
                                    {
                                        "material_id": "missing",
                                        "source_timerange": {
                                            "start": 0,
                                            "duration": 1_000_000,
                                        },
                                        "target_timerange": {
                                            "start": 0,
                                            "duration": 1_000_000,
                                        },
                                        "file_path": str(root / "missing.mp4"),
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "mídia\\(s\\) ausente\\(s\\)"):
                export_filmora13(
                    draft_path, root / "Missing.wfp", TEMPLATE
                )


if __name__ == "__main__":
    unittest.main()
