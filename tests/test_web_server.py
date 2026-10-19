"""Real Flask routes with temporary configuration and no external I/O."""
import json
from urllib.parse import urljoin, urlsplit

import pytest

from app import web_server


@pytest.fixture
def web(monkeypatch, tmp_path):
    config_path = tmp_path / "servers.json"
    server = {
        "alias": "fixture", "idrac_ip": "127.0.0.1",
        "idrac_username": "fixture", "idrac_password": "fixture",
        "enabled": True, "fan_control_enabled": True, "fan_mode": "curve",
        "fan_curve": [{"temp": 40, "speed": 20}, {"temp": 60, "speed": 60}],
    }
    config_path.write_text(json.dumps([server]))
    monkeypatch.setattr(web_server, "SERVERS_CONFIG_FILE", str(config_path))
    monkeypatch.setattr(web_server, "global_config", {
        "base_fan_speed_percent": 20, "low_temp_threshold": 45,
        "high_temp_fan_speed_percent": 50, "critical_temp_threshold": 65,
    })
    monkeypatch.setitem(web_server.app.config, "TESTING", True)
    return web_server.app.test_client(), config_path


@pytest.mark.parametrize("prefix", ["", "/api/hassio_ingress/fixture"])
@pytest.mark.parametrize("method,path,data", [
    ("post", "/servers/add", {"alias": "new"}),
    ("post", "/servers/add", {"alias": ""}),
    ("post", "/servers/add", {"alias": "fixture"}),
    ("post", "/servers/delete/fixture", {}),
    ("post", "/servers/delete/missing", {}),
    ("get", "/servers/edit/missing", {}),
    ("post", "/servers/update/missing", {}),
])
def test_redirects_reach_server_list_inside_ingress(web, prefix, method, path, data):
    client, config_path = web
    response = client.open(path, method=method.upper(), data=data,
                           environ_overrides={"SCRIPT_NAME": prefix})
    assert response.status_code == 302
    target = urlsplit(urljoin(f"http://localhost{prefix}{path}", response.headers["Location"]))
    assert target.path == f"{prefix}/servers"
    assert client.get(target.path[len(prefix):],
                      environ_overrides={"SCRIPT_NAME": prefix}).status_code == 200


@pytest.mark.parametrize("indexes", [(1, 2), (0, 2), (0, 3, 5)])
def test_deleted_curve_rows_preserve_all_remaining_points(web, indexes):
    client, config_path = web
    form = {"fan_mode": "curve", "enabled": "true", "fan_control_enabled": "true"}
    expected = []
    for number, index in enumerate(indexes):
        temp, speed = 40 + 10 * number, 20 + 10 * number
        form[f"curve_temp_{index}"] = str(temp)
        form[f"curve_speed_{index}"] = str(speed)
        expected.append({"temp": temp, "speed": speed})
    response = client.post("/servers/update/fixture", data=form)
    assert response.status_code == 302
    assert json.loads(config_path.read_text())[0]["fan_curve"] == expected


@pytest.mark.parametrize("curve_fields", [
    {},
    {"curve_temp_0": "40", "curve_speed_0": "20"},
    {"curve_temp_0": "bad", "curve_speed_0": "20", "curve_temp_1": "60", "curve_speed_1": "50"},
    {"curve_temp_0": "40", "curve_temp_1": "60", "curve_speed_1": "50"},
    {"curve_temp_0": "40", "curve_speed_0": "20", "curve_temp_1": "40", "curve_speed_1": "50"},
    {"curve_temp_0": "40", "curve_speed_0": "0", "curve_temp_1": "60", "curve_speed_1": "50"},
])
def test_invalid_curve_is_rejected_without_overwriting_configuration(web, curve_fields):
    client, config_path = web
    before = config_path.read_bytes()
    response = client.post("/servers/update/fixture", data={"fan_mode": "curve", **curve_fields})
    assert response.status_code == 302
    assert config_path.read_bytes() == before
    with client.session_transaction() as session:
        assert any(category == "error" for category, _ in session["_flashes"])


def test_switching_modes_keeps_saved_curve(web):
    client, config_path = web
    before = json.loads(config_path.read_text())[0]["fan_curve"]
    client.post("/servers/update/fixture", data={"fan_mode": "simple"})
    assert json.loads(config_path.read_text())[0]["fan_curve"] == before
