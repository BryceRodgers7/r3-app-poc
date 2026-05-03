"""Phase 11.A — recording-branch encoder selection.

The pipeline used to hardcode `jpegenc` on the splitmuxsink branch. As of
Phase 11.A the choice is driven by `[media] hardware_acceleration` in
`app_settings.toml`: the factory probes `Gst.ElementFactory.find()` for
the platform-appropriate hwaccel encoder element, falls back to software
when the requested element is unavailable, and surfaces the choice
(plus reason) for logging and diagnostics.

Only `codec="mjpeg"` rows are wired today; ProRes / DNxHR rows are
queued for Phase 11.B and slot in alongside the existing entries
without changing the public API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_HWACCEL_VALUES = ("auto", "none", "nvidia", "intel", "amd")
SOFTWARE_MJPEG_ELEMENT = "jpegenc"


@dataclass(frozen=True, slots=True)
class EncoderSelection:
    """The element name (and optional factory args) chosen for the record branch.

    `reason` is a short human-readable string used verbatim in the
    startup INFO log and the diagnostics widget — the operator can see
    at a glance which encoder ran and why."""

    element_name: str
    is_software_fallback: bool
    requested_hwaccel: str
    reason: str
    factory_args: dict[str, Any] = field(default_factory=dict)


def _mjpeg_priority(hwaccel: str) -> tuple[str, ...]:
    """Hwaccel-encoder probe order for MJPEG. Software always last."""
    if hwaccel in ("auto", "intel"):
        return ("qsvjpegenc", SOFTWARE_MJPEG_ELEMENT)
    # NVENC has no MJPEG element on Windows builds; AMD likewise.
    # Returning just software keeps the selection deterministic and
    # surfaces a `hwaccel_fallback` event for the unrequested-but-not-
    # available case.
    return (SOFTWARE_MJPEG_ELEMENT,)


def select_encoder(
    *,
    hwaccel: str,
    codec: str,
    gst_module: Any,
    force_software: bool = False,
) -> EncoderSelection:
    """Pick a recording-branch encoder element for `(hwaccel, codec)`.

    `gst_module` must expose `ElementFactory.find(name)` returning a
    truthy factory when the element is registered. Tests pass a stub.
    `force_software=True` short-circuits the probe and returns the
    software fallback — used by the bus-error retry path when an
    hwaccel element fails caps negotiation at PLAYING.
    """
    requested = (hwaccel or "auto").strip().lower()
    if requested not in VALID_HWACCEL_VALUES:
        raise ValueError(
            f"hardware_acceleration={hwaccel!r} is not one of "
            f"{list(VALID_HWACCEL_VALUES)}"
        )

    codec_normalized = (codec or "").strip().lower()
    if codec_normalized != "mjpeg":
        # Phase 11.A only wires MJPEG. ProRes / DNxHR rows arrive with
        # 11.B; until then any other codec value is a config error
        # caught upstream — but be explicit if we're called anyway.
        raise NotImplementedError(
            f"encoder_factory does not yet wire codec={codec!r}; "
            "Phase 11.B introduces ProRes / DNxHR selection."
        )

    if force_software or requested == "none":
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
        )

    priority = _mjpeg_priority(requested)
    factory_finder = gst_module.ElementFactory.find
    for candidate in priority:
        if factory_finder(candidate):
            is_software = candidate == SOFTWARE_MJPEG_ELEMENT
            if is_software and len(priority) > 1:
                # A hwaccel was requested but the requested element
                # wasn't found — name the missing one in the reason so
                # the operator can tell at a glance.
                missing = priority[0]
                reason = (
                    f"{candidate} (software fallback; {missing} not found "
                    f"for hardware_acceleration={requested})"
                )
            elif is_software:
                # No hwaccel option exists for this combination on this
                # platform (e.g. nvidia / amd MJPEG).
                reason = (
                    f"{candidate} (no hwaccel MJPEG element for "
                    f"hardware_acceleration={requested}; software only)"
                )
            else:
                hwaccel_label = {
                    "auto": "auto-selected",
                    "intel": "Intel",
                    "nvidia": "NVIDIA",
                    "amd": "AMD",
                }.get(requested, requested)
                reason = (
                    f"{candidate} ({hwaccel_label} hwaccel; "
                    f"hardware_acceleration={requested})"
                )
            return EncoderSelection(
                element_name=candidate,
                is_software_fallback=is_software,
                requested_hwaccel=requested,
                reason=reason,
            )

    # Shouldn't be reachable — `jpegenc` ships in `gst-plugins-good`,
    # which the rest of the pipeline already requires. If it really
    # isn't here we've got bigger problems than encoder selection.
    raise RuntimeError(
        f"no MJPEG encoder available; tried {list(priority)}. "
        f"Check that gst-plugins-good is installed."
    )
