"""Real Flask routes with temporary configuration and no external I/O."""
import json
from urllib.parse import urljoin, urlsplit

import pytest

from app import web_server


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
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
    monkeypatch.setattr(web_server, "restart_state", "idle")
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


def test_new_server_defaults_to_native_auto_without_custom_opt_in(web):
    client, config_path = web
    client.post("/servers/add", data={"alias": "new", "idrac_ip": "127.0.0.2"})
    added = next(server for server in json.loads(config_path.read_text()) if server["alias"] == "new")
    assert added["fan_mode"] == "auto"
    assert added["fan_control_enabled"] is True


def test_new_server_can_explicitly_select_custom_control(web):
    client, config_path = web
    client.post("/servers/add", data={"alias": "custom", "fan_mode": "simple"})
    added = next(server for server in json.loads(config_path.read_text()) if server["alias"] == "custom")
    assert added["fan_mode"] == "simple"


def test_saved_custom_curve_can_switch_to_auto_without_deleting_curve(web):
    client, config_path = web
    before = json.loads(config_path.read_text())[0]["fan_curve"]
    client.post("/servers/update/fixture", data={
        "fan_mode": "auto", "fan_control_enabled": "true", "enabled": "true",
    })
    saved = json.loads(config_path.read_text())[0]
    assert saved["fan_mode"] == "auto"
    assert saved["fan_curve"] == before


@pytest.mark.parametrize("path", ["/servers/add", "/servers/update/fixture"])
def test_invalid_cooling_selection_is_rejected_without_saving(web, path):
    client, config_path = web
    before = config_path.read_bytes()
    client.post(path, data={"alias": "new", "fan_mode": "invalid"})
    assert config_path.read_bytes() == before


def selected_cooling_mode(response):
    from html.parser import HTMLParser

    class SelectionParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inside_mode = False
            self.selected = None

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "select":
                self.inside_mode = values.get("name") == "fan_mode"
            elif tag == "option" and self.inside_mode and "selected" in values:
                self.selected = values.get("value")

        def handle_endtag(self, tag):
            if tag == "select":
                self.inside_mode = False

    parser = SelectionParser()
    parser.feed(response.get_data(as_text=True))
    return parser.selected


def test_new_server_form_selects_dell_auto_and_edit_keeps_saved_custom_mode(web):
    client, config_path = web
    assert selected_cooling_mode(client.get("/servers")) == "auto"
    assert selected_cooling_mode(client.get("/servers/edit/fixture")) == "curve"


