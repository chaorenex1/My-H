"""Image limits, fitting, and box coordinate mapping for image judgments (ADR-11 D15).

Jev-style wires (TypeSafe, OpenRouter Decisions) reject images outside strict
per-side/pixel bounds instead of resizing them, so a request image is scaled and
padded into those bounds first and its box answers are mapped back onto the
source image. Workers AI and OpenAI Decisions enforce caps by rejecting the
request; nothing is silently truncated.

Fitting needs only Pillow (a core dependency), no new runtime.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import sys
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from agent.judgment.types import (
    BoxEdges,
    JudgmentImage,
    JudgmentValidationError,
    image_bytes,
    normalized_image_mime,
)

logger = logging.getLogger(__name__)

#: JPEG qualities stepped through when a fitted PNG exceeds the byte cap.
FIT_JPEG_QUALITIES = (90, 80, 70, 60, 50, 40)

WHOLE_IMAGE = BoxEdges(top=0.0, bottom=1.0, left=0.0, right=1.0)


@dataclass(frozen=True)
class ImageLimits:
    """Bounds one wire enforces on its image input; unset fields do not constrain."""

    mime_types: tuple[str, ...] = ()
    max_count: int = 0
    max_bytes: int = 0
    max_total_bytes: int = 0
    max_pixels: int = 0
    min_pixels: int = 0
    min_side: int = 0
    max_side: int = 0
    #: Fit images into the bounds (Jev) instead of rejecting them (Workers AI/OpenAI).
    fit: bool = False


# Jev 1.14 image contract, shared by TypeSafe and OpenRouter Decisions.
JEV_LIMITS = ImageLimits(
    mime_types=("image/png", "image/jpeg"),
    max_bytes=16 * 1024 * 1024,
    max_pixels=2_458_624,
    min_pixels=78_400,
    min_side=280,
    max_side=1568,
    fit=True,
)
WORKERS_AI_LIMITS = ImageLimits(
    mime_types=("image/png", "image/jpeg", "image/webp"),
    max_count=4,
    max_bytes=4 * 1024 * 1024,
    max_total_bytes=8 * 1024 * 1024,
    max_pixels=16_000_000,
)
OPENAI_DECISIONS_LIMITS = ImageLimits(max_count=128)


#: ``judgment.models[].image_input`` bounds, keyed by their omp/camelCase spelling.
_LIMIT_FIELDS = {
    "maxcount": "max_count", "max_count": "max_count",
    "maxbytes": "max_bytes", "max_bytes": "max_bytes",
    "maxtotalbytes": "max_total_bytes", "max_total_bytes": "max_total_bytes",
    "maxpixels": "max_pixels", "max_pixels": "max_pixels",
    "minpixels": "min_pixels", "min_pixels": "min_pixels",
    "minside": "min_side", "min_side": "min_side",
    "maxside": "max_side", "max_side": "max_side",
    "mimetypes": "mime_types", "mime_types": "mime_types",
}


def limits_from_mapping(mapping: Mapping[str, Any], base: ImageLimits | None = None) -> ImageLimits:
    """Override a wire's ``ImageLimits`` with one model's catalog bounds.

    Unknown keys are ignored (catalog entries may carry unrelated metadata); malformed values
    fail loudly so a typo cannot silently widen a model's bounds.
    """
    limits = base or ImageLimits()
    overrides: dict[str, Any] = {}
    for raw_key, raw_value in mapping.items():
        field = _LIMIT_FIELDS.get(str(raw_key).strip().lower().replace("-", "_"))
        if field is None:
            continue
        if field == "mime_types":
            if not isinstance(raw_value, (list, tuple)) or not all(
                    isinstance(item, str) and item.strip() for item in raw_value):
                raise JudgmentValidationError("image_input.mimeTypes must be a list of strings")
            overrides[field] = tuple(item.strip().lower() for item in raw_value)
            continue
        if isinstance(raw_value, bool):
            raise JudgmentValidationError(f"image_input.{raw_key} must be an integer")
        try:
            number = int(raw_value)
        except (TypeError, ValueError):
            raise JudgmentValidationError(f"image_input.{raw_key} must be an integer") from None
        if number < 0:
            raise JudgmentValidationError(f"image_input.{raw_key} must be non-negative")
        overrides[field] = number
    return replace(limits, **overrides)


@dataclass(frozen=True)
class FittedImage:
    """An image sent to a candidate, plus the source's edges on it (padding lies outside)."""

    image: JudgmentImage
    content: BoxEdges


