from __future__ import annotations


def test_network_runtime_exposes_chatgpt_proxy_and_clearance_settings(client):
    response = client.get("/api/network/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["egress_path"] == "direct"
    assert payload["clearance_mode"] == "none"
    assert payload["has_clearance_cookie"] is False


def test_network_runtime_routes_are_registered(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/network/runtime" in paths
    assert "/api/network/runtime/test" in paths
    assert "/api/network/runtime/ensure-fs" in paths
