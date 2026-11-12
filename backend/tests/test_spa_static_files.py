"""Tests for SPAStaticFiles' 404 fallback behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.main import SPAStaticFiles


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    """A miniature `frontend/dist` with index.html + an asset file."""
    (tmp_path / "index.html").write_text("<!doctype html><body>spa shell</body>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "real.js").write_text("// real file")
    return tmp_path


async def _get(static: SPAStaticFiles, path: str):
    """Call get_response with a minimal Starlette scope."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/" + path,
        "headers": [],
    }
    return await static.get_response(path, scope)


@pytest.mark.asyncio
async def test_existing_file_served(static_dir: Path) -> None:
    """A real asset path resolves normally."""
    static = SPAStaticFiles(directory=str(static_dir), html=True)
    resp = await _get(static, "assets/real.js")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_index_served_for_root(static_dir: Path) -> None:
    """The bare mount path serves index.html."""
    static = SPAStaticFiles(directory=str(static_dir), html=True)
    resp = await _get(static, "")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unknown_spa_route_falls_back_to_index(static_dir: Path) -> None:
    """A client-side route (`/submit`, `/orders/WO-0001`) returns the SPA shell."""
    static = SPAStaticFiles(directory=str(static_dir), html=True)
    resp = await _get(static, "submit")
    assert resp.status_code == 200
    resp = await _get(static, "orders/WO-0001")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_asset_does_not_fall_back(static_dir: Path) -> None:
    """A request that looks like an asset (last segment has a dot) keeps
    its honest 404 — papering over with index.html would hide build /
    deploy regressions."""
    static = SPAStaticFiles(directory=str(static_dir), html=True)
    with pytest.raises(StarletteHTTPException) as exc:
        await _get(static, "assets/missing.js")
    assert exc.value.status_code == 404
    with pytest.raises(StarletteHTTPException) as exc:
        await _get(static, "logo-not-here.png")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_api_paths_do_not_fall_back(static_dir: Path) -> None:
    """An /api/* path that reaches the static mount (because no API
    route matched) must keep its 404 — falling back to index.html would
    mean a typo'd endpoint serves HTML, breaking client fetch() calls."""
    static = SPAStaticFiles(directory=str(static_dir), html=True)
    with pytest.raises(StarletteHTTPException) as exc:
        await _get(static, "api/does-not-exist")
    assert exc.value.status_code == 404
