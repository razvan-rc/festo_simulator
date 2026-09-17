from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pymysql

from simulation.models import ProductionLine
from stations.bottling import BottlingStation
from stations.distributing import DistributingStation
from stations.pick_place import PickPlaceStation
from stations.separating import SeparatingStation
from stations.sorting import SortingStation

DB_HOST = os.getenv("DB_HOST", "172.31.32.65")
DB_USER = os.getenv("DB_USER", "sensor_app")
DB_PASS = os.getenv("DB_PASS", "SenzorPass123!")
DB_NAME = os.getenv("DB_NAME", "industrial_db")
TICK_SECONDS = float(os.getenv("SIM_TICK", "0.1"))
TELEMETRY_INTERVAL = float(os.getenv("TELEMETRY_INTERVAL", "0.5"))
AUTO_FAULTS = os.getenv("AUTO_FAULTS", "1") == "1"
WEAR_CYCLE_HOURS = max(1.0, float(os.getenv("WEAR_CYCLE_HOURS", "168")))
MAINTENANCE_MINUTES = max(1.0, float(os.getenv("MAINTENANCE_MINUTES", "20")))
WEAR_START = max(0.0, min(0.25, float(os.getenv("WEAR_START", "0.04"))))
WEAR_PEAK = max(0.5, min(1.0, float(os.getenv("WEAR_PEAK", "0.92"))))
COMMAND_POLL_INTERVAL = max(0.5, float(os.getenv("COMMAND_POLL_INTERVAL", "1")))

STATION_PROFILES = {
    "Distributing": {"wear": 0.78, "fault": 0.85, "fault_onset": 0.58},
    "Separating": {"wear": 1.04, "fault": 0.82, "fault_onset": 0.52},
    "Bottling": {"wear": 1.12, "fault": 1.00, "fault_onset": 0.50},
    "Pick_and_Place": {"wear": 1.00, "fault": 0.92, "fault_onset": 0.54},
    "Sorting": {"wear": 0.84, "fault": 0.86, "fault_onset": 0.57},
}

FAULT_MAP = {
    "Distributing": "cylinder_slowdown",
    "Separating": "sensor_drift",
    "Bottling": "valve_flow_loss",
    "Pick_and_Place": "vacuum_leak",
    "Sorting": "sensor_misread",
}


def get_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def stage_for_degradation(value: float) -> str:
    if value >= 0.90:
        return "FAILURE"
    if value >= 0.72:
        return "CRITICAL"
    if value >= 0.42:
        return "DEGRADED"
    if value >= 0.15:
        return "EARLY_WEAR"
    return "NORMAL"


def maybe_inject_scenario(stations, elapsed: float) -> str:
    if not AUTO_FAULTS:
        for station in stations:
            station.set_scenario("NORMAL", WEAR_START)
            station.clear_fault()
        return "NORMAL"

    wear_seconds = WEAR_CYCLE_HOURS * 3600.0
    maintenance_seconds = MAINTENANCE_MINUTES * 60.0
    position = elapsed % (wear_seconds + maintenance_seconds)
    maintenance = position >= wear_seconds

    if maintenance:
        maintenance_progress = (position - wear_seconds) / maintenance_seconds
        base_target = WEAR_PEAK + (WEAR_START - WEAR_PEAK) * maintenance_progress
        global_stage = "MAINTENANCE"
    else:
        wear_progress = position / wear_seconds
        base_target = WEAR_START + (WEAR_PEAK - WEAR_START) * wear_progress ** 1.15
        global_stage = stage_for_degradation(base_target)

    by_name = {station.name: station for station in stations}
    for station in stations:
        profile = STATION_PROFILES[station.name]
        target = max(0.0, min(1.0, base_target * profile["wear"]))
        stage = "MAINTENANCE" if maintenance else stage_for_degradation(target)
        station.set_scenario(stage, target)
        station.clear_fault()
        if not maintenance and target >= profile["fault_onset"]:
            normalized = (target - profile["fault_onset"]) / (1.0 - profile["fault_onset"])
            station.inject_fault(FAULT_MAP[station.name], normalized * profile["fault"])

    bottling = by_name["Bottling"]
    if not maintenance and bottling.scenario_target >= 0.72:
        valve_severity = (bottling.scenario_target - 0.72) / 0.28
        bottling.inject_fault("valve_stiction", valve_severity * 0.65)
    return global_stage


def apply_control_overrides(stations, now: float, controls: dict) -> None:
    for station in stations:
        maintenance_until = controls["maintenance"].get(station.name, 0.0)
        if maintenance_until > now:
            station.demo_mode = False
            station.clear_fault()
            station.set_scenario("MAINTENANCE", WEAR_START)
            continue
        controls["maintenance"].pop(station.name, None)

        demo = controls["demo"].get(station.name)
        if demo and demo["until"] <= now:
            controls["demo"].pop(station.name, None)
            demo = None
        station.demo_mode = bool(demo)
        if not demo:
            continue

        target = demo["target"]
        profile = STATION_PROFILES[station.name]
        station.clear_fault()
        station.set_scenario(stage_for_degradation(target), target)
        if target >= profile["fault_onset"]:
            normalized = (target - profile["fault_onset"]) / (1.0 - profile["fault_onset"])
            station.inject_fault(FAULT_MAP[station.name], normalized * profile["fault"])
        if station.name == "Bottling" and target >= .72:
            station.inject_fault("valve_stiction", (target - .72) / .28 * .65)


