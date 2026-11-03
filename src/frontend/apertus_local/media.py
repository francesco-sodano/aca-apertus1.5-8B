"""Bound and normalize local media before sending it directly to Apertus."""

from __future__ import annotations

import base64
import io
import wave
from dataclasses import dataclass

import av
from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SAMPLES = 240_000
IMAGE_SIDE = 512


@dataclass(frozen=True)
class LocalAttachment:
    name: str
    mime_type: str
    data_base64: str

    def content_part(self) -> dict[str, object]:
        kind = "image_url" if self.mime_type.startswith("image/") else "audio_url"
        return {
            "type": kind,
            kind: {"url": f"data:{self.mime_type};base64,{self.data_base64}"},
        }


def prepare_attachment(name: str, mime_type: str, data: bytes) -> LocalAttachment:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Each attachment must contain data and be at most 20 MiB.")
    if mime_type.startswith("image/"):
        try:
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > 20_000_000:
                    raise ValueError("Images must contain at most 20 million pixels.")
                original.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
                output = io.BytesIO()
                original.convert("RGB").save(output, format="PNG")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("The uploaded image could not be decoded.") from exc
        normalized = output.getvalue()
        mime_type = "image/png"
    elif mime_type.startswith("audio/"):
        normalized = _normalize_audio(data)
        mime_type = "audio/wav"
    else:
        raise ValueError("Only image and audio attachments are supported.")
    return LocalAttachment(name, mime_type, base64.b64encode(normalized).decode("ascii"))


def _normalize_audio(data: bytes) -> bytes:
    pcm = bytearray()
    try:
        with av.open(io.BytesIO(data)) as source:
            if not source.streams.audio:
                raise ValueError("The attachment contains no audio stream.")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=24_000)

            def append(frames: list[av.AudioFrame]) -> None:
                for frame in frames:
                    if len(pcm) // 2 + frame.samples > MAX_AUDIO_SAMPLES:
                        raise ValueError("Audio must be at most 10 seconds long.")
                    pcm.extend(bytes(frame.planes[0])[: frame.samples * 2])

            for frame in source.decode(source.streams.audio[0]):
                append(resampler.resample(frame))
            append(resampler.resample(None))
    except av.FFmpegError as exc:
        raise ValueError("The uploaded audio could not be decoded.") from exc
    if not pcm:
        raise ValueError("The attachment contains no decoded audio.")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        target.writeframes(pcm)
    return output.getvalue()
