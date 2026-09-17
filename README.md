# Festo IIoT Simulator v3

This simulator models one integrated Festo station with five coordinated modules:

Distributing, Separating, Bottling, Pick & Place and Sorting.

The simulator separates:

- physical/process state
- PLC-like Inputs, Outputs and Memory bits
- measurements
- health/degradation
- fault scenarios
- telemetry persistence

Version 3 adds process state machines, component-level health, correlated
station faults, finite inter-module buffers, buffered event alerts, baselines,
and common line statistics in every telemetry payload. Bottling exposes states such as
`POSITIONING`, `FILLING`, `RELEASING`, and `TANK_REFILL`; Pick & Place performs
the cap pickup, placement and rotation with its three-axis pneumatic head.

The same container flows through all five modules. Faults create correlated symptoms instead of only changing one random value.

## Install

```bash
cd ~/festo_simulator
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Run

```bash
./venv/bin/python main.py
```

Environment variables:

```bash
DB_HOST=172.31.32.65
DB_USER=sensor_app
DB_PASS='SenzorPass123!'
DB_NAME=industrial_db
SIM_TICK=0.1
TELEMETRY_INTERVAL=0.5
AUTO_FAULTS=1
WEAR_CYCLE_HOURS=168
MAINTENANCE_MINUTES=20
WEAR_START=0.04
WEAR_PEAK=0.92
```

Wear develops continuously over `WEAR_CYCLE_HOURS`; the visible health stages
are `NORMAL`, `EARLY_WEAR`, `DEGRADED`, `CRITICAL`, and `FAILURE`. A planned
maintenance window then lowers the degradation before the next cycle.

## Maintenance command channel

The simulator polls `industrial_db.maintenance_commands` and applies only three
audited commands: `DEMO_ACCELERATE`, `DEMO_RESET`, and `PERFORM_MAINTENANCE`.
Normal degradation remains on the seven-day profile. Demo acceleration affects
only the selected module, expires automatically after at most 15 minutes, and is
published as `health.demo_mode=true`. A maintenance action clears the selected
module's active faults, recalibrates its wear score, records the timestamp in
telemetry, and leaves an `APPLIED` or `FAILED` result in the command history.

The line also reports operational states: `RUNNING`, `BLOCKED`, `STARVED`,
`IDLE`, `FAULT`, and `PLANNED_STOP`.

See [example_api_payload.json](example_api_payload.json) for the v3 telemetry shape.

The service can use `/usr/bin/python3` if PyMySQL is installed system-wide, or the virtualenv Python path shown above.
