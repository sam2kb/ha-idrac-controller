# HA-iDRAC/ha-idrac-controller/app/web_server.py
from flask import Flask, render_template, request, redirect, flash
from waitress import serve
from markupsafe import Markup

from .fan_logic import FAN_MODES, validate_fan_curve
from http.client import HTTPConnection, HTTPException
import os
import re
import json
import logging
import threading

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


def _app_info_url():
    """Find this installation's app page without hardcoding a repository hash."""
    fallback = "/config/apps"
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return fallback
    connection = HTTPConnection("supervisor", timeout=3)
    try:
        connection.request("GET", "/addons/self/info",
                           headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            return fallback
        payload = json.loads(response.read(65536))
        if not isinstance(payload, dict) or payload.get("result") != "ok":
            return fallback
        data = payload.get("data")
        slug = data.get("slug") if isinstance(data, dict) else None
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9_]+", slug):
            return f"/config/app/{slug}/info"
    except (OSError, HTTPException, ValueError, TypeError):
        # Metadata is optional. Never log tokens, response bodies or errors.
        pass
    finally:
        connection.close()
    return fallback


def save_servers_config(servers):
    with config_lock:
        try:
            with open(SERVERS_CONFIG_FILE, 'w') as f:
                json.dump(servers, f, indent=4)
        except IOError:
            flash("Error: Could not write to config file.", "error")
            return False

    restart_url = _app_info_url()
    label = "Open HA iDRAC Controller" if restart_url.startswith("/config/app/") else "Open Home Assistant Apps"
    message = Markup(
        'Configuration saved. <a href="{}" target="_top">{}</a> '
        'and restart the app to apply changes.'
    ).format(restart_url, label)
    flash(message, "success")
    return True


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
    return render_template('servers.html', servers=servers, defaults=global_config)


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
    servers.append(new_server)
    save_servers_config(servers)
    return redirect('../servers')


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
        return render_template('edit_server.html', server=server_to_edit)
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

    save_servers_config(servers)
    return redirect('../../servers')


@app.route('/servers/delete/<alias>', methods=['POST'])
def delete_server(alias):
    servers = load_servers_config()
    servers_to_keep = [s for s in servers if s['alias'] != alias]
    if len(servers_to_keep) < len(servers):
        save_servers_config(servers_to_keep)
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
