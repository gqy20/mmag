#!/usr/bin/env python3
"""Render the Mattermost demo with MMX narration and burned Chinese subtitles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

TITLE_FONT = "Sarasa Gothic SC SemiBold"
CAPTION_FONT = "Sarasa UI SC SemiBold"
DEFAULT_VOICE = "Chinese (Mandarin)_Warm_Bestie"
DEFAULT_SPEECH_MODEL = "speech-2.8-hd"
SPEECH_NORMALIZATION = (
    "acompressor=threshold=-22dB:ratio=2.0:attack=8:release=120:makeup=1.0,"
    "loudnorm=I=-20:TP=-3:LRA=7,alimiter=limit=0.90")
MASTER_LUFS = -16.0
MASTER_TRUE_PEAK = -2.2
MASTER_LRA = 7.0
CHAPTERS = {
    "cli-discovery": (
        "01",
        "能力发现",
        "通过 Mattermost 原生命令查看 Agent、Skill 与运行状态",
    ),
    "personal-workspace": (
        "02",
        "个人工作台",
        "集中管理个人 Skill、案例、记忆与版本",
    ),
    "personal-run": (
        "03",
        "个人 Skill",
        "复用已沉淀的工作方法，并将优秀结果保存为案例",
    ),
    "persona-request": (
        "04",
        "个人数字人",
        "只使用本人明确发布的资料，代表本人回答他人的问题",
    ),
    "meeting-request": (
        "05",
        "多人协作总结",
        "在群聊中读取真实讨论，归纳共识、分歧与后续行动",
    ),
    "ppt-request": (
        "06",
        "真人决策与交付",
        "智能体完成前置整理，关键动作由真人批准后执行",
    ),
    "ppt-preview": (
        "07",
        "原生结果交互",
        "预览生成结果，并交付图片与可编辑 PPTX 文件",
    ),
}

NORMAL_SPEED_PREFIXES = ("personal-version-action", "personal-save-action", "ppt-approve-", "ppt-preview")
READING_SPEED_PREFIXES = ("cli-", "personal-workspace", "personal-cases", "personal-memory")
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
    result_speed: float,
    reading_speed: float,
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
        source_duration = duration(clip.source)
        focus = "" if clip.name.startswith("cli-") or clip.name == "ppt-preview" else "crop=1300:900:300:0,"
        common = (
            f"{focus}fps=25,scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x111827,"
            f"drawtext=font='{TITLE_FONT}':text='{clip.label}':"
            "fontcolor=white:fontsize=34:box=1:boxcolor=0x0f172a@0.78:"
            "boxborderw=14:x=40:y=40:enable='between(t,0,2.2)'"
        )
        if clip.name.startswith(NORMAL_SPEED_PREFIXES):
            video_filter = f"setpts=PTS-STARTPTS,{common}"
        elif clip.name.startswith(READING_SPEED_PREFIXES):
            video_filter = f"setpts=(PTS-STARTPTS)/{reading_speed},{common}"
        else:
            split_at = source_duration * 0.8
            video_filter = (
                f"[0:v]split=2[head][tail];"
                f"[head]trim=start=0:end={split_at:.6f},setpts=(PTS-STARTPTS)/{speed}[headv];"
                f"[tail]trim=start={split_at:.6f},setpts=(PTS-STARTPTS)/{result_speed}[tailv];"
                f"[headv][tailv]concat=n=2:v=1:a=0,{common}[video]"
            )
        args = ["ffmpeg", "-y", "-v", "error", "-i", str(clip.source)]
        if clip.name.startswith(NORMAL_SPEED_PREFIXES + READING_SPEED_PREFIXES):
            args.extend(["-vf", video_filter])
        else:
            args.extend(["-filter_complex", video_filter, "-map", "[video]"])
        args.extend([
            "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
            "-pix_fmt", "yuv420p", str(output),
        ])
        command(args)
        clip_duration = duration(output)
        timed_clips.append(Clip(clip.source, clip.label, clip.name, cursor, cursor + clip_duration))
        cursor += clip_duration
        rendered.append(output)

    has_ppt = any(clip.name == "ppt-request" for clip in clips)
    summary = "命令发现 · 个人能力 · 数字人 · 群聊协作 · 真人审批"
    if has_ppt:
        summary += " · 可编辑 PPT 交付"
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


def clip_end(clips: list[Clip], name: str) -> float | None:
    return next((clip.end for clip in clips if clip.name == name), None)


def narration_cues(
    clips: list[Clip], chapter_starts: dict[str, float], end_start: float
) -> list[Cue]:
    cues = [
        Cue(
            "01-intro",
            0.15,
            "让智能走向行动。",
        ),
        Cue(
            "02-discovery",
            chapter_starts.get("cli-discovery", clip_start(clips, "cli-discovery") or 2.5) + 0.12,
            "就在 Mattermost 里。输入一条命令。智能体与技能清晰可见。团队知道它能做什么。也知道该从哪里开始。让智能真正进入工作现场。",
        ),
        Cue(
            "03-personal",
            chapter_starts.get(
                "personal-workspace", clip_start(clips, "personal-workspace") or 2.5
            ) + 0.12,
            "每个人都有自己的方法。它们不该散落在聊天里。技能、案例和长期记忆。",
        ),
    ]
    personal_run = chapter_starts.get("personal-run", clip_start(clips, "personal-run"))
    personal_save = clip_start(clips, "personal-save-action")
    personal_version = clip_start(clips, "personal-version-action")
    personal_cases = clip_start(clips, "personal-cases")
    personal_memory = clip_start(clips, "personal-memory")
    personal_wait = clip_start(clips, "personal-wait-0")
    persona = chapter_starts.get("persona-request", clip_start(clips, "persona-request"))
    meeting = chapter_starts.get("meeting-request", clip_start(clips, "meeting-request"))
    meeting_end = clip_end(clips, "meeting-wait-0")
    ppt = chapter_starts.get("ppt-request", clip_start(clips, "ppt-request"))
    ppt_approval = clip_start(clips, "ppt-approve-1")
    ppt_delivery = clip_start(clips, "ppt-wait-1")
    preview = clip_start(clips, "ppt-preview")
    if persona is not None:
        cues.append(
            Cue(
                "05-persona",
                persona + 0.1,
                "当你暂时不在场。数字人仍能回答问题。它延续你的知识与经验。",
            )
        )
    if meeting is not None:
        cues.append(
            Cue(
                "06-meeting",
                meeting + 0.1,
                "当讨论越来越多。重要信息不再被淹没。MMAG 主动进入群聊。读取当前讨论。把分散信息整理起来。",
            )
        )
    if ppt is not None:
        cues.append(
            Cue(
                "07-decision",
                ppt + 0.1,
                "从理解一份业务需求。到梳理受众与表达重点。智能体先形成演示方案。它完成准备与前置判断。但关键决定仍然属于人。涉及真实文件交付。",
            )
        )
    if preview is not None:
        cues.append(
            Cue(
                "08-preview",
                preview + 0.08,
                "完整演示文稿已经生成。结果直接回到 Mattermost。预览可以即时检查。PPTX 仍然可以编辑。设计也可以继续完善。工作没有停在答案。它真正走向交付。",
            )
        )
    cues.append(
        Cue(
            "09-outro",
            end_start + 0.15,
            "MMAG。让智能走向行动。",
        )
    )
    detail_cues = (
        ("03-version", personal_version, "每次成功完成的任务。都能成为下一次的起点。方法拥有清晰版本。成长也留下完整轨迹。有效经验可以被复用。"),
        ("03-cases", personal_cases, "案例留下真实的工作成果。也留下来自用户的反馈。它开始成为个人能力。"),
        ("03-memory", personal_memory, "它逐渐理解你的偏好。也记住重要的工作背景。但每个人的信息始终隔离。"),
        ("04-result", personal_wait + 8 if personal_wait is not None else None, "智能体交付的不只是回答。而是可以继续使用的成果。来源可以核对。"),
        ("06-result", meeting_end - 5 if meeting_end is not None else None, "提炼共识。识别风险。整理下一步行动。"),
        ("07-approval", ppt_approval, "系统主动请求批准。批准与拒绝清晰可见。每次决定都有记录。未经许可不会外发。效率不以失控为代价。"),
        ("07-delivery", ppt_delivery, "真人批准之后。完整演示文稿开始交付。"),
    )
    cues.extend(Cue(name, start + 0.08, text) for name, start, text in detail_cues if start is not None)
    if personal_run is not None:
        cues.append(
            Cue(
                "04-personal-run",
                personal_run + 0.1,
                "当相似任务再次出现。不必重新解释所有背景。智能体会找到合适方法。理解目标。组织步骤。调用被允许的能力。然后快速进入工作。",
            )
        )
    if personal_save is not None:
        cues.append(
            Cue(
                "04-personal-save",
                personal_save + 0.08,
                "满意的结果可以一键保存。它会成为新的个人案例。成功经验持续积累。相似任务自动复用。也会随着工作一起成长。",
            )
        )
    return sorted(cues, key=lambda cue: cue.start)


def format_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def narration_sentences(text: str, limit: int) -> list[str]:
    if limit < 4:
        raise ValueError("subtitle character limit must be at least 4")
    clean = re.sub(r"<#[^>]+#>|\s+", " ", text).strip()
    sentences = [item.strip() for item in re.findall(r"[^。！？；!?;]+[。！？；!?;]?", clean)]
    too_long = [item for item in sentences if len(item) > limit]
    if too_long:
        raise ValueError(
            f"narration sentence exceeds {limit} characters; rewrite it semantically: {too_long[0]}"
        )
    return sentences


def synthesize_narration(
    run_dir: Path,
    cues: list[Cue],
    total_duration: float,
    *,
    voice: str,
    speech_model: str,
    speech_speed: float,
    speech_pitch: int,
    subtitle_start: float,
    subtitle_max_chars: int,
    sentence_gap: float,
) -> tuple[list[tuple[Path, float]], Path, float]:
    if not 0.01 <= sentence_gap <= 99.99:
        raise ValueError("sentence gap must be between 0.01 and 99.99 seconds")
    narration_dir = run_dir / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)
    sentence_dir = narration_dir / "sentences"
    sentence_dir.mkdir(parents=True, exist_ok=True)
    audio_inputs: list[tuple[Path, float]] = []
    combined_subtitles: list[Subtitle] = []
    manifest: list[dict[str, object]] = []
    for index, cue in enumerate(cues):
        next_start = cues[index + 1].start if index + 1 < len(cues) else total_duration
        available = max(0.8, next_start - cue.start - 0.12)
        sentences = narration_sentences(cue.text, subtitle_max_chars)
        segments: list[tuple[str, Path, float]] = []
        for sentence_index, sentence in enumerate(sentences, start=1):
            stem = f"{cue.name}-{sentence_index:02d}"
            audio = sentence_dir / f"{stem}.mp3"
            cache = sentence_dir / f"{stem}.json"
            signature = {
                "text": sentence, "voice": voice, "model": speech_model,
                "speed": speech_speed, "pitch": speech_pitch,
            }
            cached = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else None
            if cached != signature or not audio.is_file():
                for attempt in range(3):
                    try:
                        command([
                            "mmx", "speech", "synthesize", "--model", speech_model,
                            "--voice", voice, "--text", sentence, "--speed", str(speech_speed),
                            "--volume", "1", "--pitch", str(speech_pitch), "--language", "Chinese",
                            "--format", "mp3", "--sample-rate", "32000", "--bitrate", "128000",
                            "--channels", "1", "--out", str(audio), "--non-interactive", "--quiet",
                            "--output", "json",
                        ])
                        time.sleep(1.1)
                        break
                    except subprocess.CalledProcessError as error:
                        if error.returncode != 10 or attempt == 2:
                            raise
                        time.sleep(15 * (attempt + 1))
                cache.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8")
            normalized = sentence_dir / f"{stem}-norm.mp3"
            norm_cache = sentence_dir / f"{stem}-norm.json"
            norm_signature = {
                "source_sha256": sha256(audio),
                "filter": SPEECH_NORMALIZATION,
            }
            cached_norm = (
                json.loads(norm_cache.read_text(encoding="utf-8"))
                if norm_cache.is_file()
                else None
            )
            if cached_norm != norm_signature or not normalized.is_file():
                command([
                    "ffmpeg", "-y", "-v", "error", "-i", str(audio),
                    "-af", SPEECH_NORMALIZATION, "-ar", "44100", "-ac", "1",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(normalized),
                ])
                norm_cache.write_text(
                    json.dumps(norm_signature, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            segments.append((sentence, normalized, duration(normalized)))
        source_duration = sum(item[2] for item in segments) + sentence_gap * max(0, len(segments) - 1)
        if source_duration > available + 0.02:
            raise ValueError(
                f"narration cue {cue.name} exceeds its visual window: "
                f"{source_duration:.3f}s > {available:.3f}s; shorten the narration"
            )
        cursor = cue.start
        segment_manifest: list[dict[str, object]] = []
        for sentence, audio, normalized_duration in segments:
            audio_inputs.append((audio, cursor))
            if cue.start >= subtitle_start:
                display = sentence[:-1] if sentence.endswith(("。", ".")) else sentence
                combined_subtitles.append(Subtitle(cursor, cursor + normalized_duration, display))
            segment_manifest.append({
                "text": sentence,
                "normalized_duration_seconds": round(normalized_duration, 3),
            })
            cursor += normalized_duration + sentence_gap
        manifest.append(
            {
                "name": cue.name,
                "start_seconds": round(cue.start, 3),
                "available_seconds": round(available, 3),
                "source_duration_seconds": round(source_duration, 3),
                "tempo_factor": 1.0,
                "sentence_gap_seconds": sentence_gap,
                "normalization": SPEECH_NORMALIZATION,
                "text": cue.text,
                "sentences": segment_manifest,
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
    spoken = sum(duration(audio) for audio, _ in audio_inputs)
    coverage = min(1.0, spoken / total_duration) if total_duration else 0.0
    return audio_inputs, combined, coverage


def measure_loudness(path: Path) -> dict[str, str]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
            f"loudnorm=I={MASTER_LUFS}:TP={MASTER_TRUE_PEAK}:LRA={MASTER_LRA}:print_format=json",
            "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r'\{\s*"input_i"[\s\S]*?\}', result.stderr)
    if not match:
        raise RuntimeError(f"could not parse loudness statistics for {path}")
    stats = json.loads(match.group(0))
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if any(not stats.get(key) for key in required):
        raise RuntimeError(f"incomplete loudness statistics for {path}")
    return stats


def master_loudnorm_filter(stats: dict[str, str]) -> str:
    return (
        f"loudnorm=I={MASTER_LUFS}:TP={MASTER_TRUE_PEAK}:LRA={MASTER_LRA}:"
        f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:linear=true:print_format=summary,"
        "alimiter=limit=0.84:level=false"
    )


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
    base_index = len(audio_inputs)
    mix_args.extend([
        "-f", "lavfi", "-t", f"{duration(silent):.6f}", "-i",
        "anullsrc=channel_layout=mono:sample_rate=48000"])
    audio_filters.append(f"[{base_index}:a]anull[base]")
    audio_names.append("[base]")
    subtitle_path = str(subtitles.resolve()).replace("\\", "\\\\").replace(":", "\\:")
    video_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Sarasa UI SC,FontSize=12,PrimaryColour=&H00FFFFFF,"
        "BackColour=&H00000000,BorderStyle=1,Outline=0.8,Shadow=0,Alignment=2,MarginV=20'"
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
    premaster_stats = measure_loudness(mix)
    master_filter = master_loudnorm_filter(premaster_stats)
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
            "-af",
            master_filter,
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
    final_stats = measure_loudness(output)
    (subtitles.parent / "loudness.json").write_text(
        json.dumps(
            {
                "segment_target": {"integrated_lufs": -20, "true_peak_dbtp": -3, "lra": 7},
                "master_target": {
                    "integrated_lufs": MASTER_LUFS,
                    "true_peak_dbtp": MASTER_TRUE_PEAK,
                    "lra": MASTER_LRA,
                },
                "premaster": premaster_stats,
                "final": final_stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-name", required=True)
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--result-speed", type=float, default=3.0)
    parser.add_argument("--reading-speed", type=float, default=2.0)
    parser.add_argument("--title-duration", type=float, default=2.5)
    parser.add_argument("--chapter-duration", type=float, default=1.5)
    parser.add_argument("--speech-model", default=DEFAULT_SPEECH_MODEL)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--speech-speed", type=float, default=1.08)
    parser.add_argument("--speech-pitch", type=int, default=-1)
    parser.add_argument("--subtitle-max-chars", type=int, default=18)
    parser.add_argument("--sentence-gap", type=float, default=0.16)
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
        result_speed=args.result_speed,
        reading_speed=args.reading_speed,
        title_duration=args.title_duration,
        chapter_duration=args.chapter_duration,
        width=args.width,
        height=args.height,
    )
    cues = narration_cues(timed_clips, chapter_starts, end_start)
    audio_inputs, subtitles, narration_coverage = synthesize_narration(
        run_dir,
        cues,
        total_duration,
        voice=args.voice,
        speech_model=args.speech_model,
        speech_speed=args.speech_speed,
        speech_pitch=args.speech_pitch,
        subtitle_start=args.title_duration,
        subtitle_max_chars=args.subtitle_max_chars,
        sentence_gap=args.sentence_gap,
    )
    output = run_dir / args.output_name
    render_narrated(silent, output, audio_inputs, subtitles)
    print(f"video={output}")
    print(f"subtitles={subtitles}")
    print(f"duration_seconds={duration(output):.6f}")
    print(f"size_bytes={output.stat().st_size}")
    print(f"sha256={sha256(output)}")
    print(f"narration_cues={len(cues)}")
    print(f"narration_coverage={narration_coverage:.1%}")


if __name__ == "__main__":
    main()
