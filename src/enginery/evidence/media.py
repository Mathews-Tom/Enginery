"""Bounded raw-media decoding. Decodability is never a semantic verdict."""

from __future__ import annotations

import struct
import zlib

from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    DecodeStatus,
    MediaDecodeResult,
)

PNG_DECODER_VERSION = "enginery-png/1"
MAX_PNG_DIMENSION = 16384
MAX_DECODED_PNG_BYTES = 64 * 1024 * 1024
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _scanlines(
    width: int, height: int, bits_per_pixel: int, interlace: int,
) -> tuple[tuple[int, int], ...]:
    passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
    rows: list[tuple[int, int]] = []
    for x, y, dx, dy in passes:
        columns = max(0, (width - x + dx - 1) // dx)
        count = max(0, (height - y + dy - 1) // dy)
        if columns and count:
            rows.append(((columns * bits_per_pixel + 7) // 8 + 1, count))
    return tuple(rows)


def decode_png(data: bytes) -> MediaDecodeResult:
    """Validate PNG structure, CRCs, deflate stream, dimensions and every scanline.

    Both ordinary and Adam7 PNGs are supported, including packed and 16-bit
    samples. Inflation is capped before allocating candidate-controlled pixels.
    No pixel comparison or behavioral assertion is performed here.
    """
    width: int | None = None
    height: int | None = None

    def invalid(reason: str) -> MediaDecodeResult:
        return MediaDecodeResult(DecodeStatus.INVALID, PNG_DECODER_VERSION, width, height, reason)

    if not isinstance(data, bytes) or len(data) > MAX_EVIDENCE_BYTES:
        return invalid("png_input_limit")
    if not data.startswith(_SIGNATURE):
        return invalid("png_signature")
    offset = len(_SIGNATURE)
    chunks = 0
    bit_depth = color_type = interlace = 0
    compressed: list[bytes] = []
    seen_header = seen_palette = seen_idat = ended_idat = seen_end = False
    while offset < len(data):
        chunks += 1
        if chunks > 4096:
            return invalid("png_chunk_limit")
        if len(data) - offset < 12:
            return invalid("png_truncated_chunk")
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return invalid("png_truncated_chunk")
        if any(not (65 <= char <= 90 or 97 <= char <= 122) for char in kind) or kind[2] > 90:
            return invalid("png_chunk_type")
        payload = data[offset + 8:end - 4]
        checksum = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF != checksum:
            return invalid("png_chunk_crc")
        if not seen_header and kind != b"IHDR":
            return invalid("png_missing_ihdr")
        if seen_idat and kind != b"IDAT":
            ended_idat = True
        if kind == b"IHDR":
            if seen_header or length != 13:
                return invalid("png_ihdr")
            w, h, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload,
            )
            if not 0 < w <= MAX_PNG_DIMENSION or not 0 < h <= MAX_PNG_DIMENSION:
                return invalid("png_dimensions")
            width, height = w, h
            if (
                color_type not in _DEPTHS or bit_depth not in _DEPTHS[color_type]
                or compression != 0 or filtering != 0 or interlace not in (0, 1)
            ):
                return invalid("png_ihdr")
            seen_header = True
        elif kind == b"PLTE":
            if (
                seen_palette or seen_idat or not length or length % 3 or length > 768
                or color_type in (0, 4) or (color_type == 3 and length // 3 > 2 ** bit_depth)
            ):
                return invalid("png_palette")
            seen_palette = True
        elif kind == b"IDAT":
            if ended_idat or (color_type == 3 and not seen_palette):
                return invalid("png_idat_order")
            seen_idat = True
            compressed.append(payload)
        elif kind == b"IEND":
            if length or not seen_idat or end != len(data):
                return invalid("png_iend")
            seen_end = True
            break
        elif not kind[0] & 32:
            return invalid("png_unknown_critical_chunk")
        offset = end
    if not seen_end or width is None or height is None:
        return invalid("png_missing_iend")
    rows = _scanlines(width, height, _CHANNELS[color_type] * bit_depth, interlace)
    expected = sum(stride * count for stride, count in rows)
    if expected > MAX_DECODED_PNG_BYTES:
        return invalid("png_decoded_limit")
    try:
        inflater = zlib.decompressobj()
        pixels = inflater.decompress(b"".join(compressed), expected + 1)
    except zlib.error:
        return invalid("png_zlib")
    if (
        not inflater.eof or inflater.unused_data or inflater.unconsumed_tail
        or len(pixels) != expected
    ):
        return invalid("png_scanline_bytes")
    offset = 0
    for stride, count in rows:
        for row in range(count):
            if pixels[offset + row * stride] > 4:
                return invalid("png_scanline_filter")
        offset += stride * count
    return MediaDecodeResult(DecodeStatus.DECODABLE, PNG_DECODER_VERSION, width, height)


def validate_media(data: bytes, media_type: str) -> MediaDecodeResult:
    """Decode supported media; ordinary raw command/JSON captures need no decoder."""
    if media_type == "image/png":
        return decode_png(data)
    if len(data) > MAX_EVIDENCE_BYTES:
        return MediaDecodeResult(DecodeStatus.INVALID, "enginery-media/1", reason="media_input_limit")
    if media_type.startswith(("image/", "audio/", "video/")):
        return MediaDecodeResult(
            DecodeStatus.INVALID, "enginery-media/1", reason="unsupported_media_type",
        )
    return MediaDecodeResult(DecodeStatus.NOT_APPLICABLE, "enginery-media/1")


__all__ = [
    "MAX_DECODED_PNG_BYTES", "MAX_PNG_DIMENSION", "PNG_DECODER_VERSION", "decode_png",
    "validate_media",
]
