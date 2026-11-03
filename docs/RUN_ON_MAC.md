# Running the server + agent on a Mac

Two programs run side by side:
* **web** (`gunicorn app:app`) - the HTTP API (health check, uploads, /decisions).
* **agent** (`python agent.py`) - checks ThingsBoard every minute, decides on fresh soil readings,
  takes a photo from the phone camera every 30 minutes in daylight.

Both start at login and restart by themselves.

## One-time setup
```bash
cd ~/Smart-agriculture && git pull
source venv/bin/activate && pip install -r requirements.txt
mkdir -p ~/Library/LaunchAgents
for n in web agent; do
  sed "s|__HOME__|$HOME|g" deploy/com.smartagriculture.$n.plist > ~/Library/LaunchAgents/com.smartagriculture.$n.plist
  launchctl bootout gui/$(id -u)/com.smartagriculture.$n 2>/dev/null
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.smartagriculture.$n.plist
done
```
Keep the Mac awake while plugged in: `sudo pmset -c sleep 0 disksleep 0`

## Check / logs / stop
```bash
tail -n 30 ~/Smart-agriculture/agent.log      # what the agent decided
tail -n 30 ~/Smart-agriculture/web.log
launchctl bootout gui/$(id -u)/com.smartagriculture.agent   # stop (same for .web)
```
After changing `.env` or pulling new code, run the setup loop above again.

## Safety
`AUTO_WATER=false` (default): decisions are only recorded. Turn it on only after you have watched the log for a few days.
