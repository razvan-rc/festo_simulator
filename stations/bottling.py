from __future__ import annotations

import random

from stations.base import StationBase
from simulation.models import Container, ProductionLine


class BottlingStation(StationBase):
    """Stops an incoming container, doses liquid by valve timing, then releases it."""

    def __init__(self, line: ProductionLine):
        super().__init__("Bottling")
        self.line = line
        self.current_container: Container | None = None
        self.position_timer = 0.0
        self.fill_timer = 0.0
        self.release_timer = 0.0
        self.target_valve_time = 0.0
        self.tank_level_ml = 8000.0
        self.tank_capacity_ml = 8000.0
        self.target_volume_ml = 500.0
        self.base_flow_ml_s = 240.0
        self.baseline = {
            "fill_volume_ml": self.target_volume_ml,
            "valve_open_time_s": round(self.target_volume_ml / self.base_flow_ml_s, 3),
            "flow_ml_s": self.base_flow_ml_s,
            "tank_level_ml": self.tank_capacity_ml,
        }
        self.inputs = {
            "entry_sensor": False,
            "fill_position_sensor": False,
            "exit_sensor": False,
            "tank_low_sensor": False,
            "tank_high_sensor": True,
            "stopper_home_sensor": True,
            "stopper_extended_sensor": False,
        }
        self.outputs = {
            "conveyor_motor": False,
            "dosing_valve": False,
            "stopper_cylinder": False,
            "tank_inlet_valve": False,
        }

    def _estimated_flow(self) -> float:
        level_ratio = max(0.0, min(1.0, self.tank_level_ml / self.tank_capacity_ml))
        return self.base_flow_ml_s * (0.84 + 0.16 * level_ratio)

    def step(self, dt: float, now: float) -> None:
        self.inputs["entry_sensor"] = bool(self.line.separated_queue) and self.current_container is None
        self.inputs["exit_sensor"] = False
        self.inputs["tank_low_sensor"] = self.tank_level_ml < 1500.0
        self.inputs["tank_high_sensor"] = self.tank_level_ml > 6500.0
        self.outputs["tank_inlet_valve"] = False

        if self.current_container is None and self.tank_level_ml < 1500.0:
            self.process_state = "TANK_REFILL"
            self.memory["cycle_active"] = False
            self.outputs["conveyor_motor"] = False
            self.outputs["tank_inlet_valve"] = True
            self.tank_level_ml = min(self.tank_capacity_ml, self.tank_level_ml + 1200.0 * dt)
            self.measurements["tank_level_ml"] = round(self.tank_level_ml, 1)
            self.measurements["tank_level_pct"] = round(self.tank_level_ml / self.tank_capacity_ml * 100.0, 2)
            if self.tank_level_ml >= self.tank_capacity_ml:
                self.emit("TANK_REFILLED", severity="INFO", component="tank", cooldown=60.0)
            return

        if self.current_container is None:
            if not self.line.separated_queue:
                self.process_state = "IDLE"
                self.memory["cycle_active"] = False
                self.outputs["conveyor_motor"] = False
                return
            if self.line.buffer_full("filled_queue"):
                self.process_state = "BLOCKED"
                self.memory["cycle_active"] = False
                self.outputs["conveyor_motor"] = False
                return

            self.current_container = self.line.separated_queue.pop(0)
            self.current_container.move_to("bottling_fill_position", now)
            self.current_container.state = "POSITIONING"
            self.position_timer = 0.0
            self.fill_timer = 0.0
            self.release_timer = 0.0
            self.target_valve_time = self.target_volume_ml / max(1.0, self._estimated_flow())
            self.cycle_id += 1
            self.memory["cycle_active"] = True
            self.process_state = "POSITIONING"
        self.memory["cycle_active"] = True

        if self.process_state == "POSITIONING":
            self.outputs["conveyor_motor"] = True
            self.position_timer += dt
            if self.position_timer < 0.3:
                return
            self.outputs["conveyor_motor"] = False
            self.outputs["stopper_cylinder"] = True
            self.inputs["stopper_home_sensor"] = False
            self.inputs["stopper_extended_sensor"] = True
            self.inputs["fill_position_sensor"] = True
            self.current_container.state = "FILLING"
            self.process_state = "FILLING"
            return

        if self.process_state == "FILLING":
            self.outputs["conveyor_motor"] = False
            self.outputs["stopper_cylinder"] = True
            self.outputs["dosing_valve"] = True
            self.inputs["fill_position_sensor"] = True
            self.inputs["stopper_home_sensor"] = False
            self.inputs["stopper_extended_sensor"] = True

            valve_wear = self.degradation_score + self.fault_severity("valve_flow_loss")
            stiction = self.fault_severity("valve_stiction")
            flow = self._estimated_flow() * max(0.45, 1.0 - valve_wear * 0.30)
            flow *= max(0.55, 1.0 - stiction * 0.35)
            flow = max(20.0, flow + random.gauss(0.0, 1.8 + valve_wear * 2.0))
            close_time = self.target_valve_time * (1.0 + stiction * 0.12)
            flow_dt = min(dt, max(0.0, close_time - self.fill_timer))
            self.current_container.volume_ml += flow * flow_dt
            self.tank_level_ml = max(0.0, self.tank_level_ml - flow * flow_dt)
            self.fill_timer += flow_dt

            self.measurements["flow_ml_s"] = round(flow, 2)
            self.measurements["tank_level_ml"] = round(self.tank_level_ml, 1)
            self.measurements["tank_level_pct"] = round(self.tank_level_ml / self.tank_capacity_ml * 100.0, 2)
            self.measurements["valve_open_time_s"] = round(self.fill_timer, 3)
            self.measurements["fill_volume_ml"] = round(self.current_container.volume_ml, 1)

            if self.fill_timer + 1e-9 < close_time:
                return

            self.outputs["dosing_valve"] = False
            fill_error = self.current_container.volume_ml - self.target_volume_ml
            self.measurements["fill_error_ml"] = round(fill_error, 1)
            if abs(fill_error) > 15.0:
                self.current_container.quality_ok = False
                self.current_container.defects.append("FILL_VOLUME_OUT_OF_TOLERANCE")
                self.emit(
                    "FILL_VOLUME_OUT_OF_TOLERANCE",
                    severity="WARNING",
                    component="dosing_valve",
                    details={"volume_ml": round(self.current_container.volume_ml, 1)},
                )
            self.current_container.state = "FILLED"
            self.process_state = "RELEASING"
            return

        if self.process_state == "RELEASING":
            self.outputs["dosing_valve"] = False
            self.outputs["stopper_cylinder"] = False
            self.outputs["conveyor_motor"] = True
            self.inputs["fill_position_sensor"] = False
            self.inputs["stopper_home_sensor"] = True
            self.inputs["stopper_extended_sensor"] = False
            self.release_timer += dt
            if self.release_timer < 0.25:
                return

            self.current_container.move_to("pick_place", now)
            self.current_container.state = "WAITING_FOR_CAP"
            self.current_container.history.append({
                "timestamp": now,
                "event": "FILLED",
                "volume_ml": round(self.current_container.volume_ml, 1),
            })
            self.line.filled_queue.append(self.current_container)
            self.inputs["exit_sensor"] = True
            self.emit("CONTAINER_FILLED", severity="INFO", component="dosing_valve", cooldown=30.0)
            self.memory["cycle_count"] += 1
            self.memory["cycle_active"] = False
            self.current_container = None
            self.position_timer = 0.0
            self.fill_timer = 0.0
            self.release_timer = 0.0
            self.outputs["conveyor_motor"] = False
            self.process_state = "IDLE"

    def component_health(self) -> dict[str, float]:
        valve = max(
            0.0,
            1.0 - self.degradation_score
            - self.fault_severity("valve_flow_loss")
            - self.fault_severity("valve_stiction"),
        )
        return {
            "dosing_valve": round(valve, 3),
            "tank_level_sensor": round(min(1.0, max(0.0, self.health_score + 0.04)), 3),
            "stopper_cylinder": round(self.health_score, 3),
            "conveyor": round(self.health_score, 3),
        }
