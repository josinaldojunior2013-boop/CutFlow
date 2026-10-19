"""Export a CapCut timeline using a Filmora 13 project as its native template."""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
import time
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from parser import TimelineProject, TimelineSegment, extract_timeline


FILMORA_TICKS_PER_SECOND = 10_000_000
CAPCUT_MICROSECONDS_PER_SECOND = 1_000_000
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MEDIA_TYPES = {"image": 16, "video": 8, "audio": 4}
STREAM_TYPES = {"image": 5, "video": 2, "audio": 3}


class FilmoraExportError(RuntimeError):
    """Raised when a CapCut timeline cannot safely be written as a Filmora project."""


@dataclass(frozen=True)
class FilmoraExportSummary:
    output_path: Path
    video_clips: int
    image_clips: int
    audio_clips: int
    transitions: int
    unsupported_effects: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]


@dataclass
class _SourceAsset:
    path: Path
    kind: str
    material: dict[str, Any]
    media_id: str
    source_uuid: str
    media_info: dict[str, Any]
    resource: dict[str, Any]
    media_json: dict[str, Any]


def _compact_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json(archive: ZipFile, member: str) -> dict[str, Any]:
    try:
        value = json.loads(archive.read(member))
    except (KeyError, json.JSONDecodeError) as error:
        raise FilmoraExportError(f"Modelo Filmora inválido: {member}") from error
    if not isinstance(value, dict):
        raise FilmoraExportError(f"Modelo Filmora inválido: {member}")
    return value


def _braced_uuid() -> str:
    return "{" + str(uuid4()).upper() + "}"


def _guid_payload(guid: str, size: int = 64) -> str:
    encoded = guid.encode("ascii")
    if len(encoded) > size:
        raise FilmoraExportError("Identificador interno do Filmora excedeu o tamanho esperado.")
    return base64.b64encode(encoded.ljust(size, b"\0")).decode("ascii")


def _seconds_to_ticks(seconds: float) -> int:
    return round(seconds * FILMORA_TICKS_PER_SECOND)


def _is_image(segment: TimelineSegment) -> bool:
    return (
        segment.media_kind.casefold() in {"photo", "image"}
        or (segment.path is not None and segment.path.suffix.casefold() in IMAGE_SUFFIXES)
    )


def _is_audio_path(path: Path) -> bool:
    return path.suffix.casefold() in {
        ".aac",
        ".aiff",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".wav",
        ".wma",
    }


def _wfp_path(path: Path) -> str:
    return "file:/" + path.resolve().as_posix().lstrip("/")


