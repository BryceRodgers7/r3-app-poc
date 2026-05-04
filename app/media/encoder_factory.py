"""Phase 11.A / 11.B — recording-branch encoder selection.

The pipeline used to hardcode `jpegenc` on the splitmuxsink branch.

- Phase 11.A introduced this factory and wired hardware-MJPEG selection
  (Intel QuickSync via `qsvjpegenc`) with a software fallback to
  `jpegenc`.
- Phase 11.B adds ProRes (`avenc_prores_ks` → `avenc_prores`) and DNxHR
  (`avenc_dnxhd`). Both are software-only on Windows — there is no
  native ProRes hwaccel encoder anywhere, no DNxHR hwaccel encoder
  anywhere, and `proresenc` from `gst-plugins-bad` isn't built into
  UCRT64. The libav-wrapped FFmpeg encoders ride on `gst-libav`.

The hwaccel parameter is honored but has no effect for ProRes/DNxHR;
the reason string is explicit so the operator isn't confused about why
"intel"/"nvidia" don't change anything for those codecs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_HWACCEL_VALUES = ("auto", "none", "nvidia", "intel", "amd")
SOFTWARE_MJPEG_ELEMENT = "jpegenc"
# avenc_prores_ks ("Kostya" implementation) is meaningfully faster than
# avenc_prores at the same target quality; probe it first.
SOFTWARE_PRORES_PRIORITY = ("avenc_prores_ks", "avenc_prores")
SOFTWARE_DNXHR_ELEMENT = "avenc_dnxhd"

# Phase 11.C — per-codec defaults applied when no
# `[recording.encoder_settings]` block is configured. The profile
# names map to GStreamer-libav integer enum values via
# `_PRORES_PROFILE_INT` / `_DNXHR_PROFILE_INT` below. These are the
# *named* defaults; the int translation happens in
# `_factory_args_for_element` at pipeline-build time.
_BUILTIN_ENCODER_SETTINGS: dict[str, dict[str, Any]] = {
    "mjpeg": {"quality": 85},
    "prores": {"profile": "lt"},
    "dnxhr": {"profile": "lb"},
}

# `avenc_prores_ks profile` enum from `gst-inspect-1.0`:
#   (0=proxy, 1=lt, 2=standard, 3=hq, 4=4444, 5=4444xq).
# Phase 11.B accidentally set 2 (standard) when the architecture-doc
# §5.2 disk-budget coefficient (0.25, ProRes 422 LT) was sized
# against profile 1 — 11.C corrects this via the profile-name TOML
# surface.
_PRORES_PROFILE_INT: dict[str, int] = {
    "proxy": 0,
    "lt": 1,
    "standard": 2,
    "hq": 3,
}

# `avenc_dnxhd profile` enum:
#   (0=dnxhd legacy, 1=dnxhr_lb, 2=dnxhr_sq, 3=dnxhr_hq, 4=dnxhr_hqx, 5=dnxhr_444).
# Phase 11.B used a `bitrate` workaround because the encoder's
# 200000-bps default is nonsensical. The profile enum is the right
# surface — picking it lets the encoder compute bitrate from
# profile + input caps, no manual tuning needed.
_DNXHR_PROFILE_INT: dict[str, int] = {
    "lb": 1,
    "sq": 2,
    "hq": 3,
}

# Operator-friendly install hint surfaced when ProRes / DNxHR encoders
# aren't registered. UCRT64 ships `gst-libav` as a separate pacman
# package from the GStreamer core.
_LIBAV_INSTALL_HINT = (
    "Install `mingw-w64-ucrt-x86_64-gst-libav` via pacman to enable "
    "ProRes / DNxHR encoding."
)


@dataclass(frozen=True, slots=True)
class EncoderSelection:
    """The element name (and optional factory args) chosen for the record branch.

    `reason` is a short human-readable string used verbatim in the
    startup INFO log and the diagnostics widget — the operator can see
    at a glance which encoder ran and why.

    `is_software_fallback=True` means a hwaccel candidate was tried (or
    would have been tried at higher priority) and the chosen element
    is software. ProRes / DNxHR with software encoders set this False
    because no hwaccel candidate exists on the platform — software is
    the canonical pick, not a fallback.
    """

    element_name: str
    is_software_fallback: bool
    requested_hwaccel: str
    reason: str
    factory_args: dict[str, Any] = field(default_factory=dict)


def _priority_for_codec(codec: str, hwaccel: str) -> tuple[str, ...]:
    """Element-probe priority for `(codec, hwaccel)`."""
    if codec == "mjpeg":
        if hwaccel in ("auto", "intel"):
            return ("qsvjpegenc", SOFTWARE_MJPEG_ELEMENT)
        # NVENC has no MJPEG element on Windows builds; AMD likewise.
        return (SOFTWARE_MJPEG_ELEMENT,)
    if codec == "prores":
        return SOFTWARE_PRORES_PRIORITY
    if codec == "dnxhr":
        return (SOFTWARE_DNXHR_ELEMENT,)
    raise NotImplementedError(
        f"encoder_factory does not wire codec={codec!r}"
    )


def _is_software_element(name: str) -> bool:
    return name in {
        SOFTWARE_MJPEG_ELEMENT,
        *SOFTWARE_PRORES_PRIORITY,
        SOFTWARE_DNXHR_ELEMENT,
    }


def _codec_has_hwaccel_option(codec: str) -> bool:
    """True iff this codec has at least one hwaccel candidate on Windows.

    Used to decide whether picking a software element counts as a
    *fallback* (for events / diagnostics) or as the canonical pick.
    Only MJPEG has a hwaccel option (`qsvjpegenc` via Intel QuickSync).
    """
    return codec == "mjpeg"


def _hwaccel_label(hwaccel: str) -> str:
    return {
        "auto": "auto-selected",
        "intel": "Intel",
        "nvidia": "NVIDIA",
        "amd": "AMD",
    }.get(hwaccel, hwaccel)


def _resolve_encoder_settings(
    encoder_settings: dict[str, dict[str, Any]] | None,
    codec: str,
) -> dict[str, Any]:
    """Phase 11.C: merge operator overrides over per-codec defaults.

    `encoder_settings` is the full per-codec dict (keys are codec
    names). When None, the built-in defaults apply unmodified. When
    set, the relevant codec's sub-dict overrides the defaults
    field-by-field — missing fields keep their default.
    """
    base = dict(_BUILTIN_ENCODER_SETTINGS.get(codec, {}))
    if encoder_settings is not None:
        override = encoder_settings.get(codec, {})
        base.update(override)
    return base


def _factory_args_for_element(
    element_name: str,
    codec: str,
    codec_settings: dict[str, Any],
) -> dict[str, Any]:
    """Phase 11.C: translate operator-facing codec settings into the
    GStreamer-element property names + values to apply at construction.

    Profile names (`"lt"`, `"lb"`, etc.) are mapped to the integer
    enum values that `avenc_prores_ks` and `avenc_dnxhd` expose. Other
    elements that don't accept the property silently skip — the
    pipeline-build site already tolerates an empty factory_args dict.
    """
    if element_name == "jpegenc":
        return {"quality": int(codec_settings["quality"])}
    if element_name == "qsvjpegenc":
        # Intel QuickSync's MJPEG encoder also exposes a `quality`
        # property; pass it through verbatim. If a future hwaccel
        # build doesn't accept it, the element-construction path will
        # raise and the build-time link fallback (Phase 11.A) catches it.
        return {"quality": int(codec_settings["quality"])}
    if element_name == "avenc_prores_ks":
        return {
            "profile": _PRORES_PROFILE_INT[codec_settings["profile"]],
        }
    if element_name == "avenc_prores":
        # `avenc_prores` has no profile property exposed via gst-libav
        # mapping; defaults to ProRes 422 standard. No factory_args.
        return {}
    if element_name == "avenc_dnxhd":
        return {
            "profile": _DNXHR_PROFILE_INT[codec_settings["profile"]],
        }
    return {}


def select_encoder(
    *,
    hwaccel: str,
    codec: str,
    gst_module: Any,
    force_software: bool = False,
    encoder_settings: dict[str, dict[str, Any]] | None = None,
) -> EncoderSelection:
    """Pick a recording-branch encoder element for `(hwaccel, codec)`.

    `gst_module` must expose `ElementFactory.find(name)` returning a
    truthy factory when the element is registered. Tests pass a stub.
    `force_software=True` short-circuits MJPEG to `jpegenc` — used by
    the bus-error retry path when an hwaccel encoder fails caps
    negotiation. For ProRes/DNxHR the flag is honored but has no
    effect (the only candidates are already software).

    `encoder_settings` is the per-codec override dict (Phase 11.C). When
    None, the built-in defaults apply.
    """
    requested = (hwaccel or "auto").strip().lower()
    if requested not in VALID_HWACCEL_VALUES:
        raise ValueError(
            f"hardware_acceleration={hwaccel!r} is not one of "
            f"{list(VALID_HWACCEL_VALUES)}"
        )

    codec_normalized = (codec or "").strip().lower()
    priority = _priority_for_codec(codec_normalized, requested)
    factory_finder = gst_module.ElementFactory.find
    codec_settings = _resolve_encoder_settings(
        encoder_settings, codec_normalized
    )

    # Dedicated MJPEG software-pin path. The reason string differs from
    # the general fallback case so the operator can see at a glance
    # whether they explicitly opted out (`hardware_acceleration =
    # "none"`) or hit the runtime negotiation-failure retry.
    if codec_normalized == "mjpeg" and (force_software or requested == "none"):
        if force_software:
            reason = (
                f"{SOFTWARE_MJPEG_ELEMENT} (software fallback after "
                f"negotiation failure; hardware_acceleration={requested})"
            )
        else:
            reason = (
                f"{SOFTWARE_MJPEG_ELEMENT} "
                f"(hardware_acceleration=none — software requested)"
            )
        return EncoderSelection(
            element_name=SOFTWARE_MJPEG_ELEMENT,
            is_software_fallback=True,
            requested_hwaccel=requested,
            reason=reason,
            factory_args=_factory_args_for_element(
                SOFTWARE_MJPEG_ELEMENT, codec_normalized, codec_settings
            ),
        )

    for candidate in priority:
        if not factory_finder(candidate):
            continue
        is_software = _is_software_element(candidate)
        # `is_software_fallback` is only True when picking software
        # *after* a hwaccel candidate would have been preferred. For
        # codecs with no hwaccel option, software is canonical.
        is_fallback = (
            is_software
            and _codec_has_hwaccel_option(codec_normalized)
        )
        reason = _build_reason(
            codec=codec_normalized,
            element_name=candidate,
            requested_hwaccel=requested,
            priority=priority,
            is_software=is_software,
            is_fallback=is_fallback,
        )
        return EncoderSelection(
            element_name=candidate,
            is_software_fallback=is_fallback,
            requested_hwaccel=requested,
            reason=reason,
            factory_args=_factory_args_for_element(
                candidate, codec_normalized, codec_settings
            ),
        )

    # Nothing in the priority list is registered. Tailor the install
    # hint per codec — gst-libav for ProRes/DNxHR, gst-plugins-good for
    # MJPEG (which would be a misconfigured environment).
    if codec_normalized == "mjpeg":
        raise RuntimeError(
            f"no MJPEG encoder available; tried {list(priority)}. "
            f"Check that gst-plugins-good is installed."
        )
    raise RuntimeError(
        f"no {codec_normalized.upper()} encoder available; tried "
        f"{list(priority)}. {_LIBAV_INSTALL_HINT}"
    )


def validate_segment_duration(codec: str, duration_seconds: float) -> None:
    """Phase 11.C: refuse a segment duration shorter than the codec's GOP.

    Today's codecs (`mjpeg` / `prores` / `dnxhr`) are all intra-frame —
    GOP=1 frame, so any positive duration is valid. The validator
    exists as a seam: when 11.D-or-later adds a long-GOP codec
    (H.264-export pipeline, say), the shorter-than-GOP check slots in
    here without changing every caller.
    """
    codec_normalized = (codec or "").strip().lower()
    if duration_seconds <= 0.0:
        raise RuntimeError(
            f"recording_segment_duration_seconds={duration_seconds} "
            f"must be > 0."
        )
    if codec_normalized in {"mjpeg", "prores", "dnxhr"}:
        return  # intra-frame; no GOP constraint.
    raise NotImplementedError(
        f"validate_segment_duration: codec={codec!r} has no registered "
        f"GOP rule. Add a case here when wiring a new codec."
    )


def _build_reason(
    *,
    codec: str,
    element_name: str,
    requested_hwaccel: str,
    priority: tuple[str, ...],
    is_software: bool,
    is_fallback: bool,
) -> str:
    if not is_software:
        return (
            f"{element_name} ({_hwaccel_label(requested_hwaccel)} hwaccel; "
            f"hardware_acceleration={requested_hwaccel})"
        )
    if is_fallback and len(priority) > 1:
        return (
            f"{element_name} (software fallback; {priority[0]} not found "
            f"for hardware_acceleration={requested_hwaccel})"
        )
    if codec == "mjpeg":
        # nvidia / amd MJPEG: no hwaccel option exists, so the operator
        # asked for hwaccel that doesn't apply.
        return (
            f"{element_name} (no hwaccel MJPEG element for "
            f"hardware_acceleration={requested_hwaccel}; software only)"
        )
    if codec == "prores":
        return (
            f"{element_name} (software ProRes; no hwaccel ProRes "
            f"option exists on this platform)"
        )
    if codec == "dnxhr":
        return (
            f"{element_name} (software DNxHR; no hwaccel DNxHR "
            f"option exists on this platform)"
        )
    return f"{element_name} (software)"
