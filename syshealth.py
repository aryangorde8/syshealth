import os
import time
import json
import socket
import subprocess
import sys
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import instance
from collector import Collector
from analyzer import Analyzer
from reporter import Reporter

HOSTNAME = socket.gethostname()
SERVER_URL = os.environ.get("SYSHEALTH_SERVER_URL", "http://13.61.11.18:5000/metrics")
CONTROL_PORT = 5001


def _make_killable():
    """Undo the service's OOM protection for the stress child.

    The unit sets OOMScoreAdjust=-500 so the agent outlives the load it
    generates, but oom_score_adj is inherited across fork and exec — which would
    protect `stress` too. On a t3.micro the workload does not fit in RAM plus
    swap, so the kernel must kill something; with both the agent and stress
    shielded it reaches for sshd instead, and the instance is lost rather than
    measured. Raising the value is always permitted, unlike lowering it.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("500")
    except OSError:
        pass


class ControlHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/stress":
            subprocess.Popen(
                ["stress", "--vm", "2", "--vm-bytes", "800M", "--vm-keep", "--timeout", "60s"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_make_killable,
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"stress started")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def run_control_server():
    HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler).serve_forever()


def calibrate():
    collector = Collector()
    samples = []
    print("Calibrating baseline... keep system idle for 60 seconds")
    for _ in range(12):
        data = collector.get_metrics()
        samples.append(data['psi'])
        time.sleep(5)
    baseline = sum(samples) / len(samples)
    with open("baseline.json", "w") as f:
        json.dump({"psi": baseline}, f)
    print(f"Baseline saved: {baseline:.4f}")


_push_failures = 0


def push_to_server(payload):
    """Send one sample. Never fatal: the agent keeps measuring regardless.

    Failures are counted and reported, because the alternative — silence — is
    what an agent looks like when it cannot reach the dashboard at all. The box
    logs healthy status lines, the dashboard stays empty, and nothing anywhere
    says why. Reported on the first failure and then every 60th (five minutes at
    a 5s interval), so a long outage does not bury the logs.
    """
    global _push_failures
    try:
        requests.post(SERVER_URL, json=payload, timeout=2)
        if _push_failures:
            print(f"push to {SERVER_URL} recovered after {_push_failures} failures", flush=True)
            _push_failures = 0
    except requests.exceptions.RequestException as exc:
        _push_failures += 1
        if _push_failures == 1 or _push_failures % 60 == 0:
            print(
                f"push to {SERVER_URL} failed ({_push_failures} in a row): {exc}",
                file=sys.stderr, flush=True,
            )


def main():
    threading.Thread(target=run_control_server, daemon=True).start()

    collector = Collector()
    analyzer = Analyzer()
    reporter = Reporter()
    identity = instance.detect()

    print(f"SysHealth started | control port {CONTROL_PORT}")
    print(f"Instance: {identity['instance_type']} ({HOSTNAME})")
    print("-" * 50)

    try:
        while True:
            data = collector.get_metrics()
            state, avg_psi, s_d, t_d, reason = analyzer.update(data['psi'], data['vmstat'])
            reporter.log_status(state, data['psi'], avg_psi, s_d, t_d, reason)

            payload = {
                "hostname": HOSTNAME,
                "timestamp": time.time(),
                "state": state,
                "psi": data['psi'],
                "avg_psi": avg_psi,
                "pgscan_delta": s_d,
                "pgsteal_delta": t_d,
                "reason": reason,
                # What the dashboard groups, orders and colours machines by,
                # and what the threshold lines are drawn from.
                "instance_type": identity["instance_type"],
                "instance_id": identity["instance_id"],
                "baseline": analyzer.baseline
            }
            push_to_server(payload)
            time.sleep(5)

    except KeyboardInterrupt:
        print("\nSysHealth stopped.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        calibrate()
    else:
        main()
