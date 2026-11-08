# -*- coding: utf-8 -*-
import subprocess
import sys
import time
import signal
import os
import threading
import http.server
import socketserver
import json

# Set console output encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

current_proc = None
shutting_down = False

# --- HTTP Healthcheck Server for Railway ---
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joiner_state.json")
        stats = {}
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    stats = {
                        "status": "online",
                        "folder_name": "grps",
                        "joined_count": len(data.get("joined_groups", {})),
                        "queue_len": len(data.get("queue", [])),
                        "failed_count": len(data.get("failed_groups", [])),
                        "combined_folder_link": data.get("combined_folder_link", ""),
                        "status_text": data.get("status_text", "")
                    }
            except Exception:
                stats = {"status": "online"}
        else:
            stats = {"status": "online", "message": "Starting up..."}

        body = json.dumps(stats, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress health check access logs

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    # Allow socket address reuse so restarts never crash with 'Address already in use'
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
            print(f"[SUPERVISOR] HTTP Healthcheck server listening on 0.0.0.0:{port}", flush=True)
            httpd.serve_forever()
    except Exception as e:
        print(f"[SUPERVISOR] Health server warning: {e}", flush=True)

# Start health check server in background daemon thread immediately
threading.Thread(target=start_health_server, daemon=True).start()

def handle_signal(sig, frame):
    global shutting_down, current_proc
    shutting_down = True
    print(f"\n[SUPERVISOR] Caught signal {sig} (SIGTERM/SIGINT). Initiating graceful shutdown...", flush=True)
    if current_proc and current_proc.poll() is None:
        try:
            print("[SUPERVISOR] Terminating child process (main.py)...", flush=True)
            current_proc.terminate()
            current_proc.wait(timeout=10)
        except Exception as e:
            print(f"[SUPERVISOR] Force-killing child process: {e}", flush=True)
            try:
                current_proc.kill()
            except Exception:
                pass
    print("[SUPERVISOR] Child process stopped. Exiting supervisor.", flush=True)
    sys.exit(0)

try:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
except Exception as sig_err:
    print(f"[SUPERVISOR] Note: Could not register signal handlers ({sig_err})", flush=True)

print("================================================================", flush=True)
print("  24/7 TELEGRAM GROUP JOINER & FOLDER COMBINER BOT SUPERVISOR   ", flush=True)
print("  Automatically keeps main.py alive 24/7 with zero downtime      ", flush=True)
print("================================================================", flush=True)

run_count = 0
while not shutting_down:
    run_count += 1
    now_str = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
    print(f"\n[SUPERVISOR] Launching main.py (Run #{run_count}) at {now_str}...", flush=True)
    start_time = time.time()
    try:
        # Launch main.py with unbuffered output
        current_proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            env={**os.environ, "PYTHONUNBUFFERED": "1"}
        )
        exit_code = current_proc.wait()
        uptime_sec = int(time.time() - start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"
        print(f"\n[SUPERVISOR] main.py exited with code {exit_code} after {uptime_str} of uptime.", flush=True)
    except Exception as run_err:
        print(f"\n[SUPERVISOR] Failed to execute main.py: {run_err}", flush=True)

    if shutting_down:
        print("[SUPERVISOR] Shutdown complete.", flush=True)
        break

    print("[SUPERVISOR] Auto-restarting main.py in 3 seconds...", flush=True)
    time.sleep(3)
