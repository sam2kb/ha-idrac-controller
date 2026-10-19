# HA-iDRAC/ha-idrac-controller/app/web_server.py
from flask import Flask, render_template, request, redirect, flash, session, jsonify
from waitress import serve

from .fan_logic import FAN_MODES, validate_fan_curve
from .power_control import POWER_ACTIONS
from http.client import HTTPConnection, HTTPException
import os
import re
import json
import logging
import threading
import secrets
import tempfile
from contextlib import suppress

log = logging.getLogger('werkzeug')
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- Global paths and locks ---
STATUS_FILE = None
SERVERS_CONFIG_FILE = "/data/servers_config.json"
status_lock = None
config_lock = threading.Lock()
global_config = {}
health_check = None
BOOT_ID = secrets.token_hex(16)
restart_lock = threading.Lock()
restart_state = "idle"


# --- Helper functions ---
def _int_field(form, name, default):
    try:
        return int(form.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_fan_curve(form):
    # Removed rows leave gaps in field indexes. Parse all submitted rows.
    indexes = sorted({
        int(match.group(1))
        for key in form
        if (match := re.fullmatch(r"curve_(?:temp|speed)_(\d+)", key))
    })
    points = []
    for index in indexes:
        temp = form.get(f"curve_temp_{index}")
        speed = form.get(f"curve_speed_{index}")
        if not temp and not speed:
            continue
        points.append({"temp": temp, "speed": speed})
    return validate_fan_curve(points)


def load_servers_config():
    with config_lock:
        if not os.path.exists(SERVERS_CONFIG_FILE):
            return []
        try:
            with open(SERVERS_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []


def save_servers_config(servers):
    """Replace the complete saved configuration before offering a reload."""
    temporary = None
    with config_lock:
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(SERVERS_CONFIG_FILE),
                                             prefix=".servers-", delete=False) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                json.dump(servers, stream, indent=4)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, SERVERS_CONFIG_FILE)
        except OSError:
            flash("Error: Could not write to config file.", "error")
            return False
        finally:
            if temporary:
                with suppress(OSError):
                    os.unlink(temporary)
    return True


def _save_response(servers, parent):
    if save_servers_config(servers):
        if request.form.get("action") == "save_reload" and os.environ.get("SUPERVISOR_TOKEN"):
            session["reload_token"] = secrets.token_urlsafe(32)
            return redirect(parent + "app/reload")
        flash("Configuration saved. Reload or restart the app to apply changes.", "success")
    return redirect(parent + "servers")


