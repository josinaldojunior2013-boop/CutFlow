"""Read video cuts from a CapCut draft_content.json file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class VideoCut:
    """A source-video range, represented in seconds."""

    path: Path
    start: float
    duration: float


def _seconds(value: Any) -> float:
    """Convert CapCut's integer microseconds to seconds."""
    return float(value or 0) / MICROSECONDS_PER_SECOND


def _resolve_media_path(raw_path: str, draft_dir: Path) -> Path:
    media_path = Path(raw_path).expanduser()
    if not media_path.is_absolute():
        direct_path = (draft_dir / media_path).resolve()
        if direct_path.is_file():
            return direct_path
    if media_path.is_file():
        return media_path.resolve()

    parts = media_path.parts
    resources_index = next(
        (index for index, part in enumerate(parts) if part.lower() == "resources"),
        None,
    )
    if resources_index is None:
        return media_path.resolve()

    resource_parts = parts[resources_index + 1 :]
    resource_roots = [draft_dir / "Resources"]
    resource_roots.extend(
        path for path in draft_dir.rglob("Resources") if path.is_dir()
    )
    for resource_root in dict.fromkeys(resource_roots):
        relocated_path = resource_root.joinpath(*resource_parts)
        if relocated_path.is_file():
            return relocated_path.resolve()

    for resource_root in dict.fromkeys(resource_roots):
        if not resource_root.is_dir():
            continue
        matches = [path for path in resource_root.rglob(media_path.name) if path.is_file()]
        if len(matches) == 1:
            return matches[0].resolve()
    return media_path.resolve()


def extract_video_cuts(draft_path: str | Path) -> list[VideoCut]:
    """Extract source paths and source time ranges from a CapCut draft."""
    draft_path = Path(draft_path).expanduser().resolve()
    with draft_path.open("r", encoding="utf-8") as draft_file:
        draft = json.load(draft_file)

    materials = draft.get("materials", {})
    videos = materials.get("videos", []) if isinstance(materials, dict) else []
    video_paths = {
        material.get("id"): material.get("path") or material.get("file_path")
        for material in videos
        if material.get("id")
    }

    cuts: list[VideoCut] = []
    for track in draft.get("tracks", []):
        if track.get("type") not in (None, "video"):
            continue
        for segment in track.get("segments", []):
            material_id = segment.get("material_id")
            material_path = video_paths.get(material_id)
            if not material_path:
                material = segment.get("material", {})
                material_path = material.get("path") or segment.get("file_path")
            if not material_path:
                continue

            source_range = segment.get("source_timerange") or {}
            duration = source_range.get("duration")
            if duration is None:
                duration = (segment.get("target_timerange") or {}).get("duration", 0)
            duration_seconds = _seconds(duration)
            if duration_seconds <= 0:
                continue

            cuts.append(
                VideoCut(
                    path=_resolve_media_path(material_path, draft_path.parent),
                    start=_seconds(source_range.get("start", 0)),
                    duration=duration_seconds,
                )
            )

    return cuts