"""Render CapCut source ranges with FFmpeg using CPU-based H.264 encoding."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import replace
import shutil
import subprocess
import sys
import threading
import tempfile
from pathlib import Path
from typing import Any, Callable

from parser import TimelineProject, TimelineSegment, VideoCut, extract_timeline


class RenderProgress:
    def __init__(self, total_work: float, callback: Callable[[int, str], None]):
        self.total_work = max(total_work, 1e-9)
        self.callback = callback
        self.completed_work = 0.0

    def run(self, command: list[str], label: str, duration: float) -> None:
        duration = max(duration, 0.0)

        def report(fraction: float) -> None:
            work = self.completed_work + duration * min(max(fraction, 0.0), 1.0)
            self.callback(min(int(work * 100 / self.total_work), 99), label)

        _run_ffmpeg(command, report, duration)
        self.completed_work += duration
        self.callback(min(int(self.completed_work * 100 / self.total_work), 99), label)


def _run_stage(
    command: list[str],
    progress: RenderProgress,
    label: str,
    duration: float,
) -> None:
    progress.run(command, label, duration)


def _run_ffmpeg(
    command: list[str],
    progress_callback: Callable[[float], None] | None = None,
    duration: float = 0.0,
) -> None:
    command = [*command, "-progress", "pipe:1"]
    output = deque(maxlen=40)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.stdout is not None:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                output.append(line)
            if progress_callback and line.startswith("out_time_us=") and duration > 0:
                try:
                    current_time = float(line.partition("=")[2]) / 1_000_000
                except ValueError:
                    continue
                progress_callback(current_time / duration)
            elif progress_callback and line == "progress=end":
                progress_callback(1.0)
    return_code = process.wait()
    if return_code:
        message = "\n".join(output) or f"FFmpeg terminou com código {return_code}."
        raise RuntimeError(message)


def resolve_ffmpeg(ffmpeg: str | None) -> str:
    if ffmpeg:
        return ffmpeg
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as error:
        raise RuntimeError(
            "FFmpeg não encontrado. Instale as dependências do projeto ou informe "
            "o caminho com --ffmpeg."
        ) from error


STILL_IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def _ffmpeg_base(ffmpeg: str) -> list[str]:
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
    ]


def _is_still_image(path: Path) -> bool:
    return path.suffix.lower() in STILL_IMAGE_SUFFIXES


def _ffmpeg_seconds(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def _render_transparent_segment(
    segment: TimelineSegment,
    output_path: Path,
    project: TimelineProject,
    ffmpeg: str,
    duration: float,
    source_start: float,
    preserve_alpha: bool,
    progress: RenderProgress,
    label: str,
) -> None:
    if not segment.path.is_file():
        raise FileNotFoundError(f"Arquivo de mídia não encontrado: {segment.path}")

    command = _ffmpeg_base(ffmpeg)
    if _is_still_image(segment.path):
        command.extend(["-loop", "1", "-framerate", str(project.fps)])
    elif source_start > 0:
        command.extend(["-ss", _ffmpeg_seconds(source_start)])
    command.extend(["-i", str(segment.path)])
    if segment.track_type == "audio":
        raise ValueError("Faixas de áudio não podem ser renderizadas como vídeo.")

    speed = segment.speed if segment.speed > 0 else 1.0
    pixel_format = "argb" if preserve_alpha else "yuv420p"
    pad_color = "0x00000000" if preserve_alpha else "black"
    filter_chain = (
        f"setpts=(PTS-STARTPTS)/{_ffmpeg_seconds(speed)},fps={project.fps},"
        f"scale={project.width}:{project.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"format=rgba,pad={project.width}:{project.height}:(ow-iw)/2:(oh-ih)/2:color={pad_color},"
        f"setsar=1,format={pixel_format}"
    )
    video_codec = "qtrle" if preserve_alpha else "libx264"
    command.extend(
        [
            "-t",
            _ffmpeg_seconds(duration),
            "-an",
            "-vf",
            filter_chain,
            "-c:v",
            video_codec,
            *(["-preset", "ultrafast", "-crf", "20"] if not preserve_alpha else []),
            "-pix_fmt",
            pixel_format,
            "-r",
            str(project.fps),
            str(output_path),
        ]
    )
    _run_stage(command, progress, label, duration)


def _render_transparent_gap(
    output_path: Path,
    project: TimelineProject,
    ffmpeg: str,
    duration: float,
    preserve_alpha: bool,
    progress: RenderProgress,
    label: str,
) -> None:
    pixel_format = "argb" if preserve_alpha else "yuv420p"
    color = "black@0.0" if preserve_alpha else "black"
    pixel_filter = "rgba" if preserve_alpha else "yuv420p"
    video_codec = "qtrle" if preserve_alpha else "libx264"
    encoder_args = ["-preset", "ultrafast", "-crf", "20"] if not preserve_alpha else []
    _run_stage(
        _ffmpeg_base(ffmpeg)
        + [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={project.width}x{project.height}:r={project.fps}:d={_ffmpeg_seconds(duration)},format={pixel_filter}",
            "-an",
            "-c:v",
            video_codec,
            *encoder_args,
            "-pix_fmt",
            pixel_format,
            str(output_path),
        ],
        progress,
        label,
        duration,
    )


def _concat_track(
    parts: list[Path],
    output_path: Path,
    ffmpeg: str,
    progress: RenderProgress,
    duration: float,
    label: str,
) -> None:
    concat_file = output_path.with_suffix(".txt")
    entries = []
    for part in parts:
        escaped_path = part.resolve().as_posix().replace("'", "'\\''")
        entries.append(f"file '{escaped_path}'\n")
    concat_file.write_text("".join(entries), encoding="utf-8")
    _run_stage(
        _ffmpeg_base(ffmpeg)
        + [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-an",
            "-c:v",
            "copy",
            str(output_path),
        ],
        progress,
        label,
        duration,
    )
    concat_file.unlink(missing_ok=True)


def _render_video_tracks(
    project: TimelineProject,
    temp_path: Path,
    ffmpeg: str,
    progress: RenderProgress,
) -> list[Path]:
    video_tracks: dict[int, list[TimelineSegment]] = {}
    for segment in project.segments:
        if segment.enabled and segment.track_type == "video":
            video_tracks.setdefault(segment.track_index, []).append(segment)

    rendered_tracks: list[Path] = []
    frame_rate = project.fps
    project_frames = max(round(project.duration * frame_rate), 1)
    base_track_index = min(video_tracks)
    for track_index, segments in sorted(video_tracks.items()):
        preserve_alpha = track_index != base_track_index
        parts: list[Path] = []
        cursor_frame = 0
        ordered_segments = sorted(segments, key=lambda item: item.timeline_start)
        for index, segment in enumerate(ordered_segments):
            start_frame = max(round(segment.timeline_start * frame_rate), 0)
            end_frame = min(
                round((segment.timeline_start + segment.duration) * frame_rate),
                project_frames,
            )
            if end_frame <= start_frame:
                continue

            if start_frame > cursor_frame:
                gap_path = temp_path / f"track_{track_index:03d}_gap_{index:05d}.mov"
                gap_duration = (start_frame - cursor_frame) / frame_rate
                _render_transparent_gap(
                    gap_path,
                    project,
                    ffmpeg,
                    gap_duration,
                    preserve_alpha,
                    progress,
                    f"Gerando vazio da faixa {track_index + 1}",
                )
                parts.append(gap_path)
                cursor_frame = start_frame

            overlap_frames = max(cursor_frame - start_frame, 0)
            duration_frames = min(end_frame - cursor_frame, project_frames - cursor_frame)
            if index + 1 < len(ordered_segments):
                next_start_frame = max(
                    round(ordered_segments[index + 1].timeline_start * frame_rate), 0
                )
                duration_frames = min(
                    duration_frames, max(next_start_frame - cursor_frame, 0)
                )
            if duration_frames <= 0:
                continue

            part_path = temp_path / f"track_{track_index:03d}_clip_{index:05d}.mov"
            duration = duration_frames / frame_rate
            _render_transparent_segment(
                segment,
                part_path,
                project,
                ffmpeg,
                duration,
                segment.source_start
                + (overlap_frames / frame_rate) * max(segment.speed, 0.0),
                preserve_alpha,
                progress,
                f"Renderizando faixa {track_index + 1}, clipe {index + 1}",
            )
            parts.append(part_path)
            cursor_frame += duration_frames

        if project_frames - cursor_frame > 0:
            gap_path = temp_path / f"track_{track_index:03d}_tail.mov"
            tail_duration = (project_frames - cursor_frame) / frame_rate
            _render_transparent_gap(
                gap_path,
                project,
                ffmpeg,
                tail_duration,
                preserve_alpha,
                progress,
                f"Finalizando faixa {track_index + 1}",
            )
            parts.append(gap_path)

        if parts:
            track_path = temp_path / f"track_{track_index:03d}.mov"
            _concat_track(
                parts,
                track_path,
                ffmpeg,
                progress,
                project.duration,
                f"Montando faixa {track_index + 1}",
            )
            for part in parts:
                part.unlink(missing_ok=True)
            rendered_tracks.append(track_path)
    return rendered_tracks


def _render_audio_inputs(
    segments: list[TimelineSegment], ffmpeg: str
) -> tuple[list[str], list[str]]:
    inputs: list[str] = []
    filters: list[str] = []
    for index, segment in enumerate(segments):
        if not segment.path.is_file():
            raise FileNotFoundError(f"Arquivo de áudio não encontrado: {segment.path}")
        command_start = ["-ss", _ffmpeg_seconds(segment.source_start)] if segment.source_start > 0 else []
        inputs.extend(command_start + ["-i", str(segment.path)])
        delay_ms = max(round(segment.timeline_start * 1000), 0)
        filters.append(
            f"[{index}:a]atrim=duration={_ffmpeg_seconds(segment.duration)},asetpts=PTS-STARTPTS,"
            f"volume={_ffmpeg_seconds(segment.volume)},adelay={delay_ms}:all=1[a{index}]"
        )
    labels = "".join(f"[a{index}]" for index in range(len(segments)))
    filters.append(
        f"{labels}amix=inputs={len(segments)}:duration=longest:dropout_transition=0[aout]"
    )
    return inputs, filters


def render_timeline(
    project: TimelineProject,
    output_path: str | Path,
    ffmpeg: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> Path:
    """Compose timed video and audio tracks into an MP4 using CPU encoding."""
    if project.duration <= 0:
        raise ValueError("O rascunho não informa uma duração de timeline válida.")
    active_segments = [item for item in project.segments if item.enabled]
    if not any(item.track_type == "video" and item.path is not None for item in active_segments):
        raise ValueError("Nenhuma mídia visual válida foi encontrada no rascunho.")
    missing_segments = [
        item for item in active_segments if item.path is None or not item.path.is_file()
    ]
    if missing_segments:
        raise FileNotFoundError(
            f"Mídia ausente na faixa {missing_segments[0].track_name}: "
            f"{missing_segments[0].media_name}"
        )

    ffmpeg = resolve_ffmpeg(ffmpeg)
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = project.width + project.width % 2
    height = project.height + project.height % 2
    project = TimelineProject(
        segments=active_segments,
        duration=project.duration,
        width=width,
        height=height,
        fps=project.fps,
    )

    video_track_count = len(
        {item.track_index for item in project.segments if item.track_type == "video"}
    )
    progress = RenderProgress(
        project.duration * (video_track_count * 2 + 1),
        progress_callback or (lambda percent, label: None),
    )
    with tempfile.TemporaryDirectory(prefix="capcut-light-") as temp_dir:
        temp_path = Path(temp_dir)
        video_tracks = _render_video_tracks(project, temp_path, ffmpeg, progress)
        if not video_tracks:
            raise ValueError("Nenhuma trilha de vídeo pôde ser renderizada.")

        audio_segments = [
            item
            for item in project.segments
            if item.enabled and item.track_type == "audio"
        ]
        command = _ffmpeg_base(ffmpeg)
        for track_path in video_tracks:
            command.extend(["-i", str(track_path)])

        filter_script = temp_path / "compose.txt"
        filters: list[str] = []
        filters.append("[0:v]setpts=PTS-STARTPTS[v0]")
        current_label = "v0"
        for index in range(1, len(video_tracks)):
            next_label = f"v{index}"
            filters.append(
                f"[{current_label}][{index}:v]overlay=0:0:shortest=1:eof_action=pass:format=auto[{next_label}]"
            )
            current_label = next_label
        filters.append(f"[{current_label}]format=yuv420p[vout]")

        audio_inputs: list[str] = []
        if audio_segments:
            audio_inputs, audio_filters = _render_audio_inputs(audio_segments, ffmpeg)
            command.extend(audio_inputs)
            audio_offset = len(video_tracks)
            filters.extend(
                item.replace(f"[{index}:", f"[{audio_offset + index}:")
                for index, item in enumerate(audio_filters[:-1])
            )
            labels = "".join(
                f"[a{index}]" for index in range(len(audio_segments))
            )
            filters.append(
                f"{labels}amix=inputs={len(audio_segments)}:duration=longest:dropout_transition=0,"
                f"atrim=duration={_ffmpeg_seconds(project.duration)}[aout]"
            )

        filter_script.write_text(";\n".join(filters), encoding="utf-8")
        command.extend(
            [
                "-filter_complex_script",
                str(filter_script),
                "-map",
                "[vout]",
            ]
        )
        if audio_segments:
            command.extend(["-map", "[aout]", "-c:a", "aac"])
        else:
            command.append("-an")
        command.extend(
            [
                "-t",
                _ffmpeg_seconds(project.duration),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "0",
                "-r",
                str(project.fps),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        _run_stage(command, progress, "Compondo faixas e áudio", project.duration)

    progress.callback(100, "Renderização concluída")
    return output_path


def render_cuts(
    cuts: list[VideoCut], output_path: str | Path, ffmpeg: str | None = None
) -> Path:
    """Render a sequential list of video ranges for backward compatibility."""
    timeline_segments = []
    cursor = 0.0
    for cut in cuts:
        timeline_segments.append(
            TimelineSegment(cut.path, "video", 0, cut.start, cursor, cut.duration, 1.0, 1.0)
        )
        cursor += cut.duration
    project = TimelineProject(timeline_segments, cursor, 1920, 1080, 30.0)
    return render_timeline(project, output_path, ffmpeg)


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("CapCut Light Renderer")
    root.geometry("1180x720")
    root.minsize(900, 560)

    project_var = tk.StringVar(value="Nenhum projeto selecionado")
    output_var = tk.StringVar()
    status_var = tk.StringVar(value="Selecione a pasta do projeto CapCut.")
    progress_var = tk.IntVar(value=0)
    state: dict[str, Any] = {"project": None}
    state["draft_path"] = None

    container = ttk.Frame(root, padding=12)
    container.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(2, weight=1)

    project_bar = ttk.Frame(container)
    project_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
    project_bar.columnconfigure(1, weight=1)
    ttk.Label(project_bar, text="Projeto").grid(row=0, column=0, padx=(0, 8))
    ttk.Entry(project_bar, textvariable=project_var, state="readonly").grid(
        row=0, column=1, sticky="ew", padx=(0, 8)
    )
    browse_project_button = ttk.Button(project_bar, text="Abrir pasta...", command=lambda: choose_project())
    browse_project_button.grid(row=0, column=2)

    output_bar = ttk.Frame(container)
    output_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    output_bar.columnconfigure(1, weight=1)
    ttk.Label(output_bar, text="Saída").grid(row=0, column=0, padx=(0, 8))
    ttk.Entry(output_bar, textvariable=output_var).grid(
        row=0, column=1, sticky="ew", padx=(0, 8)
    )
    choose_output_button = ttk.Button(
        output_bar, text="Salvar como...", command=lambda: choose_output()
    )
    choose_output_button.grid(row=0, column=2)

    table_frame = ttk.Frame(container)
    table_frame.grid(row=2, column=0, sticky="nsew")
    table_frame.rowconfigure(0, weight=1)
    table_frame.columnconfigure(0, weight=1)
    columns = (
        "active",
        "track",
        "kind",
        "media",
        "status",
        "timeline",
        "duration",
        "source",
    )
    timeline_table = ttk.Treeview(
        table_frame, columns=columns, show="headings", selectmode="browse"
    )
    headings = {
        "active": ("Usar", 58),
        "track": ("Faixa", 155),
        "kind": ("Tipo", 80),
        "media": ("Mídia", 290),
        "status": ("Arquivo", 120),
        "timeline": ("Início", 95),
        "duration": ("Duração", 95),
        "source": ("Início na fonte", 115),
    }
    for column, (label, width) in headings.items():
        timeline_table.heading(column, text=label)
        timeline_table.column(column, width=width, minwidth=65, stretch=column == "media")
    timeline_table.tag_configure("missing", foreground="#b42318")
    timeline_table.tag_configure("ready", foreground="#18794e")
    vertical_scroll = ttk.Scrollbar(
        table_frame, orient="vertical", command=timeline_table.yview
    )
    horizontal_scroll = ttk.Scrollbar(
        table_frame, orient="horizontal", command=timeline_table.xview
    )
    timeline_table.configure(
        yscrollcommand=vertical_scroll.set,
        xscrollcommand=horizontal_scroll.set,
    )
    timeline_table.grid(row=0, column=0, sticky="nsew")
    vertical_scroll.grid(row=0, column=1, sticky="ns")
    horizontal_scroll.grid(row=1, column=0, sticky="ew")

    action_bar = ttk.Frame(container)
    action_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
    action_bar.columnconfigure(4, weight=1)
    relink_button = ttk.Button(action_bar, text="Religar mídia...", command=lambda: relink_selected())
    relink_button.grid(row=0, column=0, padx=(0, 6))
    adjust_button = ttk.Button(action_bar, text="Ajustar clipe...", command=lambda: adjust_selected())
    adjust_button.grid(row=0, column=1, padx=(0, 6))
    toggle_track_button = ttk.Button(
        action_bar, text="Ativar/ignorar faixa", command=lambda: toggle_selected_track()
    )
    toggle_track_button.grid(row=0, column=2, padx=(0, 6))
    refresh_button = ttk.Button(action_bar, text="Recarregar", command=lambda: choose_project())
    refresh_button.grid(row=0, column=3)
    render_button = ttk.Button(action_bar, text="Renderizar", command=lambda: start_render())
    render_button.grid(row=0, column=4, sticky="e")

    progress_bar = ttk.Progressbar(
        container, variable=progress_var, maximum=100, mode="determinate"
    )
    progress_bar.grid(row=4, column=0, sticky="ew", pady=(8, 4))
    ttk.Label(container, textvariable=status_var, anchor="w").grid(
        row=5, column=0, sticky="ew"
    )
    ttk.Label(
        container,
        text="Renderiza vídeo, imagem e áudio. Alguns efeitos e transições do CapCut podem exigir ajustes manuais.",
        anchor="w",
    ).grid(row=6, column=0, sticky="ew", pady=(5, 0))

    def format_time(seconds: float) -> str:
        minutes, seconds = divmod(max(seconds, 0.0), 60)
        return f"{int(minutes):02d}:{seconds:05.2f}"

    def refresh_table() -> None:
        timeline_table.delete(*timeline_table.get_children())
        project = state["project"]
        if project is None:
            return
        missing_count = 0
        active_missing_count = 0
        for index, segment in enumerate(project.segments):
            file_exists = segment.path is not None and segment.path.is_file()
            if not file_exists:
                missing_count += 1
                if segment.enabled:
                    active_missing_count += 1
            media_label = (
                segment.path.name if segment.path is not None else segment.media_name
            )
            timeline_table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "Sim" if segment.enabled else "Não",
                    segment.track_name,
                    "Vídeo" if segment.track_type == "video" else "Áudio",
                    media_label,
                    "OK" if file_exists else "Religar",
                    format_time(segment.timeline_start),
                    format_time(segment.duration),
                    format_time(segment.source_start),
                ),
                tags=("ready" if file_exists else "missing",),
            )
        has_active_video = any(
            item.enabled and item.track_type == "video" and item.path is not None
            for item in project.segments
        )
        render_button.configure(
            state="normal" if active_missing_count == 0 and has_active_video else "disabled"
        )
        active_tracks = len(
            {item.track_index for item in project.segments if item.enabled}
        )
        status_var.set(
            f"{len(project.segments)} clipes · {active_tracks} faixas ativas · "
            f"{active_missing_count}/{missing_count} mídia(s) ausentes · "
            f"{project.width}×{project.height} · {project.fps:g} FPS"
        )

    def choose_project() -> None:
        folder = filedialog.askdirectory(title="Selecione a pasta do projeto")
        if not folder:
            return
        draft_file = Path(folder) / "draft_content.json"
        if not draft_file.is_file():
            messagebox.showerror(
                "Projeto não encontrado",
                "A pasta selecionada não contém draft_content.json.",
                parent=root,
            )
            return
        try:
            project = extract_timeline(draft_file)
        except Exception as error:
            messagebox.showerror(
                "Não foi possível abrir o projeto", str(error), parent=root
            )
            return
        state["project"] = project
        state["draft_path"] = draft_file
        project_var.set(str(folder))
        progress_var.set(0)
        refresh_table()

    def choose_output() -> None:
        folder = Path(state["draft_path"]).parent if state["draft_path"] else Path.cwd()
        output = filedialog.asksaveasfilename(
            title="Salvar vídeo renderizado",
            defaultextension=".mp4",
            filetypes=[("Vídeo MP4", "*.mp4")],
            initialdir=str(folder),
            initialfile="CapCutLightRenderer.mp4",
        )
        if output:
            output_var.set(output)

    def selected_index() -> int | None:
        selection = timeline_table.selection()
        return int(selection[0]) if selection else None

    def toggle_selected_track() -> None:
        index = selected_index()
        project = state["project"]
        if index is None or project is None:
            messagebox.showinfo(
                "Selecione uma faixa",
                "Selecione uma linha da faixa primeiro.",
                parent=root,
            )
            return
        track_index = project.segments[index].track_index
        enabled = not project.segments[index].enabled
        project.segments = [
            replace(item, enabled=enabled) if item.track_index == track_index else item
            for item in project.segments
        ]
        refresh_table()

    def relink_selected() -> None:
        index = selected_index()
        project = state["project"]
        if index is None or project is None:
            messagebox.showinfo("Selecione uma mídia", "Selecione uma linha da tabela primeiro.", parent=root)
            return
        segment = project.segments[index]
        file_path = filedialog.askopenfilename(
            title=f"Selecione a mídia: {segment.media_name}",
            filetypes=[
                ("Mídias de vídeo, áudio e imagem", "*.mp4 *.mov *.mkv *.avi *.webm *.mp3 *.wav *.m4a *.aac *.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
                ("Todos os arquivos", "*.*"),
            ],
            parent=root,
        )
        if not file_path:
            return
        selected_path = Path(file_path).resolve()
        project.segments = [
            replace(item, path=selected_path)
            if (segment.media_id and item.media_id == segment.media_id)
            or item is segment
            else item
            for item in project.segments
        ]
        refresh_table()

    def adjust_selected() -> None:
        index = selected_index()
        project = state["project"]
        if index is None or project is None:
            messagebox.showinfo("Selecione um clipe", "Selecione uma linha da tabela primeiro.", parent=root)
            return
        segment = project.segments[index]
        dialog = tk.Toplevel(root)
        dialog.title("Ajustar clipe")
        dialog.transient(root)
        dialog.resizable(False, False)
        dialog.grab_set()
        fields = {
            "Início na timeline (s)": tk.StringVar(value=str(segment.timeline_start)),
            "Duração (s)": tk.StringVar(value=str(segment.duration)),
            "Início na fonte (s)": tk.StringVar(value=str(segment.source_start)),
            "Volume (0–2)": tk.StringVar(value=str(segment.volume)),
        }
        for row, (label, variable) in enumerate(fields.items()):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=6)
            ttk.Entry(dialog, textvariable=variable, width=24).grid(
                row=row, column=1, sticky="ew", padx=(0, 12), pady=6
            )

        def save_adjustment() -> None:
            try:
                timeline_start = float(fields["Início na timeline (s)"].get())
                duration = float(fields["Duração (s)"].get())
                source_start = float(fields["Início na fonte (s)"].get())
                volume = float(fields["Volume (0–2)"].get())
                if (
                    timeline_start < 0
                    or source_start < 0
                    or duration <= 0
                    or not 0 <= volume <= 2
                ):
                    raise ValueError(
                        "Use inícios não negativos, duração maior que zero e volume entre 0 e 2."
                    )
            except ValueError as error:
                messagebox.showerror("Valores inválidos", str(error), parent=dialog)
                return
            project.segments[index] = replace(
                segment,
                timeline_start=timeline_start,
                duration=duration,
                source_start=source_start,
                volume=volume,
            )
            project.duration = max(project.duration, timeline_start + duration)
            project.segments.sort(key=lambda item: (item.track_index, item.timeline_start))
            refresh_table()
            dialog.destroy()

        controls = ttk.Frame(dialog, padding=(12, 4, 12, 12))
        controls.grid(row=len(fields), column=0, columnspan=2, sticky="e")
        ttk.Button(controls, text="Cancelar", command=dialog.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(controls, text="Aplicar", command=save_adjustment).grid(row=0, column=1, padx=4)

    def set_progress(percent: int, label: str) -> None:
        progress_var.set(percent)
        status_var.set(f"{percent}% · {label}")

    def finish_render(output: Path | None, error: Exception | None) -> None:
        render_button.configure(state="normal")
        browse_project_button.configure(state="normal")
        choose_output_button.configure(state="normal")
        relink_button.configure(state="normal")
        adjust_button.configure(state="normal")
        toggle_track_button.configure(state="normal")
        refresh_button.configure(state="normal")
        if error:
            refresh_table()
            status_var.set("Renderização falhou; consulte as mídias e o erro.")
            messagebox.showerror("Erro ao renderizar", str(error), parent=root)
            return
        progress_var.set(100)
        status_var.set(f"100% · Renderização concluída: {output}")
        messagebox.showinfo("Renderização concluída", f"Vídeo salvo em:\n{output}", parent=root)

    def start_render() -> None:
        project = state["project"]
        if project is None:
            messagebox.showerror("Projeto inválido", "Abra uma pasta de projeto primeiro.", parent=root)
            return
        if not output_var.get().strip():
            messagebox.showerror("Saída inválida", "Escolha onde salvar o vídeo.", parent=root)
            return
        active_segments = [item for item in project.segments if item.enabled]
        missing = [
            item for item in active_segments if item.path is None or not item.path.is_file()
        ]
        if missing:
            messagebox.showerror(
                "Mídias pendentes",
                f"Há {len(missing)} clipe(s) sem arquivo. Selecione cada linha marcada como Religar antes de renderizar.",
                parent=root,
            )
            return

        output_file = Path(output_var.get())
        render_button.configure(state="disabled")
        browse_project_button.configure(state="disabled")
        choose_output_button.configure(state="disabled")
        relink_button.configure(state="disabled")
        adjust_button.configure(state="disabled")
        toggle_track_button.configure(state="disabled")
        refresh_button.configure(state="disabled")
        progress_var.set(0)
        status_var.set("Preparando renderização...")

        def report_progress(percent: int, label: str) -> None:
            root.after(0, lambda value=percent, text=label: set_progress(value, text))

        def worker() -> None:
            try:
                result = render_timeline(project, output_file, progress_callback=report_progress)
                root.after(0, finish_render, result, None)
            except Exception as error:
                root.after(0, finish_render, None, error)

        threading.Thread(target=worker, daemon=True).start()

    timeline_table.bind("<Double-1>", lambda _event: adjust_selected())
    root.mainloop()


def main() -> None:
    if len(sys.argv) == 1:
        launch_gui()
        return

    argument_parser = argparse.ArgumentParser(
        description="Renderiza a timeline de um rascunho do CapCut."
    )
    argument_parser.add_argument("draft", help="Caminho para draft_content.json")
    argument_parser.add_argument("output", help="Caminho do vídeo MP4 de saída")
    argument_parser.add_argument(
        "--ffmpeg", help="Executável do FFmpeg (detectado automaticamente por padrão)"
    )
    args = argument_parser.parse_args()

    project = extract_timeline(args.draft)
    output_path = render_timeline(project, args.output, ffmpeg=args.ffmpeg)
    print(f"Vídeo renderizado: {output_path}")


if __name__ == "__main__":
    main()