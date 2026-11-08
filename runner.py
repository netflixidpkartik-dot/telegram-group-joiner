# -*- coding: utf-8 -*-
import subprocess
import sys
import time
import signal
import os

# Set console output encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

current_proc = None
shutting_down = False

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
