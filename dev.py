#!/usr/bin/env python3
"""Local preview: the dashboard with a simulated four-instance fleet.

`npm run dev` runs this. It exists because the dashboard only says anything
once several differently-sized machines are pushing, and standing up four EC2
instances to look at a chart is a poor trade.

The PSI numbers here are SYNTHETIC. Everything downstream of them is not: each
simulated machine is driven through the real Analyzer, so the states, the
reasons and the 2x/5x threshold lines come from the same code that runs in
production. Only the sensor is faked.

Every page served this way carries a DEMO banner. A fabricated graph that
looks real is worse than no graph at all.
"""

import math
import random
import threading
import time
from datetime import datetime

try:
    from flask import jsonify

    import server
    from analyzer import Analyzer
except ModuleNotFoundError as exc:
    # Ubuntu refuses `pip install` into the system Python (PEP 668), so the
    # obvious next move fails too. Say what does work instead of leaving a
    # traceback and an "externally-managed-environment" error to connect.
    if exc.name not in ("flask", "requests"):
        raise
    raise SystemExit(
        "\n%s is not installed.\n\n"
        "On Debian or Ubuntu, install the packaged versions:\n"
        "    sudo apt install python3-flask python3-requests\n\n"
        "Or keep them out of the system Python with a virtualenv:\n"
        "    python3 -m venv .venv\n"
        "    .venv/bin/pip install flask requests\n"
        "    source .venv/bin/activate    # then: npm run dev\n\n"
        "Plain `pip install flask` will fail with "
        "\"externally-managed-environment\" on these systems.\n" % exc.name
    )

PORT = 5000
TICK_SEC = 5.0          # what the real agent pushes at
BACKFILL_SEC = 60 * 60  # fill the server's whole buffer, so "1h" has a shape

# One profile per size. The idle and loaded figures are the point of the whole
# comparison: under the same `stress --vm 2 --vm-bytes 800M`, a 1 GiB t3.micro
# spends most of its time stalled in reclaim while a 8 GiB t3.large barely
# notices. Baselines differ too — a micro idles noisier than a large, which is
# why the agent calibrates per machine instead of sharing one number.
PROFILES = [
    # type,         baseline, idle,  loaded
    ("t3.micro",    0.09,     0.10,  62.0),
    ("t3.small",    0.05,     0.06,  18.0),
    ("t3.medium",   0.02,     0.03,   3.2),
    ("t3.large",    0.012,    0.015,  0.55),
]

# Idle, a mild elevated spell, then a real load episode. The mild spell matters:
# it is the band where the analyser's 2x/5x lines actually decide something, and
# a simulation that only ever went from dead idle to catastrophic would never
# exercise it — or show it.
# A 15-minute cycle, so the default 15m range shows one readable story — idle,
# a mild spell, idle, one load episode, recovery — rather than three identical
# spikes. It also leaves the 5m range spike-free about half the time, which is
# when the 2x/5x lines are on scale and worth looking at.
CYCLE_SEC = 900.0
MILD_START = 220.0
MILD_END = 430.0
STRESS_START = 600.0
STRESS_RAMP = 20.0
STRESS_HOLD = 60.0
STRESS_DECAY = 30.0


