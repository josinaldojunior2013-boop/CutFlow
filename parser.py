"""Read video cuts from a CapCut draft_content.json file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any, Callable


MICROSECONDS_PER_SECOND = 1_000_000
PANEL_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


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
    media_kind: str = ""
    track_name: str = ""
    enabled: bool = True
    raw_path: str = ""
    effects: tuple[str, ...] = ()
    transitions: tuple[str, ...] = ()
    source_segment_index: int = -1


@dataclass
class TimelineProject:
    """Renderable media and canvas settings from a CapCut draft."""

    segments: list[TimelineSegment]
    duration: float
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class RelinkSummary:
    linked_materials: int
    ambiguous_materials: int
    missing_materials: int


class RelinkCancelledError(RuntimeError):
    """Raised when the user cancels a folder media scan."""


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


def _is_source_less_panel_shadow(segment: TimelineSegment) -> bool:
    if segment.raw_path:
        return False
    track_name = segment.track_name.casefold()
    media_name = segment.media_name.casefold()
    return (
        "panel shadow" in track_name
        or "painel sombra" in track_name
        or "painel sombra" in media_name
    )


def _is_panel_image(segment: TimelineSegment) -> bool:
    return (
        segment.media_kind.casefold() in {"photo", "image"}
        or Path(segment.raw_path).suffix.casefold() in PANEL_IMAGE_SUFFIXES
        or (
            segment.path is not None
            and segment.path.suffix.casefold() in PANEL_IMAGE_SUFFIXES
        )
    )


def link_panel_shadows(segments: list[TimelineSegment]) -> list[TimelineSegment]:
    panels_by_range: dict[tuple[int, int], list[TimelineSegment]] = {}
    for segment in segments:
        if (
            segment.track_type != "video"
            or _is_source_less_panel_shadow(segment)
            or not _is_panel_image(segment)
        ):
            continue
        time_range = (
            round(segment.timeline_start * MICROSECONDS_PER_SECOND),
            round(segment.duration * MICROSECONDS_PER_SECOND),
        )
        panels_by_range.setdefault(time_range, []).append(segment)

    linked_segments: list[TimelineSegment] = []
    for segment in segments:
        if not _is_source_less_panel_shadow(segment):
            linked_segments.append(segment)
            continue
        if segment.path is not None and segment.path.is_file():
            linked_segments.append(segment)
            continue
        time_range = (
            round(segment.timeline_start * MICROSECONDS_PER_SECOND),
            round(segment.duration * MICROSECONDS_PER_SECOND),
        )
        matching_panels = panels_by_range.get(time_range, [])
        if (
            len(matching_panels) == 1
            and matching_panels[0].path is not None
            and matching_panels[0].path.is_file()
        ):
            segment = replace(segment, path=matching_panels[0].path)
        linked_segments.append(segment)
    return linked_segments


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
    references_by_id: dict[str, tuple[str, str]] = {}
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
                    "kind": material.get("type") or material_group,
                }
    for material_group, reference_type in (
        ("video_effects", "effect"),
        ("transitions", "transition"),
    ):
        for material in materials.get(material_group, []):
            material_id = material.get("id")
            if material_id:
                reference_name = (
                    material.get("name")
                    or material.get("category_name")
                    or reference_type
                )
                references_by_id[material_id] = (reference_type, reference_name)

    segments: list[TimelineSegment] = []
    for track_index, track in enumerate(draft.get("tracks", [])):
        track_type = track.get("type")
        if track_type not in {"video", "audio"}:
            continue
        for segment_index, segment in enumerate(track.get("segments", [])):
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
            extra_material_refs = segment.get("extra_material_refs") or []
            effects = tuple(
                references_by_id[reference_id][1]
                for reference_id in extra_material_refs
                if reference_id in references_by_id
                and references_by_id[reference_id][0] == "effect"
            )
            transitions = tuple(
                references_by_id[reference_id][1]
                for reference_id in extra_material_refs
                if reference_id in references_by_id
                and references_by_id[reference_id][0] == "transition"
            )
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
                    media_kind=material.get("kind") or track_type,
                    track_name=track.get("name") or f"Faixa {track_index + 1}",
                    enabled=True,
                    raw_path=material.get("path") or material_path or "",
                    effects=effects,
                    transitions=transitions,
                    source_segment_index=segment_index,
                )
            )

    segments.sort(key=lambda item: (item.track_index, item.timeline_start))
    segments = link_panel_shadows(segments)
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


def relink_media_in_folder(
    segments: list[TimelineSegment],
    media_folder: str | Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[TimelineSegment], RelinkSummary]:
    """Relink missing media only when its exact name maps to one compatible file."""
    media_folder = Path(media_folder).expanduser().resolve()
    if not media_folder.is_dir():
        raise NotADirectoryError(
            f"A pasta de mídias não existe ou não pode ser acessada: {media_folder}"
        )
    audio_suffixes = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
    supported_suffixes = audio_suffixes | {
        ".avi",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".png",
        ".tif",
        ".tiff",
        ".webm",
        ".webp",
    }
    files_by_name: dict[str, set[Path]] = {}
    pending_directories = [media_folder]
    scanned_directories = 0
    scanned_files = 0
    last_reported = (0, 0)
    while pending_directories:
        if cancel_event is not None and cancel_event.is_set():
            raise RelinkCancelledError("A busca de mídias foi cancelada.")
        directory = pending_directories.pop()
        try:
            entries = os.scandir(directory)
            with entries:
                for entry in entries:
                    if cancel_event is not None and cancel_event.is_set():
                        raise RelinkCancelledError("A busca de mídias foi cancelada.")
                    if entry.is_dir(follow_symlinks=False):
                        pending_directories.append(Path(entry.path))
                    elif entry.is_file():
                        scanned_files += 1
                        suffix = Path(entry.name).suffix.casefold()
                        if suffix in supported_suffixes:
                            file_path = Path(entry.path)
                            files_by_name.setdefault(
                                file_path.name.casefold(), set()
                            ).add(file_path)
                            files_by_name.setdefault(
                                file_path.stem.casefold(), set()
                            ).add(file_path)
                        if (
                            progress_callback is not None
                            and scanned_files - last_reported[1] >= 1000
                        ):
                            progress_callback(scanned_directories + 1, scanned_files)
                            last_reported = (scanned_directories + 1, scanned_files)
        except OSError as error:
            raise OSError(
                f"Não foi possível ler a pasta de mídias '{directory}': {error}"
            ) from error
        scanned_directories += 1
        current = (scanned_directories, scanned_files)
        if (
            progress_callback is not None
            and (
                scanned_directories - last_reported[0] >= 10
                or scanned_files - last_reported[1] >= 1000
                or not pending_directories
            )
        ):
            progress_callback(*current)
            last_reported = current

    paths_by_material: dict[str, Path] = {}
    ambiguous_materials: set[str] = set()
    missing_materials: set[str] = set()
    path_status: dict[Path, bool] = {}
    for index, segment in enumerate(segments):
        if _is_source_less_panel_shadow(segment):
            continue
        if segment.path is not None:
            if segment.path not in path_status:
                existing = segment.path.is_file()
                path_status[segment.path] = existing
            if path_status[segment.path]:
                continue
        material_key = segment.media_id or f"segment:{index}"
        if material_key in paths_by_material or material_key in ambiguous_materials:
            continue

        raw_name = Path(segment.raw_path.replace("\\", "/")).name if segment.raw_path else ""
        media_name = Path(segment.media_name.replace("\\", "/")).name
        names = {raw_name.casefold(), Path(raw_name).stem.casefold()}
        names.update({media_name.casefold(), Path(media_name).stem.casefold()})
        candidates = {
            candidate
            for name in names
            if name
            for candidate in files_by_name.get(name, set())
            if (candidate.suffix.lower() in audio_suffixes)
            == (segment.track_type == "audio")
        }
        if len(candidates) == 1:
            paths_by_material[material_key] = next(iter(candidates))
        elif len(candidates) > 1:
            ambiguous_materials.add(material_key)
        else:
            missing_materials.add(material_key)

    relinked_segments = [
        replace(
            segment,
            path=paths_by_material.get(
                segment.media_id or f"segment:{index}", segment.path
            ),
        )
        for index, segment in enumerate(segments)
    ]
    relinked_segments = link_panel_shadows(relinked_segments)
    return relinked_segments, RelinkSummary(
        linked_materials=len(paths_by_material),
        ambiguous_materials=len(ambiguous_materials),
        missing_materials=len(missing_materials),
    )