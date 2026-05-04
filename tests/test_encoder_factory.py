"""Phase 11.A — encoder-factory selection matrix tests.

The factory probes `Gst.ElementFactory.find()` to decide which encoder
element gets wired into the recording branch. These tests stub that
probe so they don't need a real GStreamer init — same pattern used in
`tests/test_recording_settings.py` for splitmuxsink.
"""

from __future__ import annotations

import unittest

from app.media.encoder_factory import (
    SOFTWARE_MJPEG_ELEMENT,
    EncoderSelection,
    select_encoder,
)


class _StubElementFactory:
    def __init__(self, available: set[str]) -> None:
        self.available = set(available)

    def find(self, name: str) -> object | None:
        # Truthy-but-opaque return when the element is registered;
        # the factory only checks truthiness.
        return object() if name in self.available else None


class _StubGst:
    def __init__(self, available: set[str]) -> None:
        self.ElementFactory = _StubElementFactory(available)


def _select(
    *,
    hwaccel: str,
    available: set[str],
    codec: str = "mjpeg",
    force_software: bool = False,
) -> EncoderSelection:
    return select_encoder(
        hwaccel=hwaccel,
        codec=codec,
        gst_module=_StubGst(available),
        force_software=force_software,
    )


class MjpegSelectionMatrixTests(unittest.TestCase):
    """Selection table for codec="mjpeg" — the only codec wired in 11.A."""

    def test_auto_picks_qsvjpegenc_when_available(self) -> None:
        sel = _select(hwaccel="auto", available={"qsvjpegenc", "jpegenc"})
        self.assertEqual(sel.element_name, "qsvjpegenc")
        self.assertFalse(sel.is_software_fallback)
        self.assertEqual(sel.requested_hwaccel, "auto")
        self.assertIn("auto", sel.reason)

    def test_auto_falls_back_to_jpegenc_when_qsv_missing(self) -> None:
        sel = _select(hwaccel="auto", available={"jpegenc"})
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("qsvjpegenc not found", sel.reason)
        self.assertIn("auto", sel.reason)

    def test_intel_picks_qsvjpegenc_when_available(self) -> None:
        sel = _select(hwaccel="intel", available={"qsvjpegenc", "jpegenc"})
        self.assertEqual(sel.element_name, "qsvjpegenc")
        self.assertFalse(sel.is_software_fallback)

    def test_intel_falls_back_to_software_when_qsv_missing(self) -> None:
        sel = _select(hwaccel="intel", available={"jpegenc"})
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("intel", sel.reason)

    def test_nvidia_uses_software_no_nvenc_mjpeg_exists(self) -> None:
        # NVENC has no MJPEG element on Windows builds — software is
        # the only option, and the fallback is treated as "no hwaccel
        # for this combination" rather than a missing-element fallback.
        sel = _select(hwaccel="nvidia", available={"jpegenc"})
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("nvidia", sel.reason)

    def test_amd_uses_software_no_amf_mjpeg_exists(self) -> None:
        sel = _select(hwaccel="amd", available={"jpegenc"})
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("amd", sel.reason)

    def test_none_pins_to_jpegenc_with_dedicated_reason(self) -> None:
        # "none" is the operator's explicit opt-out; the reason should
        # reflect the user choice, not "fallback".
        sel = _select(
            hwaccel="none", available={"qsvjpegenc", "jpegenc"}
        )
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("software requested", sel.reason)


class ForceSoftwareFlagTests(unittest.TestCase):
    """`force_software=True` is the bus-error retry knob — overrides
    every hwaccel choice and produces a distinctive reason string so
    the diagnostics widget can show *why* the encoder is software."""

    def test_force_software_overrides_auto(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"qsvjpegenc", "jpegenc"},
            force_software=True,
        )
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertTrue(sel.is_software_fallback)
        self.assertIn("negotiation failure", sel.reason)

    def test_force_software_overrides_intel(self) -> None:
        sel = _select(
            hwaccel="intel",
            available={"qsvjpegenc", "jpegenc"},
            force_software=True,
        )
        self.assertEqual(sel.element_name, SOFTWARE_MJPEG_ELEMENT)
        self.assertIn("negotiation failure", sel.reason)


class ValidationTests(unittest.TestCase):
    def test_unknown_hwaccel_raises(self) -> None:
        with self.assertRaises(ValueError):
            _select(hwaccel="vulkan", available={"jpegenc"})

    def test_unknown_codec_raises_not_implemented(self) -> None:
        # h264 / h265 are explicitly off the supported list (§5.2);
        # we want a NotImplementedError so the operator gets a clear
        # signal that the codec was rejected by design, not by accident.
        with self.assertRaises(NotImplementedError):
            _select(hwaccel="auto", available={"jpegenc"}, codec="h264")

    def test_no_jpegenc_anywhere_is_a_runtime_error(self) -> None:
        # gst-plugins-good is required by everything else in the
        # pipeline; if it really isn't installed we want a clear
        # error rather than a None element_name leaking into
        # _make_element.
        with self.assertRaises(RuntimeError):
            _select(hwaccel="auto", available=set())


