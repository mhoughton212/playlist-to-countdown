"""Launch with python -m countdown or Start Countdown.cmd."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.error import URLError

from .studio import PORT
from .web import APP_VERSION, create_app


def _listening_pid():
    result = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True, text=True, check=False)
    match = re.search(rf'^\s*TCP\s+127\.0\.0\.1:{PORT}\s+\S+\s+LISTENING\s+(\d+)\s*$', result.stdout, re.MULTILINE | re.IGNORECASE)
    return int(match.group(1)) if match else None


def _restart_stale_server():
    pid = _listening_pid()
    if not pid:
        return False
    stopped = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, text=True, check=False)
    if stopped.returncode:
        # The local process may be owned by the desktop launcher. PowerShell's
        # process API can stop it in environments where taskkill is denied.
        subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                        f'Stop-Process -Id {pid} -Force'], capture_output=True, text=True, check=False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _listening_pid():
            return True
        time.sleep(.1)
    return False


def main():
    import argparse
    import webbrowser
    from urllib.request import urlopen
    from werkzeug.serving import WSGIRequestHandler

    class QuietCallbackHandler(WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            if self.path.startswith('/callback'):
                self.log('info', 'Spotify callback %s', code)
            else:
                super().log_request(code, size)

    parser = argparse.ArgumentParser(description='Run Playlist to Countdown locally.')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--restart', action='store_true',
                        help='Restart an existing Playlist to Countdown server on the configured local port.')
    parser.add_argument('--reload', action='store_true',
                        help='Reload the server automatically when Python source files change.')
    args = parser.parse_args()
    running_app = None
    try:
        with urlopen(f'http://127.0.0.1:{PORT}/api/health', timeout=1) as response:
            running_app = json.load(response)
    except (OSError, URLError, ValueError):
        pass
    already_running = running_app and running_app.get('app') == 'countdown-studio'
    if already_running and (args.restart or running_app.get('version') != APP_VERSION):
        if not _restart_stale_server():
            print(f'Playlist to Countdown is running on port {PORT}, but it could not be restarted automatically.', flush=True)
            sys.exit(1)
        already_running = False
    if already_running:
        print(f'Playlist to Countdown is already running: http://127.0.0.1:{PORT}')
        if not args.no_browser:
            webbrowser.open(f'http://127.0.0.1:{PORT}')
        sys.exit(0)
    print(f'Playlist to Countdown: http://127.0.0.1:{PORT}', flush=True)
    # Werkzeug runs main() once in the reloader parent and once in the server
    # child. Open the dashboard only from the parent so reloads do not spawn
    # extra browser tabs.
    if not args.no_browser and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        threading.Timer(1, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}')).start()
    create_app().run(host='127.0.0.1', port=PORT, debug=False, threaded=True,
                     use_reloader=args.reload, request_handler=QuietCallbackHandler)


if __name__ == '__main__':
    main()
