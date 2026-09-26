"""Brand images served by Home Assistant from ``custom_components/rehom/brand/``."""

from __future__ import annotations

from pathlib import Path
import struct

from homeassistant.components.brands.const import ALLOWED_IMAGES
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
import pytest

from custom_components.rehom.const import DOMAIN

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / "rehom" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RGBA = 6  # PNG colour type: truecolour with alpha (transparent background)


def _png_header(path: Path) -> tuple[int, int, int]:
    """(width, height, colour type) from the IHDR chunk."""
    data = path.read_bytes()[:33]
    assert data[:8] == PNG_SIGNATURE, path.name
    assert data[12:16] == b"IHDR", path.name
    width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


def test_only_brand_images() -> None:
    """Every file in brand/ is one Home Assistant serves, and all of them exist."""
    assert {path.name for path in BRAND.iterdir()} == set(ALLOWED_IMAGES)


@pytest.mark.parametrize("name", sorted(ALLOWED_IMAGES))
def test_brand_image_size(name: str) -> None:
    """Icons are square 256/512 px; logos have a shortest side of 128-256 (@2x: 256-512)."""
    width, height, colour = _png_header(BRAND / name)
    assert colour == RGBA
    hidpi = "@2x" in name
    if "icon" in name:
        assert (width, height) == ((512, 512) if hidpi else (256, 256))
    else:
        low, high = (256, 512) if hidpi else (128, 256)
        assert low <= min(width, height) <= high
        assert width >= height  # landscape


async def test_loader_sees_brand(hass: HomeAssistant) -> None:
    """The loader flags the integration as shipping its own brand images."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.has_branding
