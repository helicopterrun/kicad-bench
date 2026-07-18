"""Cockpit service/API contracts. FastAPI remains an optional dependency."""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_bench.cockpit.service import CockpitService


def _config(tmp_path: Path) -> Path:
    (tmp_path / "alpha.kicad_sch").write_text("(kicad_sch (version 20231120))")
    (tmp_path / "beta.kicad_sch").write_text("(kicad_sch (version 20231120))")
    path = tmp_path / "kicad-bench.toml"
    path.write_text(
        '[project]\nroot = "."\n\n'
        '[product]\nname = "Bench fixture"\n\n'
        '[[boards]]\nname = "alpha"\nroot_sch = "alpha.kicad_sch"\n\n'
        '[[boards]]\nname = "beta"\nroot_sch = "beta.kicad_sch"\n'
    )
    return path


def test_service_isolates_multi_board_state(tmp_path):
    service = CockpitService(_config(tmp_path))
    assert service.product()["name"] == "Bench fixture"
    assert [row["id"] for row in service.boards()] == ["alpha", "beta"]
    assert service.state("alpha") is not service.state("beta")
    assert service.state("alpha").cfg.active_board == "alpha"
    assert service.state("beta").cfg.active_board == "beta"
    with pytest.raises(ValueError, match="unknown board"):
        service.state("missing")


def test_status_is_read_only_for_empty_stage(tmp_path):
    service = CockpitService(_config(tmp_path))
    status = service.status("alpha")
    assert status["board"] == "alpha"
    assert status["audit"]["status"] == "none"
    assert status["review"]["status"] == "none"
    assert status["stage"]["jobs"] == []


def test_fastapi_routes_and_stage_queue(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from kicad_bench.cockpit.app import create_app

    client = TestClient(create_app(_config(tmp_path)))
    product = client.get("/api/product").json()
    assert product["name"] == "Bench fixture"
    assert len(client.get("/api/boards").json()["items"]) == 2
    assert client.get("/api/boards/missing/status").status_code == 404
    assert client.get("/api/not-a-route").status_code == 404

    assert client.post(
        "/api/boards/alpha/stage/jobs",
        json={"label": "fixture job", "command": ["true"]},
    ).status_code == 403
    headers = {"X-Cockpit-Token": product["mutation_token"]}
    created = client.post(
        "/api/boards/alpha/stage/jobs",
        json={"label": "fixture job", "command": ["true"]},
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["jobs"][0]["label"] == "fixture job"
    assert client.delete("/api/boards/alpha/stage/jobs", headers=headers).json()["cleared"] == 1


def test_spa_and_static_assets_are_packaged(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from kicad_bench.cockpit.app import STATIC_DIR, create_app

    assert (STATIC_DIR / "index.html").is_file()
    assert list((STATIC_DIR / "assets").glob("*.js"))
    client = TestClient(create_app(_config(tmp_path)))
    response = client.get("/release")
    assert response.status_code == 200
    assert "KiCad Cockpit" in response.text
