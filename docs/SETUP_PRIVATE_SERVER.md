# Running on your own server

1. `git clone <this repo> && cd Smart-agriculture`
2. `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env`, then edit it:
   - `API_KEY` - run `openssl rand -hex 32` and paste the result.
   - `DATA_DIR` - a folder that survives restarts, e.g. `/var/lib/smart-agriculture` (create it, owned by your user).
   - leave `AUTO_WATER=false` for now.
4. Test locally: `cd api && gunicorn --bind 127.0.0.1:8000 app:app` then `curl http://127.0.0.1:8000/health`.
5. Run it as a service: see `deploy/smart-agriculture.service` (restarts after crashes and reboots).
6. Make it reachable over HTTPS (ThingsBoard cloud and the phone must be able to call it):
   - Easiest: **Cloudflare Tunnel** (`cloudflared tunnel --url http://127.0.0.1:8000` for a quick test; a named tunnel for a permanent address). No router ports need opening.
   - Or a domain + Caddy/Nginx reverse proxy with HTTPS.
   - Do not expose the raw port to the internet.
7. Add an uptime check (UptimeRobot / Uptime Kuma) on `/health`.
8. Back up `DATA_DIR/app.db` daily (`cp` or `sqlite3 app.db ".backup ..."`).
9. After the demo: rotate `API_KEY`, the ThingsBoard password and the device tokens.

Run the tests any time: `pip install pytest && python -m pytest tests -q`
