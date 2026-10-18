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


@dataclass(frozen=True)
class TimelineSegment:
    """A media segment positioned on a CapCut timeline."""

    path: Path | None
    track_type: str
    track_index: int
    source_start: float
    timeline_start: float
    duration: float
    volume: float
    speed: float
    media_id: str = ""
    media_name: str = ""
    track_name: str = ""
    enabled: bool = True


@dataclass
class TimelineProject:
    """Renderable media and canvas settings from a CapCut draft."""

    segments: list[TimelineSegment]
    duration: float
    width: int
    height: int
    fps: float


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
    """Extract video ranges, retaining the original convenience API."""
    project = extract_timeline(draft_path)
    return [
        VideoCut(segment.path, segment.source_start, segment.duration)
        for segment in project.segments
        if segment.track_type == "video" and segment.path is not None
    ]


def extract_timeline(draft_path: str | Path) -> TimelineProject:
    """Extract media, timing, and canvas settings from a CapCut draft."""
    draft_path = Path(draft_path).expanduser().resolve()
    with draft_path.open("r", encoding="utf-8") as draft_file:
        draft = json.load(draft_file)

    materials = draft.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    media_by_id: dict[str, dict[str, Any]] = {}
    for material_group in ("videos", "audios"):
        for material in materials.get(material_group, []):
            material_id = material.get("id")
            material_path = (
                material.get("path")
                or material.get("file_path")
                or material.get("media_path")
            )
            if material_id:
                media_by_id[material_id] = {
                    "path": material_path,
                    "name": material.get("material_name")
                    or material.get("name")
                    or (Path(material_path).name if material_path else material_id),
                }

    segments: list[TimelineSegment] = []
    for track_index, track in enumerate(draft.get("tracks", [])):
        track_type = track.get("type")
        if track_type not in {"video", "audio"}:
            continue
        for segment in track.get("segments", []):
            if not segment.get("visible", True):
                continue
            material_id = segment.get("material_id")
            material = media_by_id.get(material_id, {})
            material_path = material.get("path")
            if not material_path:
                segment_material = segment.get("material", {})
                material_path = (
                    segment_material.get("path")
                    or segment_material.get("file_path")
                    or segment.get("file_path")
                )

            source_range = segment.get("source_timerange") or {}
            target_range = segment.get("target_timerange") or {}
            duration_seconds = _seconds(
                target_range.get("duration") or source_range.get("duration")
            )
            if duration_seconds <= 0:
                continue

            segments.append(
                TimelineSegment(
                    path=(
                        _resolve_media_path(material_path, draft_path.parent)
                        if material_path
                        else None
                    ),
                    track_type=track_type,
                    track_index=track_index,
                    source_start=_seconds(source_range.get("start", 0)),
                    timeline_start=_seconds(target_range.get("start", 0)),
                    duration=duration_seconds,
                    volume=float(segment.get("volume", 1.0) or 0.0),
                    speed=float(segment.get("speed", 1.0) or 1.0),
                    media_id=material_id or "",
                    media_name=material.get("name") or material_id or "Mídia sem nome",
                    track_name=track.get("name") or f"Faixa {track_index + 1}",
                    enabled=True,
                )
            )

    segments.sort(key=lambda item: (item.track_index, item.timeline_start))
    canvas = draft.get("canvas_config") or {}
    duration = _seconds(draft.get("duration"))
    if duration <= 0 and segments:
        duration = max(item.timeline_start + item.duration for item in segments)

    return TimelineProject(
        segments=segments,
        duration=duration,
        width=int(canvas.get("width") or 1920),
        height=int(canvas.get("height") or 1080),
        fps=float(draft.get("fps") or 30.0),
    )