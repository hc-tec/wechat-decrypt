r"""
微信语音解码模块

微信语音通常以 SILK_V3 格式存储（部分版本在数据前会带少量前缀字节），
可通过查找 `#!SILK_V3` 标记截断得到标准 Silk 流，再解码为 WAV 供播放/转写。

本模块只做“语音字节 → 可播放音频文件”的通用能力：
  - SILK_V3 → WAV (依赖 pilk)
  - AMR → WAV (依赖 ffmpeg，可选)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple


SILK_MARKERS = (b"#!SILK_V3", b"#!SILK_V4", b"#!SILK")
AMR_MAGIC = b"#!AMR"


def normalize_wechat_silk(data: bytes) -> bytes:
    """去掉微信 Silk 语音的前缀字节，返回以 #!SILK_* 开头的标准流（best-effort）。"""
    if not data:
        return b""
    for marker in SILK_MARKERS:
        idx = data.find(marker)
        if idx >= 0:
            return data[idx:]
    return data


def detect_audio_format(data: bytes) -> str:
    """根据头部字节猜测音频格式（best-effort）。"""
    if not data:
        return "unknown"
    if any(data.startswith(m) for m in SILK_MARKERS) or any(m in data[:64] for m in SILK_MARKERS):
        return "silk"
    if data.startswith(AMR_MAGIC):
        return "amr"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return "mp3"
    return "unknown"


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def decode_voice_bytes_to_wav(
    audio_data: bytes,
    out_wav_path: str,
    *,
    sample_rate: int = 24000,
    ffmpeg_bin: str = "ffmpeg",
) -> Tuple[Optional[str], Optional[str]]:
    """将语音字节解码为 WAV 文件。

    Returns:
        (wav_path, src_format) 或 (None, None)
    """
    if not audio_data:
        return None, None

    src_fmt = detect_audio_format(audio_data)
    _ensure_parent_dir(out_wav_path)

    if src_fmt == "wav":
        with open(out_wav_path, "wb") as f:
            f.write(audio_data)
        return out_wav_path, "wav"

    if src_fmt == "silk":
        try:
            import pilk
        except Exception:
            return None, None

        normalized = normalize_wechat_silk(audio_data)
        fd, silk_path = tempfile.mkstemp(suffix=".silk")
        os.close(fd)
        try:
            with open(silk_path, "wb") as f:
                f.write(normalized)
            pilk.silk_to_wav(silk_path, out_wav_path, rate=sample_rate)
            if os.path.exists(out_wav_path) and os.path.getsize(out_wav_path) > 0:
                return out_wav_path, "silk"
        finally:
            try:
                os.unlink(silk_path)
            except OSError:
                pass
        return None, None

    if src_fmt == "amr":
        # ffmpeg supports AMR if built with libopencore-amr.
        if not shutil.which(ffmpeg_bin):
            return None, None
        fd, amr_path = tempfile.mkstemp(suffix=".amr")
        os.close(fd)
        try:
            with open(amr_path, "wb") as f:
                f.write(audio_data)
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", amr_path, out_wav_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if os.path.exists(out_wav_path) and os.path.getsize(out_wav_path) > 0:
                return out_wav_path, "amr"
        finally:
            try:
                os.unlink(amr_path)
            except OSError:
                pass
        return None, None

    return None, None