def process_commands(cursor, stations, controls: dict, now: float) -> None:
    cursor.execute(
        """SELECT id, station_name, action_type, component, parameters_json
           FROM maintenance_commands WHERE status='PENDING' ORDER BY id LIMIT 10"""
    )
    commands = cursor.fetchall()
    by_name = {station.name: station for station in stations}
    for command in commands:
        command_id = command["id"]
        cursor.execute(
            "UPDATE maintenance_commands SET status='PROCESSING' WHERE id=%s AND status='PENDING'",
            (command_id,),
        )
        if cursor.rowcount != 1:
            continue
        try:
            station = by_name[command["station_name"]]
            parameters = command["parameters_json"] or {}
            if isinstance(parameters, str):
                parameters = json.loads(parameters)
            action = command["action_type"]
            if action == "DEMO_ACCELERATE":
                target = max(.45, min(.78, float(parameters.get("target", .68))))
                duration = max(60, min(900, int(parameters.get("duration_seconds", 600))))
                controls["demo"][station.name] = {"target": target, "until": now + duration}
                controls["maintenance"].pop(station.name, None)
                station.emit(
                    "DEMO_ACCELERATION_STARTED", "INFO", command["component"],
                    {"target_degradation": round(target, 3), "duration_seconds": duration}, cooldown=0,
                )
                result = {"message": "Scenariu demonstrativ activat", "target": target, "duration_seconds": duration}
            elif action == "DEMO_RESET":
                controls["demo"].pop(station.name, None)
                station.demo_mode = False
                station.emit("DEMO_ACCELERATION_STOPPED", "INFO", command["component"], cooldown=0)
                result = {"message": "Scenariu demonstrativ oprit"}
            elif action == "PERFORM_MAINTENANCE":
                before = station.degradation_score
                hold_seconds = max(5, min(60, int(parameters.get("hold_seconds", 12))))
                controls["demo"].pop(station.name, None)
                controls["maintenance"][station.name] = now + hold_seconds
                station.degradation_score = WEAR_START
                station.health_score = 1.0 - WEAR_START
                station.clear_fault()
                station._fault_history.clear()
                station.last_maintenance_at = datetime.now(timezone.utc).isoformat()
                station.emit(
                    "MAINTENANCE_COMPLETED", "INFO", command["component"],
                    {"degradation_before": round(before, 3), "degradation_after": WEAR_START}, cooldown=0,
                )
                result = {
                    "message": "Mentenanță înregistrată și uzură recalibrată",
                    "degradation_before": round(before, 3), "degradation_after": WEAR_START,
                    "hold_seconds": hold_seconds,
                }
            else:
                raise ValueError(f"Acțiune nesuportată: {action}")
            cursor.execute(
                """UPDATE maintenance_commands
                   SET status='APPLIED', applied_at=UTC_TIMESTAMP(3), result_json=%s WHERE id=%s""",
                (json.dumps(result), command_id),
            )
            print(f"[COMMAND] id={command_id} station={station.name} action={action} status=APPLIED")
        except Exception as error:
            cursor.execute(
                """UPDATE maintenance_commands
                   SET status='FAILED', applied_at=UTC_TIMESTAMP(3), result_json=%s WHERE id=%s""",
                (json.dumps({"error": str(error)}), command_id),
            )
            print(f"[COMMAND] id={command_id} status=FAILED error={error}")


def main():
    line = ProductionLine()
    stations = [
        DistributingStation(line),
        SeparatingStation(line),
        BottlingStation(line),
        PickPlaceStation(line),
        SortingStation(line),
    ]
    for station in stations:
        initial_wear = WEAR_START * STATION_PROFILES[station.name]["wear"]
        station.degradation_score = initial_wear
        station.health_score = 1.0 - initial_wear
        station.wear_cycle_hours = WEAR_CYCLE_HOURS

    conn = get_connection()
    cursor = conn.cursor()
    start = time.monotonic()
    last_telemetry = 0.0
    last_db_retry = 0.0
    last_command_poll = 0.0
    last_stage = None
    controls = {"demo": {}, "maintenance": {}}

    try:
        while True:
            now = time.monotonic()
            elapsed = now - start
            if now - last_command_poll >= COMMAND_POLL_INTERVAL:
                try:
                    process_commands(cursor, stations, controls, now)
                except pymysql.MySQLError as error:
                    print(f"[COMMAND] poll_failed error={error}")
                last_command_poll = now

            stage = maybe_inject_scenario(stations, elapsed)
            apply_control_overrides(stations, now, controls)
            if stage != last_stage:
                print(f"[SCENARIO] stage={stage}")
                last_stage = stage

            for station in stations:
                station.update(TICK_SECONDS, now)
            line.update_operational_state(stations, TICK_SECONDS)

            if now - last_telemetry >= TELEMETRY_INTERVAL:
                for station in stations:
                    payload = station.get_payload()
                    payload["line"] = line.payload()
                    sql = """
                        INSERT INTO festo_telemetry (timestamp, station_name, payload_json)
                        VALUES (%s, %s, %s)
                    """
                    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    cursor.execute(sql, (timestamp, station.name, json.dumps(payload)))
                    if payload["events"]:
                        print(f"{payload['timestamp']} | {station.name} | events={payload['events']}")
                    station.acknowledge_events()
                last_telemetry = now

            if now - last_db_retry >= 30:
                conn.ping(reconnect=True)
                last_db_retry = now

            time.sleep(TICK_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
