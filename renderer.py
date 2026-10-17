"""Render CapCut source ranges with FFmpeg using CPU-based H.264 encoding."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import tempfile
from pathlib import Path

from parser import VideoCut, extract_video_cuts


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def render_cuts(
    cuts: list[VideoCut], output_path: str | Path, ffmpeg: str | None = None
) -> Path:
    """Render and concatenate cuts into one MP4 using libx264 on the CPU."""
    if not cuts:
        raise ValueError("Nenhum corte de vídeo válido foi encontrado no rascunho.")

    ffmpeg = resolve_ffmpeg(ffmpeg)
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="capcut-light-") as temp_dir:
        temp_path = Path(temp_dir)
        rendered_parts: list[Path] = []
        for index, cut in enumerate(cuts):
            if not cut.path.is_file():
                raise FileNotFoundError(f"Arquivo de vídeo não encontrado: {cut.path}")
            part_path = temp_path / f"part_{index:05d}.mp4"
            is_still_image = cut.path.suffix.lower() in {
                ".bmp",
                ".gif",
                ".jpeg",
                ".jpg",
                ".png",
                ".tif",
                ".tiff",
                ".webp",
            }
            command = [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
            ]
            if is_still_image:
                command.extend(["-loop", "1"])
            else:
                command.extend(["-ss", str(cut.start)])
            command.extend(["-i", str(cut.path)])
            if is_still_image:
                command.extend(
                    [
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-vf",
                        "fps=30,format=yuv420p",
                        "-shortest",
                    ]
                )
            command.extend(
                [
                "-t",
                str(cut.duration),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-threads",
                "0",
                "-movflags",
                "+faststart",
                str(part_path),
                ]
            )
            _run_ffmpeg(command)
            rendered_parts.append(part_path)

        concat_file = temp_path / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in rendered_parts),
            encoding="utf-8",
        )
        _run_ffmpeg(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostats",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(output_path),
            ]
        )

    return output_path


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
                cuts = extract_video_cuts(draft_file)
                result = render_cuts(cuts, output_file)
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
        text="Renderiza cortes de vídeo. Efeitos, textos e transições do CapCut não são aplicados.",
        wraplength=470,
    ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
    root.mainloop()


def main() -> None:
    if len(sys.argv) == 1:
        launch_gui()
        return

    argument_parser = argparse.ArgumentParser(
        description="Renderiza os cortes de vídeo de um rascunho do CapCut."
    )
    argument_parser.add_argument("draft", help="Caminho para draft_content.json")
    argument_parser.add_argument("output", help="Caminho do vídeo MP4 de saída")
    argument_parser.add_argument(
        "--ffmpeg", help="Executável do FFmpeg (detectado automaticamente por padrão)"
    )
    args = argument_parser.parse_args()

    cuts = extract_video_cuts(args.draft)
    output_path = render_cuts(cuts, args.output, ffmpeg=args.ffmpeg)
    print(f"Vídeo renderizado: {output_path}")


if __name__ == "__main__":
    main()