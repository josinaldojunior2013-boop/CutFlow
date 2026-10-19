"""Read and safely update editable timing fields in CapCut draft files."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from parser import MICROSECONDS_PER_SECOND, TimelineProject


def save_capcut_draft(
    draft_path: str | Path, project: TimelineProject
) -> Path:
    """Update the edited timeline in place and return the automatic backup path."""
    draft_path = Path(draft_path).expanduser().resolve()
    if not draft_path.is_file():
        raise FileNotFoundError(f"Draft do CapCut não encontrado: {draft_path}")

    try:
        original_content = draft_path.read_bytes()
        draft = json.loads(original_content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Não foi possível ler o draft do CapCut: {error}") from error
    if not isinstance(draft, dict) or not isinstance(draft.get("tracks"), list):
        raise ValueError("O draft do CapCut não contém uma lista válida de faixas.")
    materials = draft.get("materials")
    if not isinstance(materials, dict):
        raise ValueError("O draft do CapCut não contém uma lista válida de mídias.")
    media_by_id = {
        str(material.get("id")): material
        for group in ("videos", "audios")
        for material in materials.get(group, [])
        if isinstance(material, dict) and material.get("id")
    }

    for segment in project.segments:
        if segment.track_index < 0 or segment.source_segment_index < 0:
            raise ValueError(
                f"O clipe '{segment.media_name}' não está associado a um clipe original do draft."
            )
        if segment.track_index >= len(draft["tracks"]):
            raise ValueError(
                f"A faixa original do clipe '{segment.media_name}' não existe no draft."
            )
        track = draft["tracks"][segment.track_index]
        raw_segments = track.get("segments") if isinstance(track, dict) else None
        if (
            not isinstance(raw_segments, list)
            or segment.source_segment_index >= len(raw_segments)
        ):
            raise ValueError(
                f"O clipe original '{segment.media_name}' não existe mais no draft."
            )
        raw_segment = raw_segments[segment.source_segment_index]
        if not isinstance(raw_segment, dict):
            raise ValueError(
                f"Os dados originais do clipe '{segment.media_name}' são inválidos."
            )
        if str(raw_segment.get("material_id") or "") != segment.media_id:
            raise ValueError(
                f"O draft foi alterado externamente; não foi possível confirmar "
                f"o clipe '{segment.media_name}'. Reabra o projeto antes de salvar."
            )

        source_range = raw_segment.get("source_timerange")
        target_range = raw_segment.get("target_timerange")
        if not isinstance(source_range, dict) or not isinstance(target_range, dict):
            raise ValueError(
                f"O clipe '{segment.media_name}' não tem intervalos CapCut editáveis."
            )

        source_range["start"] = round(
            segment.source_start * MICROSECONDS_PER_SECOND
        )
        source_range["duration"] = round(
            segment.duration * max(segment.speed, 0.000001)
            * MICROSECONDS_PER_SECOND
        )
        target_range["start"] = round(
            segment.timeline_start * MICROSECONDS_PER_SECOND
        )
        target_range["duration"] = round(
            segment.duration * MICROSECONDS_PER_SECOND
        )
        raw_segment["volume"] = segment.volume
        if not segment.enabled or "visible" in raw_segment:
            raw_segment["visible"] = segment.enabled
        if segment.path is not None and segment.raw_path:
            material = media_by_id.get(segment.media_id)
            if material is not None:
                material["path"] = str(segment.path.resolve())

    edited_duration = max(
        (
            round(
                (segment.timeline_start + segment.duration)
                * MICROSECONDS_PER_SECOND
            )
            for segment in project.segments
        ),
        default=0,
    )
    draft["duration"] = max(int(draft.get("duration") or 0), edited_duration)

    try:
        serialized = json.dumps(
            draft, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Não foi possível serializar o draft do CapCut: {error}") from error

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = draft_path.with_name(
        f"{draft_path.name}.{timestamp}.bak"
    )
    temporary_path: Path | None = None
    try:
        shutil.copy2(draft_path, backup_path)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=draft_path.parent,
            prefix=f".{draft_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        shutil.copystat(draft_path, temporary_path)
        os.replace(temporary_path, draft_path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise OSError(f"Não foi possível salvar o draft do CapCut: {error}") from error

    return backup_path