@pytest.fixture
def supervisor_self(web, monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fixture-supervisor-token")
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = json.dumps({
        "result": "ok", "data": {"slug": "a85f1537_ha_idrac_controller"},
    }).encode()
    factory = Mock(return_value=connection)
    monkeypatch.setattr(web_server, "HTTPConnection", factory)
    return *web, factory, connection, response


def restart_links(response):
    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == "a" and values.get("href", "").startswith("/config/"):
                self.links.append(values)

    parser = LinkParser()
    parser.feed(response.get_data(as_text=True))
    return parser.links


@pytest.mark.parametrize("prefix", ["", "/api/hassio_ingress/fixture"])
def test_regular_save_has_no_restart_link_or_supervisor_request(supervisor_self, prefix):
    client, config_path, factory, connection, response = supervisor_self
    client.post("/servers/add", data={"alias": "save-fixture"}, environ_overrides={"SCRIPT_NAME": prefix})
    page = client.get("/servers", environ_overrides={"SCRIPT_NAME": prefix})
    assert b"Configuration saved. Reload or restart the app to apply changes." in page.data
    assert restart_links(page) == []
    factory.assert_not_called()
    assert "fixture-supervisor-token" not in page.get_data(as_text=True)
    assert "fixture-supervisor-token" not in config_path.read_text()


@pytest.mark.parametrize("prefix", ["", "/api/hassio_ingress/phone"])
@pytest.mark.parametrize("path", ["/servers/add", "/servers/update/fixture"])
def test_save_reload_stays_in_ingress_and_starts_only_after_page_loaded(supervisor_self, monkeypatch, prefix, path):
    from unittest.mock import Mock
    client, config_path, factory, connection, response = supervisor_self
    timer = Mock()
    monkeypatch.setattr(web_server.threading, "Timer", timer)
    result = client.post(path, data={"alias": "new", "fan_mode": "auto", "action": "save_reload"},
                         environ_overrides={"SCRIPT_NAME": prefix})
    destination = urljoin(f"https://ha.example{prefix}{path}", result.headers["Location"])
    assert destination == f"https://ha.example{prefix}/app/reload"
    page = client.get("/app/reload", environ_overrides={"SCRIPT_NAME": prefix})
    assert page.status_code == 200
    assert b"Configuration saved" in page.data
    assert b"fixture-supervisor-token" not in page.data
    timer.assert_not_called()
    with client.session_transaction() as state:
        token = state["reload_token"]
    result = client.post("/app/reload", headers={"X-Reload-Token": token})
    assert result.status_code == 202
    timer.assert_called_once_with(0.5, web_server._restart_app)
    assert timer.return_value.daemon is True
    timer.return_value.start.assert_called_once()
    factory.assert_not_called()
    assert client.post("/app/reload", headers={"X-Reload-Token": token}).status_code == 403


def test_restart_uses_only_supervisor_self_endpoint(supervisor_self):
    client, config_path, factory, connection, response = supervisor_self
    web_server._restart_app()
    factory.assert_called_once_with("supervisor", timeout=65)
    connection.request.assert_called_once_with("POST", "/addons/self/restart",
                                             headers={"Authorization": "Bearer fixture-supervisor-token"})
    connection.close.assert_called_once()


@pytest.mark.parametrize("failure", ["denied", "bad-json", "error-result", "timeout"])
def test_restart_failure_is_visible_without_leaking_details(supervisor_self, failure, caplog):
    client, config_path, factory, connection, response = supervisor_self
    if failure == "denied":
        response.status = 403
    elif failure == "bad-json":
        response.read.return_value = b"fixture-supervisor-token"
    elif failure == "error-result":
        response.read.return_value = b'{"result":"error"}'
    else:
        connection.request.side_effect = TimeoutError("fixture-supervisor-token")
    web_server._restart_app()
    status = client.get("/app/status")
    assert status.json["state"] == "failed"
    assert status.headers["Cache-Control"] == "no-store"
    assert b"fixture-supervisor-token" not in status.data
    assert "fixture-supervisor-token" not in caplog.text
    connection.close.assert_called_once()


@pytest.mark.parametrize("supplied", [None, "incorrect", "é"])
def test_restart_rejects_missing_wrong_and_non_ascii_tokens(supervisor_self, supplied):
    client, config_path, factory, connection, response = supervisor_self
    with client.session_transaction() as state:
        state["reload_token"] = "expected"
    headers = {"X-Reload-Token": supplied} if supplied else {}
    assert client.post("/app/reload", headers=headers).status_code == 403
    factory.assert_not_called()


def test_restart_unavailable_without_supervisor_does_not_restart(web, monkeypatch):
    from unittest.mock import Mock
    client, config_path = web
    timer = Mock()
    monkeypatch.setattr(web_server.threading, "Timer", timer)
    with client.session_transaction() as state:
        state["reload_token"] = "expected"
    assert client.post("/app/reload", headers={"X-Reload-Token": "expected"}).status_code == 503
    timer.assert_not_called()
    assert b'disabled title="Available when running as a Home Assistant app"' in client.get("/servers").data


def test_duplicate_reload_request_does_not_schedule_second_restart(supervisor_self, monkeypatch):
    from unittest.mock import Mock
    client, config_path, factory, connection, response = supervisor_self
    monkeypatch.setattr(web_server, "restart_state", "restarting")
    timer = Mock()
    monkeypatch.setattr(web_server.threading, "Timer", timer)
    with client.session_transaction() as state:
        state["reload_token"] = "expected"
    assert client.post("/app/reload", headers={"X-Reload-Token": "expected"}).status_code == 409
    timer.assert_not_called()


def test_failed_atomic_save_keeps_old_config_and_never_restarts(supervisor_self, monkeypatch):
    client, config_path, factory, connection, response = supervisor_self
    before = config_path.read_bytes()
    def fail(*args):
        raise OSError("fixture-secret-detail")
    monkeypatch.setattr(web_server.os, "replace", fail)
    result = client.post("/servers/add", data={"alias": "new", "action": "save_reload"})
    assert result.headers["Location"] == "../servers"
    page = client.get("/servers")
    assert b"Could not write" in page.data
    assert config_path.read_bytes() == before
    assert not list(config_path.parent.glob(".servers-*"))
    assert b"fixture-secret-detail" not in page.data
    assert restart_links(page) == []
    factory.assert_not_called()


@pytest.mark.parametrize("path", ["/servers/add", "/servers/update/fixture"])
@pytest.mark.parametrize("enabled_action", [None, "allow_power_on", "allow_shutdown", "allow_reboot", "allow_power_off", "allow_power_cycle"])
def test_power_permissions_are_independent_opt_ins_and_can_be_disabled(web, path, enabled_action):
    client, config_path = web
    # Existing enabled flags must be removed when the new form opts out.
    servers = json.loads(config_path.read_text())
    servers[0].update(allow_power_on=True, allow_shutdown=True, allow_reboot=True, allow_power_off=True, allow_power_cycle=True)
    config_path.write_text(json.dumps(servers))
    form = {"alias": "new", "fan_mode": "auto"}
    if enabled_action:
        form[enabled_action] = "true"
    assert client.post(path, data=form).status_code == 302
    saved = json.loads(config_path.read_text())[-1 if path.endswith("add") else 0]
    for option in ("allow_power_on", "allow_shutdown", "allow_reboot", "allow_power_off", "allow_power_cycle"):
        assert saved[option] is (option == enabled_action)


@pytest.mark.parametrize("path", ["/servers", "/servers/edit/fixture"])
def test_legacy_and_new_server_forms_default_power_actions_to_disabled(web, path):
    from html.parser import HTMLParser

    class PowerParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.name = None
            self.selected = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.name = attrs.get("name")
            if tag == "option" and self.name and self.name.startswith("allow_") and "selected" in attrs:
                self.selected[self.name] = attrs["value"]

        def handle_endtag(self, tag):
            if tag == "select":
                self.name = None

    client, config_path = web
    before = config_path.read_bytes()
    response = client.get(path)
    assert response.status_code == 200
    parser = PowerParser()
    parser.feed(response.get_data(as_text=True))
    assert parser.selected == {"allow_power_on": "false", "allow_shutdown": "false", "allow_reboot": "false", "allow_power_off": "false", "allow_power_cycle": "false"}
    assert "without an OS shutdown" in response.get_data(as_text=True)
    assert config_path.read_bytes() == before



def test_dashboard_labels_cpu_sockets_and_shows_reported_battery_health(web, monkeypatch):
    client, config_path = web
    monkeypatch.setattr(web_server, "load_all_servers_status", lambda: [{
        "alias": "fixture", "ip": "127.0.0.1", "last_updated": "fixture",
        "hottest_cpu_temp_c": 35, "inlet_temp_c": 24, "exhaust_temp_c": None,
        "power_consumption_watts": 70, "target_fan_speed_percent": "Dell Auto",
        "include_advanced_diagnostics": True, "power_capacity_percent": 7.78, "power_capacity_watts": 900,
        "power_warning_watts": 896, "power_failure_watts": 980,
        "intrusion_seen": True, "intrusion_active": False,
        "cpu_temps_c": [35, 32], "cpu_ids": [1, 2], "actual_fan_rpms": [],
        "batteries": [{"name": "CMOS Battery", "health": "Healthy", "voltage": None}],
    }])
    page = client.get("/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "CPU1: 35" in text and "CPU2: 32" in text
    assert "CMOS Battery" in text and "Healthy" in text
    assert "None V" not in text
    assert "7.78% Capacity" in text
    assert "System Board Intrusion" in text and "Clear" in text
    assert "Warning Threshold" in text and "896" in text


def test_dashboard_shows_available_additional_readings_and_escapes_labels(web, monkeypatch):
    client, config_path = web
    monkeypatch.setattr(web_server, "load_all_servers_status", lambda: [{
        "alias": "fixture", "ip": "127.0.0.1", "last_updated": "fixture",
        "hottest_cpu_temp_c": None, "inlet_temp_c": None, "exhaust_temp_c": None,
        "power_consumption_watts": None, "cpu_temps_c": [], "actual_fan_rpms": [],
        "sensor_readings": [
            {"name": "PS1 Voltage <test>", "value": 0, "unit": "V", "status": "ok"},
            {"name": "PS1 Status", "value": "AC lost", "unit": None, "status": "ok"},
            {"name": "Hidden absent reading", "value": None, "unit": "V", "status": "ns"},
        ],
    }])
    page = client.get("/")
    assert page.status_code == 200
    assert b"PS1 Voltage &lt;test&gt;" in page.data and b"0 V" in page.data
    assert b"AC lost" in page.data
    assert b"Hidden absent reading" not in page.data


@pytest.mark.parametrize("path", ["/servers", "/servers/edit/fixture"])
def test_advanced_diagnostics_is_off_by_default_without_modifying_saved_legacy_config(web, path):
    client, config_path = web
    before = config_path.read_bytes()
    page = client.get(path)
    text = page.get_data(as_text=True)
    assert '<option value="false" selected>Off — important readings only' in text
    assert "Include advanced diagnostics" in text
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("path", ["/servers/add", "/servers/update/fixture"])
@pytest.mark.parametrize("setting,expected", [(None, False), ("false", False), ("true", True), ("1", False)])
def test_advanced_diagnostics_option_saves_independently_of_power_controls(web, path, setting, expected):
    client, config_path = web
    form = {"alias": "new", "fan_mode": "auto", "fan_control_enabled": "false"}
    if setting is not None:
        form["include_advanced_diagnostics"] = setting
    assert client.post(path, data=form).status_code == 302
    server = json.loads(config_path.read_text())[-1 if path.endswith("add") else 0]
    assert server["include_advanced_diagnostics"] is expected
    assert server["fan_control_enabled"] is False
    assert all(server[key] is False for key in ("allow_power_on", "allow_shutdown", "allow_reboot", "allow_power_off", "allow_power_cycle"))