class FakeMachine:
    """One simulated instance: synthetic PSI in, real analyser out."""

    def __init__(self, instance_type, baseline, idle, loaded, index):
        self.instance_type = instance_type
        self.instance_id = "i-demo%011x" % (0xd0 + index)
        self.hostname = "demo-" + instance_type.replace(".", "-")
        self.idle = idle
        self.loaded = loaded

        self.analyzer = Analyzer()
        # Bypass baseline.json: each machine gets the baseline its own idle
        # calibration would have produced.
        self.analyzer.baseline = baseline

        self.pgscan = 1000 * (index + 1)
        self.pgsteal = 900 * (index + 1)
        self.rng = random.Random(1234 + index)

        # Set by "Stress all": an episode outside the regular cycle.
        self.forced_until = 0.0

    def load_fraction(self, ts):
        """0.0 idle to 1.0 fully loaded, at wall-clock time `ts`."""
        if ts < self.forced_until:
            return 1.0

        phase = ts % CYCLE_SEC
        if phase < STRESS_START:
            return 0.0
        into = phase - STRESS_START
        if into < STRESS_RAMP:
            return into / STRESS_RAMP
        if into < STRESS_RAMP + STRESS_HOLD:
            return 1.0
        # Reclaim keeps stalling for a while after the load stops.
        decay = (into - STRESS_RAMP - STRESS_HOLD) / STRESS_DECAY
        return max(0.0, 1.0 - decay)

    def mild_fraction(self, ts):
        """0.0 to 1.0 across the mild spell — a smooth bump, not a square wave."""
        if ts < self.forced_until:
            return 0.0
        phase = ts % CYCLE_SEC
        if not (MILD_START <= phase < MILD_END):
            return 0.0
        # Half a sine over the window: rises, peaks in the middle, falls.
        into = (phase - MILD_START) / (MILD_END - MILD_START)
        return math.sin(into * math.pi)

    def sample(self, ts):
        """Build the payload the real agent would push at time `ts`."""
        load = self.load_fraction(ts)
        # Squared, because stalling is not linear in demand: pressure stays
        # near idle until reclaim cannot keep up, then climbs fast.
        psi = self.idle + (self.loaded - self.idle) * (load ** 2)

        # The mild spell is expressed as a multiple of the machine's own
        # baseline rather than an absolute figure, so it straddles the 2x and 5x
        # lines on every size instead of only on the small ones.
        mild = self.mild_fraction(ts)
        if mild > 0:
            psi = max(psi, self.analyzer.baseline * (1.4 + 3.4 * mild))

        psi = max(0.0, psi * self.rng.uniform(0.82, 1.18))

        # Tie reclaim to the stall figure rather than to the load knob: tasks
        # stall *because* the kernel is scanning, so the two move together.
        # Scans outnumber successful steals.
        scans = int(psi * self.rng.uniform(8, 22))
        self.pgscan += scans
        self.pgsteal += int(scans * self.rng.uniform(0.55, 0.85))

        # Two decimal places, because that is all /proc/pressure/memory gives
        # the real collector.
        psi = round(psi, 2)

        state, avg_psi, s_delta, t_delta, reason = self.analyzer.update(
            psi,
            {"pgscan_direct": self.pgscan, "pgsteal_direct": self.pgsteal},
        )

        return {
            "hostname": self.hostname,
            "timestamp": ts,
            "state": state,
            "psi": psi,
            "avg_psi": round(avg_psi, 4),
            "pgscan_delta": s_delta,
            "pgsteal_delta": t_delta,
            "reason": reason,
            "instance_type": self.instance_type,
            "instance_id": self.instance_id,
            "baseline": self.analyzer.baseline,
            "demo": True,
        }


def ingest(machine, payload, ts):
    """Store a sample as if it had arrived over POST /metrics at time `ts`.

    The real endpoint stamps arrival from the wall clock, which is right for
    an agent and useless for a seeder that has to place samples in the past —
    hence the explicit timestamp here.
    """
    payload = dict(payload)
    payload["received_ts"] = ts
    payload["received_at"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    inst = server.instances[machine.hostname]
    inst["hostname"] = machine.hostname
    inst["instance_type"] = machine.instance_type
    inst["instance_id"] = machine.instance_id
    inst["agent_ip"] = "127.0.0.1"
    inst["last_seen"] = ts
    inst["latest"] = payload
    inst["history"].append(payload)


def backfill(machines, now):
    """An hour of history, so the page has something on it immediately."""
    start = now - BACKFILL_SEC
    ticks = int(BACKFILL_SEC / TICK_SEC)
    for i in range(ticks):
        ts = start + i * TICK_SEC
        for machine in machines:
            ingest(machine, machine.sample(ts), ts)


def push_forever(machines):
    while True:
        now = time.time()
        for machine in machines:
            ingest(machine, machine.sample(now), now)
        time.sleep(TICK_SEC)


def install_demo_routes(machines):
    @server.app.route("/dev-mode")
    def dev_mode():
        return jsonify({"demo": True, "instances": len(machines)})

    # Replace the real /run-stress, which would try to reach agents that do not
    # exist, with one that starts a load episode in the simulation. Keeps the
    # button meaningful in the preview.
    def demo_run_stress():
        until = time.time() + 60.0
        result = {}
        for machine in machines:
            machine.forced_until = until
            result[machine.hostname] = "started (simulated)"
        return jsonify(result)

    server.app.view_functions["run_stress"] = demo_run_stress


def main():
    machines = [
        FakeMachine(t, base, idle, loaded, i)
        for i, (t, base, idle, loaded) in enumerate(PROFILES)
    ]

    now = time.time()
    backfill(machines, now)
    install_demo_routes(machines)
    threading.Thread(target=push_forever, args=(machines,), daemon=True).start()

    print("=" * 66)
    print(" SysHealth dashboard — DEMO MODE")
    print("=" * 66)
    print(" The PSI values are simulated. Nothing here is a measurement of")
    print(" this machine or of any EC2 instance.")
    print()
    for machine in machines:
        print("   %-11s baseline %-6.3f idle ~%-5.2f loaded ~%.2f"
              % (machine.instance_type, machine.analyzer.baseline,
                 machine.idle, machine.loaded))
    print()
    print(" Open http://127.0.0.1:%d" % PORT)
    print(" Use the Instance toggle to compare sizes; 'Stress all' starts a")
    print(" simulated load episode. Ctrl-C to stop.")
    print("=" * 66)

    # No reloader: it would fork a second process, and the seeded history lives
    # in this one's memory.
    server.app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
