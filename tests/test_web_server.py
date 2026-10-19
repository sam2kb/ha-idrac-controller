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
@pytest.mark.parametrize("slug", ["a85f1537_ha_idrac_controller", "local_ha_idrac_controller", "otherrepo_ha_idrac_controller"])
def test_saved_message_links_to_installed_app_info_outside_ingress(supervisor_self, prefix, slug):
    client, config_path, factory, connection, response = supervisor_self
    response.read.return_value = json.dumps({"result": "ok", "data": {"slug": slug}}).encode()
    client.post("/servers/add", data={"alias": "link-fixture"},
                environ_overrides={"SCRIPT_NAME": prefix})
    page = client.get("/servers", environ_overrides={"SCRIPT_NAME": prefix})
    links = restart_links(page)
    assert links == [{"href": f"/config/app/{slug}/info", "target": "_top"}]
    assert urljoin("https://ha.bit-b.com" + prefix + "/servers", links[0]["href"]) == (
        f"https://ha.bit-b.com/config/app/{slug}/info")
    factory.assert_called_once_with("supervisor", timeout=3)
    connection.request.assert_called_once_with(
        "GET", "/addons/self/info", headers={"Authorization": "Bearer fixture-supervisor-token"})
    connection.close.assert_called_once()
    assert "fixture-supervisor-token" not in page.get_data(as_text=True)
    assert "fixture-supervisor-token" not in config_path.read_text()


def test_missing_supervisor_token_links_to_current_apps_page(supervisor_self, monkeypatch):
    client, config_path, factory, connection, response = supervisor_self
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    client.post("/servers/add", data={"alias": "link-fixture"})
    assert restart_links(client.get("/servers")) == [{"href": "/config/apps", "target": "_top"}]
    factory.assert_not_called()


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b'{"result":"error"}', b'{"result":"ok","data":[]}',
    b'{"result":"ok","data":{}}',
    json.dumps({"result": "ok", "data": {"slug": "../other"}}).encode(),
    json.dumps({"result": "ok", "data": {"slug": "bad<script>alert(1)</script>"}}).encode(),
    json.dumps({"result": "ok", "data": {"slug": None}}).encode(),
])
def test_bad_self_metadata_falls_back_without_breaking_save(supervisor_self, body):
    client, config_path, factory, connection, response = supervisor_self
    response.read.return_value = body
    client.post("/servers/add", data={"alias": "link-fixture"})
    assert any(server["alias"] == "link-fixture" for server in json.loads(config_path.read_text()))
    assert restart_links(client.get("/servers")) == [{"href": "/config/apps", "target": "_top"}]
    connection.close.assert_called_once()


@pytest.mark.parametrize("status", [302, 401, 404, 503])
def test_unavailable_self_metadata_falls_back(supervisor_self, status):
    client, config_path, factory, connection, response = supervisor_self
    response.status = status
    client.post("/servers/add", data={"alias": "link-fixture"})
    assert restart_links(client.get("/servers")) == [{"href": "/config/apps", "target": "_top"}]
    response.read.assert_not_called()
    connection.request.assert_called_once()


def test_metadata_timeout_does_not_break_configuration_save(supervisor_self):
    client, config_path, factory, connection, response = supervisor_self
    connection.request.side_effect = TimeoutError("fixture-secret-detail")
    client.post("/servers/add", data={"alias": "link-fixture"})
    page = client.get("/servers")
    assert restart_links(page) == [{"href": "/config/apps", "target": "_top"}]
    assert "fixture-secret-detail" not in page.get_data(as_text=True)


def test_failed_save_does_not_offer_restart_link_or_query_metadata(supervisor_self, monkeypatch):
    client, config_path, factory, connection, response = supervisor_self
    monkeypatch.setattr(web_server, "SERVERS_CONFIG_FILE", str(config_path.parent))
    client.post("/servers/add", data={"alias": "link-fixture"})
    page = client.get("/servers")
    assert b"Could not write" in page.data
    assert restart_links(page) == []
    factory.assert_not_called()


@pytest.mark.parametrize("path", ["/servers/add", "/servers/update/fixture"])
@pytest.mark.parametrize("enabled_action", [None, "allow_power_on", "allow_shutdown", "allow_reboot"])
def test_power_permissions_are_independent_opt_ins_and_can_be_disabled(web, path, enabled_action):
    client, config_path = web
    # Existing enabled flags must be removed when the new form opts out.
    servers = json.loads(config_path.read_text())
    servers[0].update(allow_power_on=True, allow_shutdown=True, allow_reboot=True)
    config_path.write_text(json.dumps(servers))
    form = {"alias": "new", "fan_mode": "auto"}
    if enabled_action:
        form[enabled_action] = "true"
    assert client.post(path, data=form).status_code == 302
    saved = json.loads(config_path.read_text())[-1 if path.endswith("add") else 0]
    for option in ("allow_power_on", "allow_shutdown", "allow_reboot"):
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
    assert parser.selected == {"allow_power_on": "false", "allow_shutdown": "false", "allow_reboot": "false"}
    assert "without an OS shutdown" in response.get_data(as_text=True)
    assert config_path.read_bytes() == before



def test_dashboard_labels_cpu_sockets_and_shows_reported_battery_health(web, monkeypatch):
    client, config_path = web
    monkeypatch.setattr(web_server, "load_all_servers_status", lambda: [{
        "alias": "fixture", "ip": "127.0.0.1", "last_updated": "fixture",
        "hottest_cpu_temp_c": 35, "inlet_temp_c": 24, "exhaust_temp_c": None,
        "power_consumption_watts": 70, "target_fan_speed_percent": "Dell Auto",
        "cpu_temps_c": [35, 32], "cpu_ids": [1, 2], "actual_fan_rpms": [],
        "batteries": [{"name": "CMOS Battery", "health": "Healthy", "voltage": None}],
    }])
    page = client.get("/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "CPU1: 35" in text and "CPU2: 32" in text
    assert "CMOS Battery" in text and "Healthy" in text
    assert "None V" not in text