class ProresSelectionTests(unittest.TestCase):
    """Phase 11.B — ProRes selection. avenc_prores_ks first, avenc_prores
    as a same-codec backup, all software-only on Windows."""

    def test_picks_prores_ks_when_both_libav_encoders_available(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_prores_ks", "avenc_prores"},
            codec="prores",
        )
        self.assertEqual(sel.element_name, "avenc_prores_ks")
        # ProRes has no hwaccel option on Windows, so software is
        # the canonical pick — not a "fallback".
        self.assertFalse(sel.is_software_fallback)
        self.assertIn("ProRes", sel.reason)

    def test_falls_back_to_avenc_prores_when_ks_missing(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_prores"},
            codec="prores",
        )
        self.assertEqual(sel.element_name, "avenc_prores")
        self.assertFalse(sel.is_software_fallback)

    def test_no_libav_raises_with_install_hint(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _select(hwaccel="auto", available=set(), codec="prores")
        self.assertIn("gst-libav", str(ctx.exception).lower())

    def test_factory_args_include_prores_lt_profile(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_prores_ks"},
            codec="prores",
        )
        self.assertEqual(sel.factory_args.get("profile"), 2)

    def test_hwaccel_request_does_not_change_prores_selection(self) -> None:
        # Operator asks for Intel hwaccel + ProRes; there's no hwaccel
        # ProRes on Windows. We honor the codec choice and pick the
        # software encoder, with a reason that explains why.
        for hwaccel in ("intel", "nvidia", "amd"):
            with self.subTest(hwaccel=hwaccel):
                sel = _select(
                    hwaccel=hwaccel,
                    available={"avenc_prores_ks"},
                    codec="prores",
                )
                self.assertEqual(sel.element_name, "avenc_prores_ks")
                self.assertFalse(sel.is_software_fallback)

    def test_force_software_is_a_noop_for_prores(self) -> None:
        # ProRes encoders are already software; force_software=True
        # picks the same element with no special reason text.
        sel = _select(
            hwaccel="auto",
            available={"avenc_prores_ks"},
            codec="prores",
            force_software=True,
        )
        self.assertEqual(sel.element_name, "avenc_prores_ks")


class DnxhrSelectionTests(unittest.TestCase):
    """Phase 11.B — DNxHR selection. avenc_dnxhd is the only candidate."""

    def test_picks_avenc_dnxhd_when_available(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_dnxhd"},
            codec="dnxhr",
        )
        self.assertEqual(sel.element_name, "avenc_dnxhd")
        self.assertFalse(sel.is_software_fallback)
        self.assertIn("DNxHR", sel.reason)

    def test_no_libav_raises_with_install_hint(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _select(hwaccel="auto", available=set(), codec="dnxhr")
        self.assertIn("gst-libav", str(ctx.exception).lower())

    def test_factory_args_include_dnxhr_default_bitrate(self) -> None:
        # avenc_dnxhd needs an explicit bitrate; default targets
        # DNxHR LB at 1080p30.
        sel = _select(
            hwaccel="auto",
            available={"avenc_dnxhd"},
            codec="dnxhr",
        )
        self.assertEqual(sel.factory_args.get("bitrate"), 36000000)


class ReasonStringTests(unittest.TestCase):
    """The `reason` field is rendered in the diagnostics widget — make
    sure each path produces something humans can read."""

    def test_hwaccel_reason_names_the_vendor(self) -> None:
        sel = _select(hwaccel="intel", available={"qsvjpegenc", "jpegenc"})
        self.assertIn("Intel", sel.reason)

    def test_fallback_reason_names_the_missing_element(self) -> None:
        sel = _select(hwaccel="auto", available={"jpegenc"})
        self.assertIn("qsvjpegenc", sel.reason)

    def test_prores_reason_calls_out_no_hwaccel_option(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_prores_ks"},
            codec="prores",
        )
        self.assertIn("no hwaccel", sel.reason)

    def test_dnxhr_reason_calls_out_no_hwaccel_option(self) -> None:
        sel = _select(
            hwaccel="auto",
            available={"avenc_dnxhd"},
            codec="dnxhr",
        )
        self.assertIn("no hwaccel", sel.reason)


if __name__ == "__main__":
    unittest.main()
