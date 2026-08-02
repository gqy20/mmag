#!/usr/bin/env python3
"""Render the Mattermost demo with MMX narration and burned Chinese subtitles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TITLE_FONT = "Sarasa Gothic SC SemiBold"
CAPTION_FONT = "Sarasa UI SC SemiBold"
DEFAULT_VOICE = "Chinese (Mandarin)_Reliable_Executive"
DEFAULT_SPEECH_MODEL = "speech-2.8-hd"
CHAPTERS = {
    "default-get": (
        "01",
        "即时智能协作",
        "用户提出请求，系统在同一个 Thread 中确认、处理并交付结果",
    ),
    "project-request": (
        "02",
        "受控知识沉淀",
        "重要决策经过真人确认后写入，并通过独立查询验证",
    ),
    "ppt-request": (
        "03",
        "可编辑演示文稿交付",
        "自动生成 PPT，文件外发经过审批，并在 Mattermost 中直接预览",
    ),
}
SRT_TIMING = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass(frozen=True)
class Clip:
    source: Path
    label: str
    name: str
    start: float = 0.0
    end: float = 0.0


@dataclass(frozen=True)
class Cue:
    name: str
    start: float
    text: str


@dataclass(frozen=True)
class Subtitle:
    start: float
    end: float
    text: str


def command(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def duration(path: Path) -> float:
    return float(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture=True,
        )
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_clips(run_dir: Path) -> list[Clip]:
    manifest = run_dir / "clips.tsv"
    if not manifest.is_file():
        raise SystemExit(f"Recording manifest is missing: {manifest}")
    clips: list[Clip] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        source_text, label = line.split("\t", 1)
        source = Path(source_text).resolve()
        if not source.is_file():
            raise SystemExit(f"Recorded clip is missing: {source}")
        name = re.sub(r"^\d+-", "", source.stem)
        clips.append(Clip(source, label, name))
    if not clips:
        raise SystemExit("Recording manifest contains no clips")
    return clips


def render_chapter_card(
    output: Path,
    *,
    number: str,
    title: str,
    summary: str,
    card_duration: float,
    width: int,
    height: int,
) -> None:
    fade_out = max(0.0, card_duration - 0.2)
    command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x111827:s={width}x{height}:d={card_duration}:r=25",
            "-vf",
            (
                f"drawtext=font='{CAPTION_FONT}':text='CHAPTER {number}':"
                "fontcolor=0x60a5fa:fontsize=32:x=(w-text_w)/2:y=480,"
                f"drawtext=font='{TITLE_FONT}':text='{title}':"
                "fontcolor=white:fontsize=76:x=(w-text_w)/2:y=570,"
                f"drawtext=font='{CAPTION_FONT}':text='{summary}':"
                "fontcolor=0xcbd5e1:fontsize=34:x=(w-text_w)/2:y=700,"
                f"fade=t=in:st=0:d=0.2,fade=t=out:st={fade_out}:d=0.2"
            ),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
    )


def render_silent(
    run_dir: Path,
    clips: list[Clip],
    *,
    speed: float,
    title_duration: float,
    chapter_duration: float,
    width: int,
    height: int,
) -> tuple[Path, list[Clip], dict[str, float], float, float]:
    edit_dir = run_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    title = edit_dir / "00-title.mp4"
    command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x111827:s={width}x{height}:d={title_duration}:r=25",
            "-vf",
            (
                f"drawtext=font='{TITLE_FONT}':text='MMAG 企业智能体真实演示':"
                "fontcolor=white:fontsize=84:x=(w-text_w)/2:y=575,"
                f"drawtext=font='{CAPTION_FONT}':"
                "text='Mattermost · Agent · Skill · Approval · PPT Artifact':"
                "fontcolor=0x93c5fd:fontsize=46:x=(w-text_w)/2:y=695"
            ),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(title),
        ]
    )
    rendered: list[Path] = [title]
    cursor = duration(title)
    timed_clips: list[Clip] = []
    chapter_starts: dict[str, float] = {}
    for index, clip in enumerate(clips, start=1):
        chapter = CHAPTERS.get(clip.name)
        if chapter is not None:
            number, chapter_title, chapter_summary = chapter
            chapter_output = edit_dir / f"chapter-{number}.mp4"
            chapter_starts[clip.name] = cursor
            render_chapter_card(
                chapter_output,
                number=number,
                title=chapter_title,
                summary=chapter_summary,
                card_duration=chapter_duration,
                width=width,
                height=height,
            )
            cursor += duration(chapter_output)
            rendered.append(chapter_output)
        output = edit_dir / f"{index:02d}.mp4"
        command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(clip.source),
                "-vf",
                (
                    f"setpts=PTS/{speed},fps=25,scale={width}:{height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x111827,"
                    f"drawtext=font='{TITLE_FONT}':text='{clip.label}':"
                    "fontcolor=white:fontsize=42:box=1:boxcolor=black@0.65:"
                    "boxborderw=22:x=48:y=48"
                ),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ]
        )
        clip_duration = duration(output)
        timed_clips.append(Clip(clip.source, clip.label, clip.name, cursor, cursor + clip_duration))
        cursor += clip_duration
        rendered.append(output)

    has_ppt = any(clip.name == "ppt-request" for clip in clips)
    summary = (
        "默认 get · 真实 Agent · 人工审批 · PPT 预览与附件"
        if has_ppt
        else "默认 get · 真实 Agent · 人工审批 · 知识写入"
    )
    end_start = cursor
    end = edit_dir / "99-end.mp4"
    command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x111827:s={width}x{height}:d=5:r=25",
            "-vf",
            (
                f"drawtext=font='{TITLE_FONT}':text='真实流程完成':"
                "fontcolor=white:fontsize=88:x=(w-text_w)/2:y=585,"
                f"drawtext=font='{CAPTION_FONT}':text='{summary}':"
                "fontcolor=0x86efac:fontsize=46:x=(w-text_w)/2:y=710"
            ),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(end),
        ]
    )
    rendered.append(end)
    concat_file = edit_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in rendered),
        encoding="utf-8",
    )
    silent = run_dir / "mmag-demo-silent.mp4"
    command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(silent),
        ]
    )
    return silent, timed_clips, chapter_starts, end_start, duration(silent)


def clip_start(clips: list[Clip], name: str) -> float | None:
    return next((clip.start for clip in clips if clip.name == name), None)


def narration_cues(
    clips: list[Clip], chapter_starts: dict[str, float], end_start: float
) -> list[Cue]:
    cues = [
        Cue("01-intro", 0.15, "这是 MMAG，企业智能体真实演示。"),
        Cue(
            "02-mmchat",
            chapter_starts.get("default-get", clip_start(clips, "default-get") or 2.5) + 0.12,
            "消息立即收到 get 回执。<#0.15#>LangGraph 完成路由，结果更新在同一个 Thread。",
        ),
    ]
    project = chapter_starts.get("project-request", clip_start(clips, "project-request"))
    verify = clip_start(clips, "project-verify-request")
    ppt = chapter_starts.get("ppt-request", clip_start(clips, "ppt-request"))
    preview = clip_start(clips, "ppt-preview")
    if project is not None:
        cues.append(
            Cue(
                "03-project",
                project + 0.1,
                "Project Agent 准备写入知识。<#0.15#>没有用户批准，写操作不会执行。",
            )
        )
    if verify is not None:
        cues.append(
            Cue(
                "04-verify",
                verify + 0.1,
                "批准后，原运行恢复并写入知识库。<#0.15#>独立回读证明结果已经持久化。",
            )
        )
    if ppt is not None:
        cues.append(
            Cue(
                "05-ppt",
                ppt + 0.1,
                "PPT Agent 绑定 slides Skill，生成可编辑演示稿。<#0.15#>文件外发再次经过人工审批和标识核验。",
            )
        )
    if preview is not None:
        cues.append(
            Cue(
                "06-preview",
                preview + 0.08,
                "点击预览图，直接检查生成结果。",
            )
        )
    cues.append(
        Cue(
            "07-outro",
            end_start + 0.15,
            "完整闭环已经跑通。<#0.15#>能力可控，过程可审计，结果可交付。",
        )
    )
    return sorted(cues, key=lambda cue: cue.start)


def parse_time(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def parse_srt(path: Path) -> list[Subtitle]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    subtitles: list[Subtitle] = []
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if SRT_TIMING.fullmatch(line)), None)
        if timing_index is None:
            continue
        match = SRT_TIMING.fullmatch(lines[timing_index])
        assert match is not None
        text = "\n".join(lines[timing_index + 1 :]).strip()
        if text:
            subtitles.append(Subtitle(parse_time(match["start"]), parse_time(match["end"]), text))
    return subtitles


def atempo_filter(factor: float) -> str:
    filters: list[str] = []
    while factor > 2.0:
        filters.append("atempo=2.0")
        factor /= 2.0
    filters.append(f"atempo={factor:.6f}")
    return ",".join(filters)


def synthesize_narration(
    run_dir: Path,
    cues: list[Cue],
    total_duration: float,
    *,
    voice: str,
    speech_model: str,
    speech_speed: float,
    speech_pitch: int,
) -> tuple[list[tuple[Path, float]], Path]:
    narration_dir = run_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    audio_inputs: list[tuple[Path, float]] = []
    combined_subtitles: list[Subtitle] = []
    manifest: list[dict[str, object]] = []
    for index, cue in enumerate(cues):
        next_start = cues[index + 1].start if index + 1 < len(cues) else total_duration
        available = max(0.8, next_start - cue.start - 0.12)
        audio = narration_dir / f"{cue.name}.mp3"
        source_srt = narration_dir / f"{cue.name}.srt"
        cache = narration_dir / f"{cue.name}.json"
        signature = {
            "text": cue.text,
            "voice": voice,
            "model": speech_model,
            "speed": speech_speed,
            "pitch": speech_pitch,
        }
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else None
        if cached != signature or not audio.is_file() or not source_srt.is_file():
            command(
                [
                    "mmx",
                    "speech",
                    "synthesize",
                    "--model",
                    speech_model,
                    "--voice",
                    voice,
                    "--text",
                    cue.text,
                    "--speed",
                    str(speech_speed),
                    "--volume",
                    "1",
                    "--pitch",
                    str(speech_pitch),
                    "--language",
                    "Chinese",
                    "--format",
                    "mp3",
                    "--sample-rate",
                    "32000",
                    "--bitrate",
                    "128000",
                    "--channels",
                    "1",
                    "--subtitles",
                    "--out",
                    str(audio),
                    "--non-interactive",
                    "--quiet",
                    "--output",
                    "json",
                ]
            )
            cache.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8")
        source_duration = duration(audio)
        factor = max(1.0, source_duration / available)
        fitted = narration_dir / f"{cue.name}-fit.mp3"
        if factor > 1.005:
            command(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(audio),
                    "-filter:a",
                    atempo_filter(factor),
                    str(fitted),
                ]
            )
        else:
            fitted = audio
        audio_inputs.append((fitted, cue.start))
        entries = parse_srt(source_srt)
        if not entries:
            plain_text = re.sub(r"<#[^>]+#>", " ", cue.text).strip()
            entries = [Subtitle(0.0, source_duration, plain_text)]
        for entry in entries:
            combined_subtitles.append(
                Subtitle(
                    cue.start + entry.start / factor,
                    min(next_start - 0.05, cue.start + entry.end / factor),
                    entry.text,
                )
            )
        manifest.append(
            {
                "name": cue.name,
                "start_seconds": round(cue.start, 3),
                "available_seconds": round(available, 3),
                "source_duration_seconds": round(source_duration, 3),
                "tempo_factor": round(factor, 4),
                "text": cue.text,
            }
        )

    combined = narration_dir / "mmag-demo.srt"
    combined.write_text(
        "\n\n".join(
            f"{index}\n{format_time(item.start)} --> {format_time(item.end)}\n{item.text}"
            for index, item in enumerate(combined_subtitles, start=1)
        )
        + "\n",
        encoding="utf-8",
    )
    (narration_dir / "narration.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (narration_dir / "narration.txt").write_text(
        "\n".join(f"{cue.name}\t{cue.text}" for cue in cues) + "\n", encoding="utf-8"
    )
    return audio_inputs, combined


def render_narrated(
    silent: Path,
    output: Path,
    audio_inputs: list[tuple[Path, float]],
    subtitles: Path,
) -> None:
    mix = subtitles.parent / "narration-mix.wav"
    mix_args = ["ffmpeg", "-y", "-v", "error"]
    for audio, _ in audio_inputs:
        mix_args.extend(["-i", str(audio)])
    audio_filters: list[str] = []
    audio_names: list[str] = []
    for index, (_, start) in enumerate(audio_inputs):
        delay = round(start * 1000)
        name = f"voice{index}"
        audio_filters.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=mono,"
            f"adelay={delay}:all=1[{name}]"
        )
        audio_names.append(f"[{name}]")
    subtitle_path = str(subtitles.resolve()).replace("\\", "\\\\").replace(":", "\\:")
    video_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Sarasa UI SC,FontSize=14,PrimaryColour=&H00FFFFFF,"
        "BackColour=&H50000000,BorderStyle=3,Outline=1,Shadow=0,Alignment=2,MarginV=28'"
    )
    mix_filter = (
        "".join(audio_names) + f"amix=inputs={len(audio_names)}:duration=longest:normalize=0[audio]"
    )
    mix_args.extend(
        [
            "-filter_complex",
            ";".join([*audio_filters, mix_filter]),
            "-map",
            "[audio]",
            str(mix),
        ]
    )
    command(mix_args)
    command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(silent),
            "-i",
            str(mix),
            "-vf",
            video_filter,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ]
    )
    command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--title-duration", type=float, default=2.5)
    parser.add_argument("--chapter-duration", type=float, default=1.5)
    parser.add_argument("--speech-model", default=DEFAULT_SPEECH_MODEL)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--speech-speed", type=float, default=1.08)
    parser.add_argument("--speech-pitch", type=int, default=-1)
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1440)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    clips = read_clips(run_dir)
    silent, timed_clips, chapter_starts, end_start, total_duration = render_silent(
        run_dir,
        clips,
        speed=args.speed,
        title_duration=args.title_duration,
        chapter_duration=args.chapter_duration,
        width=args.width,
        height=args.height,
    )
    cues = narration_cues(timed_clips, chapter_starts, end_start)
    audio_inputs, subtitles = synthesize_narration(
        run_dir,
        cues,
        total_duration,
        voice=args.voice,
        speech_model=args.speech_model,
        speech_speed=args.speech_speed,
        speech_pitch=args.speech_pitch,
    )
    output = run_dir / args.output_name
    render_narrated(silent, output, audio_inputs, subtitles)
    print(f"video={output}")
    print(f"subtitles={subtitles}")
    print(f"duration_seconds={duration(output):.6f}")
    print(f"size_bytes={output.stat().st_size}")
    print(f"sha256={sha256(output)}")
    print(f"narration_cues={len(cues)}")


if __name__ == "__main__":
    main()