def _pillow():
    try:
        from PIL import Image
    except Exception:  # health: allow BLE001 -- Pillow is a core dep on 3.14+ only; older envs may lack it
        raise JudgmentValidationError(
            "image fitting requires Pillow; install the vision extra") from None
    return Image


def _dimensions(raw: bytes) -> tuple[int, int] | None:
    """Header dimensions; ``None`` when Pillow cannot read the image."""
    try:
        with _pillow().open(io.BytesIO(raw)) as image:
            width, height = image.size
    except Exception:  # health: allow BLE001 -- an unreadable image is a candidate failure, not a crash
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _limit_violation(images: Sequence[JudgmentImage], limits: ImageLimits,
                     *, count_only: bool = False) -> str | None:
    if limits.max_count and len(images) > limits.max_count:
        return f"accepts at most {limits.max_count} images, got {len(images)}"
    if count_only:
        return None
    total_bytes = 0
    for index, image in enumerate(images):
        mime = normalized_image_mime(image.mime_type)
        if limits.mime_types and mime not in limits.mime_types:
            return (f"does not accept {mime} images (images[{index}]); "
                    f"use {', '.join(limits.mime_types)}")
        raw = image_bytes(image)
        total_bytes += len(raw)
        if limits.max_bytes and len(raw) > limits.max_bytes:
            return (f"accepts images up to {limits.max_bytes} bytes; "
                    f"images[{index}] is {len(raw)} bytes")
        if not (limits.max_pixels or limits.min_pixels or limits.min_side or limits.max_side):
            continue
        dimensions = _dimensions(raw)
        if dimensions is None:
            continue
        width, height = dimensions
        area = width * height
        size = f"images[{index}] is {width}x{height}"
        if limits.max_pixels and area > limits.max_pixels:
            return f"accepts images up to {limits.max_pixels} pixels; {size}"
        if limits.min_pixels and area < limits.min_pixels:
            return f"accepts images of at least {limits.min_pixels} pixels; {size}"
        if limits.max_side and max(width, height) > limits.max_side:
            return f"accepts images up to {limits.max_side} pixels per side; {size}"
        if limits.min_side and min(width, height) < limits.min_side:
            return f"accepts images of at least {limits.min_side} pixels per side; {size}"
    if limits.max_total_bytes and total_bytes > limits.max_total_bytes:
        return (f"accepts at most {limits.max_total_bytes} image bytes per request, "
                f"got {total_bytes}")
    return None


def _within(image: JudgmentImage, limits: ImageLimits, max_bytes: int) -> bool:
    if limits.mime_types and normalized_image_mime(image.mime_type) not in limits.mime_types:
        return False
    raw = image_bytes(image)
    if len(raw) > max_bytes:
        return False
    dimensions = _dimensions(raw)
    if dimensions is None:
        return False
    width, height = dimensions
    area = width * height
    if limits.min_side and min(width, height) < limits.min_side:
        return False
    if limits.max_side and max(width, height) > limits.max_side:
        return False
    if limits.min_pixels and area < limits.min_pixels:
        return False
    if limits.max_pixels and area > limits.max_pixels:
        return False
    return True