def _restart_app():
    """Ask Supervisor to restart only this app; normal SIGTERM cleanup applies."""
    global restart_state
    connection = HTTPConnection("supervisor", timeout=65)
    try:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise ValueError
        connection.request("POST", "/addons/self/restart", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        payload = json.loads(response.read(65536)) if response.status == 200 else None
        if not isinstance(payload, dict) or payload.get("result") != "ok":
            raise ValueError
    except (OSError, HTTPException, ValueError, TypeError):
        with restart_lock:
            restart_state = "failed"
        log.warning("App restart request failed. Saved configuration is unchanged.")
    finally:
        connection.close()


@app.route("/app/reload", methods=["GET", "POST"])
def reload_app():
    global restart_state
    if request.method == "GET":
        if not session.get("reload_token"):
            return redirect("../servers")
        response = app.make_response(render_template("reload.html", boot_id=BOOT_ID, reload_token=session["reload_token"]))
        response.headers["Cache-Control"] = "no-store"
        return response
    expected = session.get("reload_token")
    supplied = request.headers.get("X-Reload-Token", "")
    if not isinstance(expected, str) or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        return jsonify(error="Reload request expired. Save your changes again."), 403
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return jsonify(error="App reload requires Home Assistant Supervisor. Your configuration is saved."), 503
    with restart_lock:
        if restart_state == "restarting":
            return jsonify(error="The app is already reloading."), 409
        session.pop("reload_token", None)
        restart_state = "restarting"
        # The browser receives its response before the Supervisor stops us.
        timer = threading.Timer(0.5, _restart_app)
        timer.daemon = True
        timer.start()
    return jsonify(state="restarting"), 202


@app.route("/app/status")
def app_status():
    response = jsonify(boot_id=BOOT_ID, state=restart_state)
    response.headers["Cache-Control"] = "no-store"
    return response


def load_all_servers_status():
    if STATUS_FILE and os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def set_health_check(callback):
    global health_check
    health_check = callback


# --- Routes ---
@app.route('/health')
def health():
    healthy = health_check() if health_check else True
    return ("ok" if healthy else "unhealthy"), (200 if healthy else 503)


@app.route('/')
def index():
    all_statuses = load_all_servers_status()
    all_statuses.sort(key=lambda x: x.get('alias', ''))
    return render_template('index.html', servers=all_statuses)


@app.route('/servers')
def manage_servers():
    servers = load_servers_config()
    return render_template('servers.html', servers=servers, defaults=global_config, power_actions=POWER_ACTIONS, can_reload=bool(os.environ.get("SUPERVISOR_TOKEN")))


@app.route('/servers/add', methods=['POST'])
def add_server():
    servers = load_servers_config()
    new_alias = (request.form.get('alias') or "").strip()
    if not new_alias:
        flash("Server alias is required.", "error")
        return redirect('../servers')
    if any(s['alias'] == new_alias for s in servers):
        flash(f"Server alias '{new_alias}' already exists.", "error")
        return redirect('../servers')

    fan_mode = request.form.get('fan_mode') or 'auto'
    if fan_mode not in FAN_MODES:
        flash("Choose a supported cooling mode.", "error")
        return redirect('../servers')
    try:
        fan_curve = _parse_fan_curve(request.form) if fan_mode == 'curve' else []
    except ValueError as error:
        flash(f"Invalid fan curve: {error}", "error")
        return redirect('../servers')

    new_server = {
        "alias": new_alias,
        "idrac_ip": (request.form.get('idrac_ip') or "").strip(),
        "idrac_username": request.form.get('idrac_username') or "root",
        "idrac_password": request.form.get('idrac_password') or "",
        "enabled": True,
        "fan_control_enabled": request.form.get('fan_control_enabled', 'true') == 'true',
        "disable_pcie_cooling_response": request.form.get('disable_pcie_cooling_response') == 'true',
        "fan_mode": fan_mode,
        "fan_curve": fan_curve,
        "base_fan_speed_percent": _int_field(request.form, 'base_fan_speed_percent', 20),
        "low_temp_threshold": _int_field(request.form, 'low_temp_threshold', 45),
        "high_temp_fan_speed_percent": _int_field(request.form, 'high_temp_fan_speed_percent', 50),
        "critical_temp_threshold": _int_field(request.form, 'critical_temp_threshold', 65),
    }
    for action in POWER_ACTIONS.values():
        new_server[action["option"]] = request.form.get(action["option"]) == "true"
    servers.append(new_server)
    return _save_response(servers, "../")


@app.route('/servers/edit/<alias>')
def edit_server_form(alias):
    servers = load_servers_config()
    server_to_edit = next((s for s in servers if s['alias'] == alias), None)
    if server_to_edit:
        server_to_edit.setdefault('fan_mode', 'auto')
        server_to_edit.setdefault('fan_curve', [])
        server_to_edit.setdefault('pid_config', {})
        server_to_edit.setdefault('target_temp', 55)
        server_to_edit.setdefault('fan_control_enabled', True)
        server_to_edit.setdefault('disable_pcie_cooling_response', False)
        return render_template('edit_server.html', server=server_to_edit, power_actions=POWER_ACTIONS, can_reload=bool(os.environ.get("SUPERVISOR_TOKEN")))
    flash(f"Server '{alias}' not found.", "error")
    return redirect('../../servers')


@app.route('/servers/update/<alias>', methods=['POST'])
def update_server(alias):
    servers = load_servers_config()
    server_to_update = next((s for s in servers if s['alias'] == alias), None)
    if not server_to_update:
        flash(f"Server '{alias}' not found.", "error")
        return redirect('../../servers')

    fan_mode = request.form.get('fan_mode') or server_to_update.get('fan_mode', 'auto')
    if fan_mode not in FAN_MODES:
        flash("Choose a supported cooling mode.", "error")
        return redirect('../../servers')
    fan_curve = server_to_update.get('fan_curve', [])
    if fan_mode == 'curve':
        try:
            fan_curve = _parse_fan_curve(request.form)
        except ValueError as error:
            flash(f"Invalid fan curve: {error}", "error")
            return redirect('../../servers')

    server_to_update['idrac_ip'] = (request.form.get('idrac_ip') or "").strip()
    server_to_update['idrac_username'] = request.form.get('idrac_username') or "root"
    new_password = request.form.get('idrac_password')
    if new_password:
        server_to_update['idrac_password'] = new_password
    server_to_update['enabled'] = request.form.get('enabled') == 'true'
    server_to_update['fan_control_enabled'] = request.form.get('fan_control_enabled') == 'true'
    server_to_update['disable_pcie_cooling_response'] = request.form.get('disable_pcie_cooling_response') == 'true'

    for action in POWER_ACTIONS.values():
        server_to_update[action["option"]] = request.form.get(action["option"]) == "true"

    server_to_update['fan_mode'] = fan_mode

    server_to_update['base_fan_speed_percent'] = _int_field(request.form, 'base_fan_speed_percent', 20)
    server_to_update['low_temp_threshold'] = _int_field(request.form, 'low_temp_threshold', 45)
    server_to_update['high_temp_fan_speed_percent'] = _int_field(request.form, 'high_temp_fan_speed_percent', 50)
    server_to_update['critical_temp_threshold'] = _int_field(request.form, 'critical_temp_threshold', 65)

    server_to_update['pid_config'] = {
        "target_temp": _int_field(request.form, 'target_temp', 55),
        "kp": float(request.form.get('pid_kp') or 4.0),
        "ki": float(request.form.get('pid_ki') or 0.2),
        "kd": float(request.form.get('pid_kd') or 0.1),
    }

    server_to_update['fan_curve'] = fan_curve

    return _save_response(servers, "../../")


@app.route('/servers/delete/<alias>', methods=['POST'])
def delete_server(alias):
    servers = load_servers_config()
    servers_to_keep = [s for s in servers if s['alias'] != alias]
    if len(servers_to_keep) < len(servers):
        if save_servers_config(servers_to_keep):
            flash("Configuration saved. Reload or restart the app to apply changes.", "success")
    else:
        flash(f"Server '{alias}' not found.", "error")
    return redirect('../../servers')


def run_web_server(port, status_file_path, lock):
    global STATUS_FILE, status_lock
    STATUS_FILE = status_file_path
    status_lock = lock

    host = '0.0.0.0'
    print(f"[WEBSERVER INFO] Serving with waitress on {host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=4)
