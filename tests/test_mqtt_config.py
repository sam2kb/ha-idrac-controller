"""Supervisor MQTT discovery tests using fixture data and no external I/O."""
from http.client import HTTPException
import json
from unittest.mock import Mock

import pytest

from app import mqtt_config


SERVICE = {"host": "core-mosquitto", "port": 1883,
           "username": "fixture-service-user", "password": "fixture-service-password",
           "ssl": False, "protocol": "3.1.1"}


@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fixture-supervisor-token")
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = json.dumps({"result": "ok", "data": SERVICE}).encode()
    factory = Mock(return_value=connection)
    monkeypatch.setattr(mqtt_config, "HTTPConnection", factory)
    return factory, connection, response


def test_auto_uses_supervisor_service_credentials_without_logging_them(supervisor):
    factory, connection, response = supervisor
    log = Mock()
    result = mqtt_config.resolve_mqtt_config({}, log)
    assert result == {key: SERVICE[key] for key in ("host", "port", "username", "password", "ssl")}
    factory.assert_called_once_with("supervisor", timeout=3)
    connection.request.assert_called_once_with(
        "GET", "/services/mqtt", headers={"Authorization": "Bearer fixture-supervisor-token"})
    connection.close.assert_called_once()
    assert "fixture-service-password" not in str(log.call_args_list)
    assert "fixture-supervisor-token" not in str(log.call_args_list)


def test_old_credential_free_defaults_also_use_supervisor(supervisor):
    factory, connection, response = supervisor
    result = mqtt_config.resolve_mqtt_config(
        {"mqtt_host": "  core-mosquitto  ", "mqtt_port": 1883,
         "mqtt_username": "", "mqtt_password": ""}, Mock())
    assert result["username"] == SERVICE["username"]
    connection.request.assert_called_once()


@pytest.mark.parametrize("host,port,user,password", [
    ("external.example", 1883, "fixture-manual-user", "fixture-manual-password"),
    ("external.example", 1883, "", ""),
    ("core-mosquitto", 1883, "fixture-manual-user", "fixture-manual-password"),
    ("core-mosquitto", 1884, "", ""),
])
def test_explicit_manual_settings_override_discovery(supervisor, host, port, user, password):
    factory, connection, response = supervisor
    result = mqtt_config.resolve_mqtt_config({
        "mqtt_host": host, "mqtt_port": port, "mqtt_username": user, "mqtt_password": password,
    }, Mock())
    assert result == {"host": host, "port": port, "username": user, "password": password, "ssl": False}
    factory.assert_not_called()


def test_no_token_does_not_attempt_network(supervisor, monkeypatch):
    factory, connection, response = supervisor
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    assert mqtt_config.resolve_mqtt_config({}, Mock()) is None
    factory.assert_not_called()


def test_incomplete_manual_credentials_are_not_mixed_with_service_credentials(supervisor):
    factory, connection, response = supervisor
    assert mqtt_config.resolve_mqtt_config({"mqtt_username": "fixture-manual"}, Mock()) is None
    factory.assert_not_called()


@pytest.mark.parametrize("status", [301, 401, 403, 404, 503])
def test_unavailable_or_redirected_service_is_not_followed(supervisor, status):
    factory, connection, response = supervisor
    response.status = status
    assert mqtt_config.resolve_mqtt_config({}, Mock()) is None
    response.read.assert_not_called()
    connection.request.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("failure", [
    TimeoutError("fixture-token-must-not-be-logged"),
    OSError("fixture-token-must-not-be-logged"),
    HTTPException("fixture-token-must-not-be-logged"),
])
def test_discovery_errors_are_nonfatal_and_do_not_log_exception_details(supervisor, failure):
    factory, connection, response = supervisor
    connection.request.side_effect = failure
    log = Mock()
    assert mqtt_config.resolve_mqtt_config({}, log) is None
    assert "fixture-token-must-not-be-logged" not in str(log.call_args_list)
    connection.close.assert_called_once()


@pytest.mark.parametrize("body", [
    b"not json", b"[]", b'{"result":"error","message":"fixture-private-response"}',
    b'{"result":"ok"}', b'{"result":"ok","data":[]}',
])
def test_invalid_service_responses_are_nonfatal(supervisor, body):
    factory, connection, response = supervisor
    response.read.return_value = body
    log = Mock()
    assert mqtt_config.resolve_mqtt_config({}, log) is None
    assert "fixture-private-response" not in str(log.call_args_list)


@pytest.mark.parametrize("changes", [
    {"host": ""}, {"host": None}, {"port": 0}, {"port": 70000},
    {"port": "bad"}, {"port": True}, {"username": []}, {"password": {}},
    {"ssl": "true"}, {"protocol": "3.1"},
])
def test_invalid_or_unsupported_service_settings_are_rejected(supervisor, changes):
    factory, connection, response = supervisor
    response.read.return_value = json.dumps(
        {"result": "ok", "data": {**SERVICE, **changes}}).encode()
    assert mqtt_config.resolve_mqtt_config({}, Mock()) is None


def test_discovered_tls_requirement_is_preserved(supervisor):
    factory, connection, response = supervisor
    response.read.return_value = json.dumps(
        {"result": "ok", "data": {**SERVICE, "port": 8883, "ssl": True}}).encode()
    assert mqtt_config.resolve_mqtt_config({}, Mock())["ssl"] is True


@pytest.mark.parametrize("port", [0, 65536, "bad"])
def test_bad_manual_port_is_nonfatal(supervisor, port):
    factory, connection, response = supervisor
    assert mqtt_config.resolve_mqtt_config({"mqtt_host": "external.example", "mqtt_port": port}, Mock()) is None
    factory.assert_not_called()
