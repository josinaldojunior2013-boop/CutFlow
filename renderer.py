"""Render CapCut source ranges with FFmpeg using CPU-based H.264 encoding."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import tempfile
from pathlib import Path

from parser import TimelineProject, TimelineSegment, VideoCut, extract_timeline


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        message = result.stderr.strip() or f"FFmpeg terminou com código {result.returncode}."
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
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats"]


def _is_still_image(path: Path) -> bool:
    return path.suffix.lower() in STILL_IMAGE_SUFFIXES


def _render_transparent_segment(
    segment: TimelineSegment,
    output_path: Path,
    project: TimelineProject,
    ffmpeg: str,
    duration: float,
    source_start: float,
) -> None:
    if not segment.path.is_file():
        raise FileNotFoundError(f"Arquivo de mídia não encontrado: {segment.path}")

    command = _ffmpeg_base(ffmpeg)
    if _is_still_image(segment.path):
        command.extend(["-loop", "1", "-framerate", str(project.fps)])
    elif source_start > 0:
        command.extend(["-ss", str(source_start)])
    command.extend(["-i", str(segment.path)])
    if segment.track_type == "audio":
        raise ValueError("Faixas de áudio não podem ser renderizadas como vídeo.")

    speed = segment.speed if segment.speed > 0 else 1.0
    filter_chain = (
        f"setpts=(PTS-STARTPTS)/{speed},fps={project.fps},"
        f"scale={project.width}:{project.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"format=rgba,pad={project.width}:{project.height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
        "setsar=1,format=argb"
    )
    command.extend(
        [
            "-t",
            str(duration),
            "-an",
            "-vf",
            filter_chain,
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            "-r",
            str(project.fps),
            str(output_path),
        ]
    )
    _run_ffmpeg(command)


def _render_transparent_gap(
    output_path: Path,
    project: TimelineProject,
    ffmpeg: str,
    duration: float,
) -> None:
    _run_ffmpeg(
        _ffmpeg_base(ffmpeg)
        + [
            "-f",
            "lavfi",
            "-i",
            f"color=c=black@0.0:s={project.width}x{project.height}:r={project.fps}:d={duration},format=rgba",
            "-an",
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output_path),
        ]
    )


def _concat_track(parts: list[Path], output_path: Path, ffmpeg: str) -> None:
    concat_file = output_path.with_suffix(".txt")
    entries = []
    for part in parts:
        escaped_path = part.resolve().as_posix().replace("'", "'\\''")
        entries.append(f"file '{escaped_path}'\n")
    concat_file.write_text("".join(entries), encoding="utf-8")
    _run_ffmpeg(
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
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output_path),
        ]
    )


def _render_video_tracks(
    project: TimelineProject, temp_path: Path, ffmpeg: str
) -> list[Path]:
    video_tracks: dict[int, list[TimelineSegment]] = {}
    for segment in project.segments:
        if segment.track_type == "video":
            video_tracks.setdefault(segment.track_index, []).append(segment)

    rendered_tracks: list[Path] = []
    for track_index, segments in sorted(video_tracks.items()):
        parts: list[Path] = []
        cursor = 0.0
        ordered_segments = sorted(segments, key=lambda item: item.timeline_start)
        for index, segment in enumerate(ordered_segments):
            start = max(segment.timeline_start, 0.0)
            if start > cursor:
                gap_path = temp_path / f"track_{track_index:03d}_gap_{index:05d}.mov"
                _render_transparent_gap(gap_path, project, ffmpeg, start - cursor)
                parts.append(gap_path)
                cursor = start

            overlap = max(cursor - start, 0.0)
            duration = min(segment.duration - overlap, project.duration - cursor)
            if index + 1 < len(ordered_segments):
                next_start = ordered_segments[index + 1].timeline_start
                duration = min(duration, max(next_start - cursor, 0.0))
            if duration <= 0:
                continue

            part_path = temp_path / f"track_{track_index:03d}_clip_{index:05d}.mov"
            _render_transparent_segment(
                segment,
                part_path,
                project,
                ffmpeg,
                duration,
                segment.source_start + overlap * max(segment.speed, 0.0),
            )
            parts.append(part_path)
            cursor += duration

        if cursor < project.duration:
            gap_path = temp_path / f"track_{track_index:03d}_tail.mov"
            _render_transparent_gap(gap_path, project, ffmpeg, project.duration - cursor)
            parts.append(gap_path)

        if parts:
            track_path = temp_path / f"track_{track_index:03d}.mov"
            _concat_track(parts, track_path, ffmpeg)
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
        command_start = ["-ss", str(segment.source_start)] if segment.source_start > 0 else []
        inputs.extend(command_start + ["-i", str(segment.path)])
        delay_ms = max(round(segment.timeline_start * 1000), 0)
        filters.append(
            f"[{index}:a]atrim=duration={segment.duration},asetpts=PTS-STARTPTS,"
            f"volume={segment.volume},adelay={delay_ms}:all=1[a{index}]"
        )
    labels = "".join(f"[a{index}]" for index in range(len(segments)))
    filters.append(
        f"{labels}amix=inputs={len(segments)}:duration=longest:dropout_transition=0[aout]"
    )
    return inputs, filters


def render_timeline(
    project: TimelineProject, output_path: str | Path, ffmpeg: str | None = None
) -> Path:
    """Compose timed video and audio tracks into an MP4 using CPU encoding."""
    if project.duration <= 0:
        raise ValueError("O rascunho não informa uma duração de timeline válida.")
    if not any(item.track_type == "video" for item in project.segments):
        raise ValueError("Nenhuma mídia visual válida foi encontrada no rascunho.")

    ffmpeg = resolve_ffmpeg(ffmpeg)
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width = project.width + project.width % 2
    height = project.height + project.height % 2
    project = TimelineProject(
        segments=project.segments,
        duration=project.duration,
        width=width,
        height=height,
        fps=project.fps,
    )

    with tempfile.TemporaryDirectory(prefix="capcut-light-") as temp_dir:
        temp_path = Path(temp_dir)
        video_tracks = _render_video_tracks(project, temp_path, ffmpeg)
        if not video_tracks:
            raise ValueError("Nenhuma trilha de vídeo pôde ser renderizada.")

        audio_segments = [item for item in project.segments if item.track_type == "audio"]
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
                f"atrim=duration={project.duration}[aout]"
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
                str(project.duration),
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
        _run_ffmpeg(command)

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
    root.resizable(False, False)

    project_var = tk.StringVar()
    output_var = tk.StringVar()
    status_var = tk.StringVar(value="Selecione a pasta do projeto CapCut.")

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
        project_var.set(str(draft_file))
        output_var.set(str(Path(folder) / "CapCutLightRenderer.mp4"))
        status_var.set("Projeto carregado. Pronto para renderizar.")

    def choose_output() -> None:
        output = filedialog.asksaveasfilename(
            title="Salvar vídeo renderizado",
            defaultextension=".mp4",
            filetypes=[("Vídeo MP4", "*.mp4")],
            initialfile="CapCutLightRenderer.mp4",
        )
        if output:
            output_var.set(output)

    def finish_render(output: Path | None, error: Exception | None) -> None:
        render_button.configure(state="normal")
        if error:
            status_var.set("Falha na renderização.")
            messagebox.showerror("Erro ao renderizar", str(error), parent=root)
            return
        status_var.set(f"Concluído: {output}")
        messagebox.showinfo("Renderização concluída", f"Vídeo salvo em:\n{output}", parent=root)

    def start_render() -> None:
        draft_file = Path(project_var.get())
        output_file = Path(output_var.get())
        if not draft_file.is_file():
            messagebox.showerror("Projeto inválido", "Selecione uma pasta de projeto válida.", parent=root)
            return
        if not output_var.get().strip():
            messagebox.showerror("Saída inválida", "Escolha onde salvar o vídeo.", parent=root)
            return

        render_button.configure(state="disabled")
        status_var.set("Renderizando com CPU. Isso pode levar alguns minutos...")

        def worker() -> None:
            try:
                project = extract_timeline(draft_file)
                result = render_timeline(project, output_file)
                root.after(0, finish_render, result, None)
            except Exception as error:
                root.after(0, finish_render, None, error)

        threading.Thread(target=worker, daemon=True).start()

    frame = ttk.Frame(root, padding=18)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="Pasta do projeto CapCut").grid(row=0, column=0, sticky="w")
    ttk.Entry(frame, textvariable=project_var, width=58, state="readonly").grid(
        row=1, column=0, padx=(0, 8), pady=(5, 12)
    )
    ttk.Button(frame, text="Selecionar pasta...", command=choose_project).grid(
        row=1, column=1, pady=(5, 12)
    )
    ttk.Label(frame, text="Arquivo MP4 de saída").grid(row=2, column=0, sticky="w")
    ttk.Entry(frame, textvariable=output_var, width=58).grid(
        row=3, column=0, padx=(0, 8), pady=(5, 14)
    )
    ttk.Button(frame, text="Escolher...", command=choose_output).grid(
        row=3, column=1, pady=(5, 14)
    )
    render_button = ttk.Button(frame, text="Renderizar", command=start_render)
    render_button.grid(row=4, column=0, sticky="w")
    ttk.Label(frame, textvariable=status_var, wraplength=470).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(14, 0)
    )
    ttk.Label(
        frame,
        text="Renderiza trilhas de vídeo, imagens e áudio. Efeitos, textos e transições do CapCut podem não ser aplicados.",
        wraplength=470,
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
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