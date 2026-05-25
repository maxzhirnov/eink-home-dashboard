SHELL := /bin/bash

SERVICE := eink-renderer
SERVICE_FILE := /etc/systemd/system/$(SERVICE).service
PROJECT_DIR := $(CURDIR)
RUN_USER := $(shell whoami)
RUN_GROUP := $(shell id -gn)
SUDO := $(shell if [ "$$(id -u)" -ne 0 ]; then printf 'sudo'; fi)
PYTHON ?= python3
VENV := $(PROJECT_DIR)/.venv
HOST ?= 0.0.0.0
PORT ?= 8080

.PHONY: help install run check service-install service-start service-stop service-restart service-status logs update

help:
	@printf '%s\n' \
		'Targets:' \
		'  make install          Create/update .venv and install Python deps' \
		'  make run              Run renderer in foreground on 0.0.0.0:8080' \
		'  make check            Compile/import/render smoke checks' \
		'  make service-install  Install and start systemd background service' \
		'  make service-restart  Restart systemd service' \
		'  make service-status   Show systemd service status' \
		'  make logs             Follow systemd logs' \
		'  make update           Pull code if git is available, install deps, restart service'

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

run:
	$(VENV)/bin/uvicorn app:app --host $(HOST) --port $(PORT)

check:
	$(VENV)/bin/python -m py_compile app.py
	$(VENV)/bin/python -c "import app; print(app.app.title)"
	$(VENV)/bin/python -c "from datetime import datetime, timedelta; from PIL import Image; import app; now=datetime.now(app.DISPLAY_TZ); hourly=[{'datetime': (now + timedelta(hours=i)).isoformat(), 'condition': 'rainy' if i == 6 else 'sunny', 'native_temperature': 20 + i % 8} for i in range(72)]; events=[{'summary': 'Smoke test', 'start': (now + timedelta(hours=1)).isoformat()}, {'summary': 'Massage', 'start': (now + timedelta(hours=4)).isoformat()}, {'summary': 'Español', 'start': (now + timedelta(days=1, hours=2)).isoformat()}]; app._render_dashboard({'calendar_events': events, 'weather': {'state': 'sunny', 'attributes': {'temperature': 30, 'feels_like': 32, 'yandex_condition': 'sunny', 'forecastHourly': hourly}}, 'living_temp': {'state': '22'}, 'living_humidity': {'state': '44'}, 'kitchen_temp': {'state': '21'}, 'kitchen_humidity': {'state': '48'}, 'usd_rub': {'state': '89.12'}, 'quote': {'state': 'OK'}}, []); im = Image.open(app.OUTPUT_PATH); print(im.size, im.mode)"

service-install: install
	sed -e 's|__RUN_USER__|$(RUN_USER)|g' -e 's|__RUN_GROUP__|$(RUN_GROUP)|g' -e 's|__PROJECT_DIR__|$(PROJECT_DIR)|g' eink-renderer.service | $(SUDO) install -m 0644 /dev/stdin $(SERVICE_FILE)
	$(SUDO) systemctl daemon-reload
	$(SUDO) systemctl enable --now $(SERVICE)
	$(SUDO) systemctl status $(SERVICE) --no-pager

service-start:
	$(SUDO) systemctl start $(SERVICE)

service-stop:
	$(SUDO) systemctl stop $(SERVICE)

service-restart:
	$(SUDO) systemctl restart $(SERVICE)

service-status:
	systemctl status $(SERVICE) --no-pager

logs:
	journalctl -u $(SERVICE) -f

update:
	@if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git pull --ff-only; else echo 'No valid git repository here; skipping git pull.'; fi
	$(MAKE) install
	@if systemctl list-unit-files $(SERVICE).service >/dev/null 2>&1; then $(SUDO) systemctl restart $(SERVICE); else echo 'Service is not installed yet. Run make service-install.'; fi