def _encode(image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def fit_image(image: JudgmentImage, limits: ImageLimits, *, max_bytes: int) -> FittedImage:
    """Scale and pad ``image`` into ``limits``; pad uses the top-left pixel when a side runs short."""
    Image = _pillow()
    raw = image_bytes(image)
    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            rgba = source.convert("RGBA")
    except Exception as exc:  # health: allow BLE001 -- decode failure is a caller-visible validation error
        raise JudgmentValidationError(f"judgment image could not be decoded: {exc}") from None
    if width <= 0 or height <= 0:
        raise JudgmentValidationError("judgment image has no pixels")

    area = width * height
    unlimited = math.inf
    max_side = limits.max_side or unlimited
    max_pixels = limits.max_pixels or unlimited
    min_side = limits.min_side or 1
    min_pixels = limits.min_pixels or 1
    ceiling = min(max_side / max(width, height), math.sqrt(max_pixels / area))
    floor = max(min_side / min(width, height), math.sqrt(min_pixels / area))
    scale = min(floor, ceiling) if floor > 1 else min(1.0, ceiling)
    scaled_width = int(min(max_side, max(1, round(width * scale))))
    scaled_height = int(min(max_side, max(1, round(height * scale))))
    # Rounding both sides up can overshoot the area cap by a sliver; trim the longer side.
    if scaled_width * scaled_height > max_pixels:
        if scaled_width >= scaled_height:
            scaled_width = max(1, int(max_pixels // scaled_height))
        else:
            scaled_height = max(1, int(max_pixels // scaled_width))

    canvas_width = max(scaled_width, min_side)
    canvas_height = max(scaled_height, min_side)
    if canvas_width * canvas_height < min_pixels:
        if canvas_width >= canvas_height:
            canvas_height = math.ceil(min_pixels / canvas_width)
        else:
            canvas_width = math.ceil(min_pixels / canvas_height)
    left = (canvas_width - scaled_width) // 2
    top = (canvas_height - scaled_height) // 2

    if (scaled_width, scaled_height) != (width, height):
        rgba = rgba.resize((scaled_width, scaled_height), Image.LANCZOS)
    canvas = rgba
    if (canvas_width, canvas_height) != (scaled_width, scaled_height):
        canvas = Image.new("RGBA", (canvas_width, canvas_height), rgba.getpixel((0, 0)))
        canvas.paste(rgba, (left, top))

    encoded = _encode(canvas, "PNG")
    mime_type = "image/png"
    if len(encoded) > max_bytes:
        rgb = canvas.convert("RGB")
        smallest = encoded
        for quality in FIT_JPEG_QUALITIES:
            attempt = _encode(rgb, "JPEG", quality=quality)
            mime_type = "image/jpeg"
            if len(attempt) < len(smallest):
                smallest = attempt
            if len(attempt) <= max_bytes:
                encoded = attempt
                break
        else:
            encoded = smallest
    if len(encoded) > max_bytes:
        raise JudgmentValidationError(
            f"judgment image cannot be encoded into {max_bytes} bytes")

    return FittedImage(
        image=JudgmentImage(
            data=base64.b64encode(encoded).decode("ascii"),
            mime_type=mime_type,
            detail=image.detail,
        ),
        content=BoxEdges(
            top=top / canvas_height,
            bottom=(top + scaled_height) / canvas_height,
            left=left / canvas_width,
            right=(left + scaled_width) / canvas_width,
        ),
    )


def images_acceptable(images: Sequence[JudgmentImage], limits: ImageLimits) -> bool:
    """Whether ``limits`` can carry ``images`` at all; size and MIME are fixed by fitting when enabled."""
    if limits.max_count and len(images) > limits.max_count:
        return False
    if limits.fit:
        return True
    for image in images:
        if limits.mime_types and normalized_image_mime(image.mime_type) not in limits.mime_types:
            return False
    return True


def fit_images(images: Sequence[JudgmentImage], limits: ImageLimits | None) -> tuple[FittedImage, ...]:
    """Reject what ``limits`` cannot take; fit what it would reject when ``limits.fit`` is set."""
    if not images:
        return ()
    if limits is None:
        raise JudgmentValidationError("the selected judge candidate does not accept images")
    violation = _limit_violation(images, limits, count_only=True)
    if violation is not None:
        raise JudgmentValidationError(f"judgment image input {violation}")
    if not limits.fit:
        violation = _limit_violation(images, limits)
        if violation is not None:
            raise JudgmentValidationError(f"judgment image input {violation}")
        return tuple(FittedImage(image=image, content=WHOLE_IMAGE) for image in images)

    per_image_cap = limits.max_bytes or math.inf
    if limits.max_total_bytes:
        per_image_cap = min(per_image_cap, limits.max_total_bytes // len(images) or 1)
    cap_bytes = int(per_image_cap) if math.isfinite(per_image_cap) else sys.maxsize
    fitted: list[FittedImage] = []
    for image in images:
        if _within(image, limits, cap_bytes):
            fitted.append(FittedImage(image=image, content=WHOLE_IMAGE))
        else:
            fitted.append(fit_image(image, limits, max_bytes=cap_bytes))
    total = sum(len(image_bytes(item.image)) for item in fitted)
    if limits.max_total_bytes and total > limits.max_total_bytes:
        raise JudgmentValidationError(
            f"judgment image input accepts at most {limits.max_total_bytes} image bytes "
            f"per request, got {total}")
    return tuple(fitted)


def unfit_box(box: BoxEdges, content: BoxEdges) -> BoxEdges:
    """Measure ``box`` (on a fitted canvas) on the source whose edges are ``content``; clamp."""
    width = content.right - content.left
    height = content.bottom - content.top

    def x(edge: float) -> float:
        return min(1.0, max(0.0, (edge - content.left) / width)) if width else 0.0

    def y(edge: float) -> float:
        return min(1.0, max(0.0, (edge - content.top) / height)) if height else 0.0

    return BoxEdges(
        top=y(box.top), bottom=y(box.bottom), left=x(box.left), right=x(box.right))
