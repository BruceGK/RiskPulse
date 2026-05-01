#!/usr/bin/env python3
"""Transcribe or translate local audio files with OpenAI's audio API.

Usage:
  OPENAI_API_KEY=... python3 scripts/transcribe_audio.py file1.mp3 file2.mp3
  OPENAI_API_KEY=... python3 scripts/transcribe_audio.py --english file.mp3
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import ssl
from pathlib import Path
from uuid import uuid4

try:
    import certifi
except ImportError:  # pragma: no cover - optional local cert helper
    certifi = None


API_BASE = "https://api.openai.com/v1/audio"
MAX_AUDIO_MB = 24.5


def slugify(path: Path) -> str:
    stem = path.stem.strip()
    stem = re.sub(r"[^\w\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem[:90] or "audio"


def multipart_body(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----riskpulse-{uuid4().hex}"
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def call_audio_api(
    *,
    api_key: str,
    file_path: Path,
    model: str,
    language: str | None,
    english: bool,
) -> str:
    endpoint = "translations" if english else "transcriptions"
    fields = {
        "model": model,
        "response_format": "json",
    }
    if language and not english:
        fields["language"] = language

    body, content_type = multipart_body(fields, file_path)
    request = urllib.request.Request(
        f"{API_BASE}/{endpoint}",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
        },
        method="POST",
    )

    context = None
    if certifi is not None:
        context = ssl.create_default_context(cafile=certifi.where())

    try:
        with urllib.request.urlopen(request, timeout=180, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError(f"OpenAI API returned no transcript text: {payload}")
    return text.strip()


def split_audio(file_path: Path, chunk_minutes: int) -> list[Path]:
    if chunk_minutes <= 0:
        return [file_path]

    temp_dir = Path(tempfile.mkdtemp(prefix=f"riskpulse-transcribe-{slugify(file_path)}-"))
    chunk_pattern = temp_dir / "chunk_%03d.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(file_path),
            "-f",
            "segment",
            "-segment_time",
            str(chunk_minutes * 60),
            "-reset_timestamps",
            "1",
            "-map",
            "0:a",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "48k",
            str(chunk_pattern),
        ],
        check=True,
    )
    chunks = sorted(temp_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError(f"ffmpeg did not create chunks for {file_path}")
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe local audio files.")
    parser.add_argument("files", nargs="+", type=Path, help="Audio files to transcribe")
    parser.add_argument("--out-dir", default="transcripts", type=Path)
    parser.add_argument("--model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--language", default="zh", help="ISO language code for transcription")
    parser.add_argument("--english", action="store_true", help="Translate audio into English text")
    parser.add_argument(
        "--chunk-minutes",
        default=10,
        type=int,
        help="Split long files before transcription; use 0 to disable.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "en" if args.english else args.language

    for file_path in args.files:
        file_path = file_path.expanduser().resolve()
        if not file_path.exists():
            print(f"Missing file: {file_path}", file=sys.stderr)
            return 1

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_AUDIO_MB:
            print(
                f"{file_path.name} is {size_mb:.1f} MB; split/compress before upload.",
                file=sys.stderr,
            )
            return 1

        print(f"Transcribing {file_path.name} ({size_mb:.1f} MB)...", flush=True)
        chunks = split_audio(file_path, args.chunk_minutes)
        parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            print(f"  part {index}/{len(chunks)}", flush=True)
            parts.append(
                call_audio_api(
                    api_key=api_key,
                    file_path=chunk,
                    model=args.model,
                    language=args.language,
                    english=args.english,
                )
            )
        text = "\n\n".join(parts)
        output_path = args.out_dir / f"{slugify(file_path)}.{suffix}.txt"
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