def _source_groups(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    materials = draft.get("materials")
    if not isinstance(materials, dict):
        raise FilmoraExportError("O draft do CapCut não contém materiais.")
    grouped: dict[str, dict[str, Any]] = {}
    for group in ("videos", "audios"):
        for material in materials.get(group, []):
            if isinstance(material, dict) and material.get("id"):
                grouped[str(material["id"])] = material
    return grouped


def _raw_segments_by_track(
    draft: dict[str, Any],
) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for track_index, track in enumerate(draft.get("tracks", [])):
        if track.get("type") not in {"video", "audio"}:
            continue
        result[track_index] = [
            item
            for item in track.get("segments", [])
            if item.get("visible", True)
            and (item.get("target_timerange") or item.get("source_timerange") or {}).get(
                "duration", 0
            )
            > 0
        ]
    return result


def _rewrite_binary_user_data(
    entries: list[dict[str, Any]],
    media_id: str,
    clip_map_id: str,
    clip_path: Path,
    template_paths: dict[str, str],
) -> None:
    for entry in entries:
        key = entry.get("key")
        if key == 3:
            entry["data"] = _guid_payload(clip_map_id)
            entry["size"] = 64
            continue
        if key == 10:
            encoded = media_id.encode("ascii")
            entry["data"] = base64.b64encode(encoded).decode("ascii")
            entry["size"] = len(encoded)
            continue
        if key == 50:
            encoded = hashlib.md5(str(clip_path).encode("utf-8")).hexdigest().encode("ascii")
            entry["data"] = base64.b64encode(encoded).decode("ascii")
            entry["size"] = len(encoded)
            continue

        payload = entry.get("data")
        if not isinstance(payload, str):
            continue
        try:
            decoded = base64.b64decode(payload, validate=True)
        except ValueError:
            continue
        replaced = decoded
        for original, replacement in template_paths.items():
            replaced = replaced.replace(
                original.encode("utf-8"), replacement.encode("utf-8")
            )
        if replaced != decoded:
            entry["data"] = base64.b64encode(replaced).decode("ascii")
            entry["size"] = len(replaced)


def _clone_clip(
    template_clip: dict[str, Any],
    segment: TimelineSegment,
    raw_segment: dict[str, Any],
    asset: _SourceAsset,
    timeline_start: float,
    duration: float,
    transition: dict[str, Any] | None,
    transition_index: int,
    template_paths: dict[str, str],
) -> tuple[dict[str, Any], str]:
    clip = copy.deepcopy(template_clip)
    clip_map_id = _braced_uuid()
    clip["filename"] = _wfp_path(asset.path)
    clip["sourceUuid"] = asset.source_uuid
    clip["streamId"] = 0
    clip["thisUId"] = str(uuid4())
    clip["inPoint"] = _seconds_to_ticks(segment.source_start)
    source_duration = duration * max(segment.speed, 0.000001)
    clip["outPoint"] = _seconds_to_ticks(segment.source_start + source_duration)
    clip["tlBegin"] = _seconds_to_ticks(timeline_start)
    clip["tlEnd"] = _seconds_to_ticks(timeline_start + duration)

    speed = clip.get("speed")
    if isinstance(speed, dict):
        speed["offset"] = 0.0
        speed["offsetEnd"] = source_duration
        speed["reverse"] = bool(raw_segment.get("reverse", False))
        try:
            speed_param = json.loads(speed.get("speedParam", "{}"))
        except json.JSONDecodeError as error:
            raise FilmoraExportError("A curva de velocidade do modelo Filmora é inválida.") from error
        speed_param["keyframeSets"] = [
            {"_time": 0.0, "Interpolation": 6, "_value": max(segment.speed, 0.000001)},
            {
                "_time": source_duration,
                "Interpolation": 6,
                "_value": max(segment.speed, 0.000001),
            },
        ]
        speed_param["_totalTime"] = source_duration
        speed["speedParam"] = json.dumps(speed_param, separators=(",", ":"))

    if segment.track_type == "audio":
        gain_db = 20 * math.log10(max(segment.volume, 1e-5))
        for chain in clip.get("effectChainList", []):
            for effect in chain.get("effectList", []):
                if effect.get("id") != "audio/effect/volume":
                    continue
                for parameter in effect.get("paramList", []):
                    if parameter.get("name") == "VolumeGain":
                        parameter["fxParam"]["unValue"] = gain_db

    _rewrite_binary_user_data(
        clip.get("userData", []),
        asset.media_id,
        clip_map_id,
        asset.path,
        template_paths,
    )
    for chain in clip.get("effectChainList", []):
        for effect in chain.get("effectList", []):
            effect["thisUId"] = str(uuid4())
            _rewrite_binary_user_data(
                effect.get("userData", []),
                asset.media_id,
                _braced_uuid(),
                asset.path,
                template_paths,
            )

    if transition is not None:
        applied_transition = copy.deepcopy(transition)
        transition_duration = min(
            _seconds_to_ticks(duration),
            max(
                round(
                    float(
                        next(
                            (
                                ref.get("duration", 0)
                                for ref in raw_segment.get("_transition_materials", [])
                            ),
                            0,
                        )
                    )
                    * FILMORA_TICKS_PER_SECOND
                    / CAPCUT_MICROSECONDS_PER_SECOND
                ),
                1,
            ),
        )
        clip_end = clip["tlEnd"]
        applied_transition["tlBegin"] = max(clip["tlBegin"], clip_end - transition_duration)
        applied_transition["tlEnd"] = clip_end
        applied_transition["thisUId"] = str(uuid4())
        for entry in applied_transition.get("userData", []):
            if entry.get("key") == 3:
                entry["data"] = _guid_payload(_braced_uuid())
                entry["size"] = 64
            elif entry.get("key") == 8:
                packed = struct.pack("<I", transition_index)
                entry["data"] = base64.b64encode(packed).decode("ascii")
                entry["size"] = len(packed)
        clip["postTransition"] = applied_transition
        _rewrite_binary_user_data(
            clip.get("userData", []),
            asset.media_id,
            clip_map_id,
            asset.path,
            template_paths,
        )
    else:
        clip.pop("postTransition", None)

    return clip, clip_map_id


def _asset_metadata(
    kind: str,
    material: dict[str, Any],
    source_path: Path,
    media_id: str,
    source_uuid: str,
    resource_templates: dict[int, dict[str, Any]],
    media_json_templates: dict[int, dict[str, Any]],
    project: TimelineProject,
) -> _SourceAsset:
    stream_type = STREAM_TYPES[kind]
    if stream_type not in resource_templates or stream_type not in media_json_templates:
        raise FilmoraExportError(
            f"O modelo Filmora não tem metadados de mídia para o tipo {kind}."
        )
    resource = copy.deepcopy(resource_templates[stream_type])
    media_json = copy.deepcopy(media_json_templates[stream_type])
    filename = _wfp_path(source_path)
    media_info = {
        "download_url": source_path.resolve().as_posix(),
        "id": media_id,
        "media_type": MEDIA_TYPES[kind],
        "media_length": (
            50_000_000
            if kind == "image"
            else max(
                round(
                    float(material.get("duration", 0) or 0)
                    * FILMORA_TICKS_PER_SECOND
                    / CAPCUT_MICROSECONDS_PER_SECOND
                ),
                1,
            )
        ),
        "name": source_path.stem,
        "import_time": int(time.time()),
        "src_md5": hashlib.md5(source_path.resolve().as_posix().encode("utf-8")).hexdigest(),
        "stream_idx": -1 if kind == "image" else 0,
        "mark_info_list": [{"mark_in": -1, "mark_out": -1}],
        "scence_info": [],
    }

    resource.update(
        {
            "filename": filename,
            "sourceUuid": source_uuid,
            "streamType": stream_type,
        }
    )
    source_info = media_json.get("sourceInfo", {})
    basic_info = source_info.get("basicInfo", {})
    basic_info.update(
        {
            "streamType": stream_type,
            "mediaLength": 0 if kind == "image" else media_info["media_length"],
            "videoStreamCount": 1 if kind != "audio" else 0,
            "audioStreamCount": 1 if kind == "audio" else 0,
        }
    )
    media_json["file_name"] = source_path.resolve().as_posix()

    if kind in {"image", "video"}:
        width = int(material.get("width") or project.width)
        height = int(material.get("height") or project.height)
        stream_keys = ("vidStreamInfo", "vidStreamInfos")
        for stream_key in stream_keys:
            stream_infos = resource.get(stream_key) or source_info.get(stream_key)
            if isinstance(stream_infos, list) and stream_infos:
                stream_infos[0]["width"] = width
                stream_infos[0]["height"] = height
                stream_infos[0]["xRatio"] = width
                stream_infos[0]["yRatio"] = height
                stream_infos[0]["frameRate"] = {
                    "num": round(project.fps * 1000),
                    "den": 1000,
                }
                stream_infos[0]["maxFrameRate"] = copy.deepcopy(
                    stream_infos[0]["frameRate"]
                )
                if kind == "image":
                    stream_infos[0]["streamLength"] = 0
                    stream_infos[0]["totalFrames"] = 0
        if kind == "image":
            resource["mediaLength"] = 0
    else:
        audio_stream = resource.get("audStreamInfo") or source_info.get("audStreamInfos")
        if isinstance(audio_stream, list) and audio_stream:
            audio_stream[0]["streamLength"] = media_info["media_length"]
            audio_stream[0]["duration"] = media_info["media_length"] / FILMORA_TICKS_PER_SECOND
        resource["mediaLength"] = media_info["media_length"]

    source_info["basicInfo"] = basic_info
    media_json["sourceInfo"] = source_info
    return _SourceAsset(
        path=source_path.resolve(),
        kind=kind,
        material=material,
        media_id=media_id,
        source_uuid=source_uuid,
        media_info=media_info,
        resource=resource,
        media_json=media_json,
    )


def _media_info_document(
    timeline_media_id: str, assets: list[_SourceAsset], duration_ticks: int
) -> bytes:
    media_items = {
        asset.media_id: asset.media_info for asset in assets
    }
    media_items[timeline_media_id] = {
        "name": "CapCut import",
        "download_url": "",
        "id": timeline_media_id,
        "timeline_uuid": _braced_uuid(),
        "media_type": 1048576,
        "create_time": int(time.time()),
        "duration": duration_ticks,
        "mark_info_list": [{"mark_in": -1, "mark_out": -1}],
    }
    source_entries = "".join(
        f'"media_item":{json.dumps(asset.media_id, ensure_ascii=False)},'
        for asset in assets
    )
    folder_entries = source_entries[:-1] if source_entries else ""
    structure = (
        '{"media_structure":{"Folder":{'
        + folder_entries
        + '},"media_item":'
        + json.dumps(timeline_media_id, ensure_ascii=False)
        + '},"media_items":'
        + json.dumps(media_items, ensure_ascii=False, separators=(",", ":"))
        + "}"
    )
    return structure.encode("utf-8")


def _resolve_templates(template: ZipFile) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    dict[int, dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[str, Any],
]:
    info_path = "ProjectFolder/project_info.json"
    project_info = _read_json(template, info_path)
    version = str(project_info.get("project_editor_create_version", ""))
    if not version.startswith("13."):
        raise FilmoraExportError(
            f"O conversor exige um modelo Filmora 13; o modelo encontrado é {version or 'desconhecido'}."
        )
    timeline_media_id = project_info.get("timeline_mediaId")
    if not isinstance(timeline_media_id, str):
        raise FilmoraExportError("O modelo Filmora não identifica a timeline principal.")
    timeline_root = f"ProjectFolder/Medias/{timeline_media_id}/"
    timeline_path = timeline_root + "timeline.wesproj"
    timeline = _read_json(template, timeline_path)
    timeline_infos = timeline.get("timelineInfos", [])
    if not timeline_infos:
        raise FilmoraExportError("O modelo Filmora não contém uma timeline editável.")
    timeline_info = timeline_infos[0]
    video_tracks = [
        track for track in timeline_info.get("trackInfos", []) if track.get("trackType") == 1
    ]
    audio_tracks = [
        track for track in timeline_info.get("trackInfos", []) if track.get("trackType") == 2
    ]
    video_clips = [
        clip
        for track in video_tracks
        for clip in track.get("clipList", [])
        if clip.get("type") == 1
    ]
    audio_clips = [
        clip
        for track in audio_tracks
        for clip in track.get("clipList", [])
        if clip.get("type") == 2
    ]
    def normal_speed(clip: dict[str, Any]) -> bool:
        try:
            speed = json.loads(clip.get("speed", {}).get("speedParam", "{}"))
        except (AttributeError, json.JSONDecodeError):
            return False
        values = [
            keyframe.get("_value")
            for keyframe in speed.get("keyframeSets", [])
            if isinstance(keyframe, dict)
        ]
        return bool(values) and all(value == 1.0 for value in values)

    video_template = next(
        (
            clip
            for clip in video_clips
            if "postTransition" not in clip and normal_speed(clip)
        ),
        None,
    )
    audio_template = next(
        (
            clip
            for clip in audio_clips
            if "postTransition" not in clip and normal_speed(clip)
        ),
        None,
    )
    transition_template = next(
        (
            clip["postTransition"]
            for track in video_tracks
            for clip in track.get("clipList", [])
            if "postTransition" in clip
        ),
        None,
    )
    if video_template is None or audio_template is None or transition_template is None:
        raise FilmoraExportError(
            "O modelo Filmora precisa conter ao menos um clipe de vídeo, um de áudio e uma transição."
        )

    resources: dict[int, dict[str, Any]] = {}
    for resource in timeline.get("resources", []):
        stream_type = resource.get("streamType")
        if stream_type in STREAM_TYPES.values():
            resources.setdefault(stream_type, resource)
    media_jsons: dict[int, dict[str, Any]] = {}
    for member in template.namelist():
        if not member.endswith("/media.json") or member.startswith(timeline_root):
            continue
        document = _read_json(template, member)
        stream_type = document.get("sourceInfo", {}).get("basicInfo", {}).get("streamType")
        if stream_type in STREAM_TYPES.values():
            media_jsons.setdefault(stream_type, document)
    if any(stream_type not in resources for stream_type in STREAM_TYPES.values()):
        raise FilmoraExportError("O modelo Filmora não contém os três tipos de mídia necessários.")
    if any(stream_type not in media_jsons for stream_type in STREAM_TYPES.values()):
        raise FilmoraExportError("O modelo Filmora não contém metadados de vídeo, imagem e áudio.")
    extra = _read_json(template, timeline_root + "extra.json")
    return (
        project_info,
        timeline_root,
        timeline,
        timeline_info,
        resources,
        media_jsons,
        {"video": video_template, "audio": audio_template},
        {"transition": transition_template, "extra": extra},
    )


def export_filmora13(
    draft_path: str | Path,
    output_path: str | Path,
    template_path: str | Path,
    project: TimelineProject | None = None,
) -> FilmoraExportSummary:
    """Create a Filmora 13 project from a CapCut draft and a Filmora-made template."""
    draft_path = Path(draft_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    template_path = Path(template_path).expanduser().resolve()
    if not draft_path.is_file():
        raise FileNotFoundError(f"Draft do CapCut não encontrado: {draft_path}")
    if not template_path.is_file():
        raise FileNotFoundError(f"Modelo Filmora não encontrado: {template_path}")
    if output_path in {draft_path, template_path}:
        raise FilmoraExportError("O projeto de saída não pode substituir o draft nem o modelo.")
    if output_path.exists():
        raise FilmoraExportError(f"O arquivo de saída já existe: {output_path}")

    project = project or extract_timeline(draft_path)
    try:
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FilmoraExportError(f"Não foi possível ler o draft: {error}") from error
    source_materials = _source_groups(draft)
    raw_tracks = _raw_segments_by_track(draft)

    active_segments = [segment for segment in project.segments if segment.enabled]
    if not any(segment.track_type == "video" for segment in active_segments):
        raise FilmoraExportError("Não há clipes de vídeo ativos para exportar.")
    missing = [
        segment
        for segment in active_segments
        if segment.path is None or not segment.path.is_file()
    ]
    if missing:
        examples = "\n".join(
            f"• {segment.track_name}: {segment.media_name}" for segment in missing[:8]
        )
        raise FilmoraExportError(
            f"Há {len(missing)} mídia(s) ausente(s). Religue-as no aplicativo antes de exportar:\n{examples}"
        )

    with ZipFile(template_path, "r") as template:
        (
            project_info,
            timeline_root,
            timeline,
            timeline_info,
            resource_templates,
            media_json_templates,
            clip_templates,
            other_templates,
        ) = _resolve_templates(template)
        template_paths = {
            str(resource.get("filename", "")): ""
            for resource in timeline.get("resources", [])
            if resource.get("filename")
        }
        original_timeline_id = project_info["timeline_mediaId"]
        extra = other_templates["extra"]
        extra["mediaClipsMapInfo"] = {}
        extra.setdefault("allMarkersInfo", [])
        extra["allMarkersInfo"] = []

        material_by_path: dict[Path, dict[str, Any]] = {}
        for segment in active_segments:
            if segment.path is None:
                continue
            material = source_materials.get(segment.media_id, {})
            if segment.path.suffix.casefold() in IMAGE_SUFFIXES:
                kind = "image"
            elif segment.track_type == "audio" or _is_audio_path(segment.path):
                kind = "audio"
            else:
                kind = "video"
            existing = material_by_path.get(segment.path.resolve())
            if existing is None or existing.get("type") != "photo":
                material_by_path[segment.path.resolve()] = {
                    "kind": kind,
                    "material": material,
                }

        assets_by_path: dict[Path, _SourceAsset] = {}
        assets: list[_SourceAsset] = []
        for path, details in material_by_path.items():
            media_id = _braced_uuid()
            asset = _asset_metadata(
                details["kind"],
                details["material"],
                path,
                media_id,
                str(uuid4()),
                resource_templates,
                media_json_templates,
                project,
            )
            assets_by_path[path] = asset
            assets.append(asset)

        raw_by_track_and_media: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for track_index, raw_segments in raw_tracks.items():
            for raw_segment in raw_segments:
                key = (track_index, str(raw_segment.get("material_id") or ""))
                raw_by_track_and_media.setdefault(key, []).append(raw_segment)

        tracks: list[dict[str, Any]] = []
        video_track_template = next(
            track
            for track in timeline_info["trackInfos"]
            if track.get("trackType") == 1 and track.get("clipList")
        )
        audio_track_template = next(
            track
            for track in timeline_info["trackInfos"]
            if track.get("trackType") == 2 and track.get("clipList")
        )
        transition_materials = {
            str(material.get("id")): material
            for material in draft.get("materials", {}).get("transitions", [])
            if isinstance(material, dict) and material.get("id")
        }
        ref_materials = {
            str(material.get("id")): material
            for group in ("video_effects", "transitions")
            for material in draft.get("materials", {}).get(group, [])
            if isinstance(material, dict) and material.get("id")
        }
        effects_seen: dict[str, int] = {}
        transition_count = 0
        video_count = image_count = audio_count = 0
        track_segments = sorted(
            active_segments, key=lambda item: (item.track_index, item.timeline_start)
        )
        track_indices = sorted({item.track_index for item in track_segments})
        for tag, track_index in enumerate(track_indices, start=1):
            source_track = draft["tracks"][track_index]
            track_type = source_track.get("type")
            is_video = track_type == "video"
            track = copy.deepcopy(video_track_template if is_video else audio_track_template)
            track["clipList"] = []
            track["trackTag"] = tag
            segments = [item for item in track_segments if item.track_index == track_index]
            for segment in segments:
                raw_candidates = raw_by_track_and_media.get(
                    (track_index, segment.media_id), []
                )
                if not raw_candidates:
                    raise FilmoraExportError(
                        f"Não foi possível localizar os dados do clipe {segment.media_name} no draft."
                    )
                target_start = round(
                    segment.timeline_start * CAPCUT_MICROSECONDS_PER_SECOND
                )
                source_start = round(
                    segment.source_start * CAPCUT_MICROSECONDS_PER_SECOND
                )
                duration = round(segment.duration * CAPCUT_MICROSECONDS_PER_SECOND)
                candidate_index = min(
                    range(len(raw_candidates)),
                    key=lambda index: (
                        abs(
                            int(
                                (raw_candidates[index].get("target_timerange") or {}).get(
                                    "start", 0
                                )
                            )
                            - target_start
                        ),
                        abs(
                            int(
                                (raw_candidates[index].get("source_timerange") or {}).get(
                                    "start", 0
                                )
                            )
                            - source_start
                        ),
                        abs(
                            int(
                                (
                                    raw_candidates[index].get("target_timerange")
                                    or raw_candidates[index].get("source_timerange")
                                    or {}
                                ).get("duration", 0)
                            )
                            - duration
                        ),
                    ),
                )
                raw_segment = raw_candidates.pop(candidate_index)
                source_asset = assets_by_path[segment.path.resolve()]
                transition_refs = [
                    ref_materials[reference_id]
                    for reference_id in raw_segment.get("extra_material_refs", [])
                    if reference_id in transition_materials
                ]
                raw_segment["_transition_materials"] = transition_refs
                selected_transition = (
                    other_templates["transition"] if transition_refs else None
                )
                clip, clip_map_id = _clone_clip(
                    clip_templates["audio" if segment.track_type == "audio" else "video"],
                    segment,
                    raw_segment,
                    source_asset,
                    segment.timeline_start,
                    segment.duration,
                    selected_transition,
                    transition_count + 1,
                    template_paths,
                )
                track["clipList"].append(clip)
                extra["mediaClipsMapInfo"][clip_map_id] = {
                    "mediaId": source_asset.media_id,
                    "subClips": {},
                }
                if segment.track_type == "audio":
                    audio_count += 1
                elif _is_image(segment):
                    image_count += 1
                else:
                    video_count += 1
                transition_count += bool(transition_refs)
                for effect_name in segment.effects:
                    effects_seen[effect_name] = effects_seen.get(effect_name, 0) + 1
            tracks.append(track)

        timeline_info["trackInfos"] = tracks
        fps_num = round(project.fps * 1000)
        fps_divisor = math.gcd(fps_num, 1000)
        project_fps = [fps_num // fps_divisor, 1000 // fps_divisor]
        width, height = project.width, project.height
        aspect_divisor = math.gcd(width, height)
        timeline_info["frameRate"] = {
            "num": project_fps[0],
            "den": project_fps[1],
        }
        timeline_info["resolutionWidth"] = width
        timeline_info["resolutionHeight"] = height
        timeline_info["aspectRatioX"] = width // aspect_divisor
        timeline_info["aspectRatioY"] = height // aspect_divisor
        timeline["resources"] = [asset.resource for asset in assets]

        duration_ticks = _seconds_to_ticks(project.duration)
        project_info.update(
            {
                "project_file_name": output_path.stem,
                "project_file_suffix": ".wfp",
                "project_date_modify": int(time.time()),
                "project_timeline_duration": duration_ticks,
                "project_timeline_framerate": project_fps,
                "project_timeline_resolution": [width, height],
                "project_timeline_ratio": [
                    width // aspect_divisor,
                    height // aspect_divisor,
                ],
                "project_guid": _braced_uuid(),
                "proj_zip_save_path": output_path.as_posix(),
                "proj_cover_proj_path": "",
                "project_backup": False,
            }
        )
        media_items = _media_info_document(
            original_timeline_id, assets, duration_ticks
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output_path.with_suffix(output_path.suffix + ".tmp")
        old_media_prefix = "ProjectFolder/Medias/"
        with ZipFile(temporary_output, "w", compression=ZIP_DEFLATED) as output:
            for member in template.namelist():
                if member in {
                    "ProjectFolder/project_info.json",
                    "ProjectFolder/Medias/medias_info.json",
                    timeline_root + "timeline.wesproj",
                    timeline_root + "extra.json",
                }:
                    continue
                if member.startswith(old_media_prefix) and member.endswith(
                    ("/media.json", "/thumbnail.png")
                ):
                    if not member.startswith(timeline_root):
                        continue
                output.writestr(member, template.read(member))

            output.writestr(
                "ProjectFolder/project_info.json", _compact_json(project_info)
            )
            output.writestr("ProjectFolder/Medias/medias_info.json", media_items)
            output.writestr(timeline_root + "timeline.wesproj", _compact_json(timeline))
            output.writestr(timeline_root + "extra.json", _compact_json(extra))
            for asset in assets:
                folder = f"ProjectFolder/Medias/{asset.media_id}/"
                output.writestr(folder + "media.json", _compact_json(asset.media_json))
        temporary_output.replace(output_path)

    with ZipFile(output_path, "r") as result:
        if result.testzip() is not None:
            output_path.unlink(missing_ok=True)
            raise FilmoraExportError("O projeto Filmora gerado está corrompido.")

    unsupported = tuple(
        sorted(
            effects_seen.items(),
            key=lambda item: item[0].casefold(),
        )
    )
    warnings = (
        "As transições do CapCut foram convertidas para a transição Fade Grayscale do modelo Filmora; o visual pode diferir do original.",
        "Efeitos de vídeo e animações por keyframes do CapCut não foram transferidos nesta versão.",
    )
    return FilmoraExportSummary(
        output_path=output_path,
        video_clips=video_count,
        image_clips=image_count,
        audio_clips=audio_count,
        transitions=transition_count,
        unsupported_effects=unsupported,
        warnings=warnings,
    )
