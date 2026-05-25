# E-Ink Renderer Commands

Short operational notes for this box.

## Addresses

- Home Assistant API is configured in `.env` as `HA_URL`, for example `http://<home-assistant-ip>:8123`.
- Weather entity is configured in `.env` as `WEATHER_ENTITY=weather.yandex_weather`.
- Debug footer is disabled with `DEBUG_MODE=false`.
- Renderer is this app. It serves `http://<renderer-ip>:8080/dashboard.png`.

## Manual Run

```fish
cd ~/eink-home-dashboard
source .venv/bin/activate.fish
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Make Commands

```bash
make install
```

Create/update `.venv` and install Python dependencies.

```bash
make run
```

Run the renderer in the foreground.

```bash
make check
```

Run compile/import/render smoke checks.

```bash
nano .env
```

Edit Home Assistant token/entity config.

```bash
make service-install
```

Install systemd service, enable it on boot, and start it in the background.

```bash
make service-status
make logs
make service-restart
make service-stop
```

Manage the background service.

```bash
make update
```

Pull code if this directory is a valid git repo, reinstall dependencies, and restart the service.

## Test From Another Machine

```bash
RENDERER_IP=192.0.2.10
curl "http://${RENDERER_IP}:8080/health"
curl -o dashboard.png "http://${RENDERER_IP}:8080/dashboard.png"
```

Browser preview:

```text
http://<renderer-ip>:8080/
http://<renderer-ip>:8080/preview
```

## ESPHome URL

```text
http://<renderer-ip>:8080/dashboard.png
```
