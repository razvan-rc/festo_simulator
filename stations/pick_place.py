from __future__ import annotations

import random

from stations.base import StationBase
from simulation.models import Container, ProductionLine


class PickPlaceStation(StationBase):
    """Stops a filled container and mounts its cap with a three-axis pneumatic head."""

    def __init__(self, line: ProductionLine):
        super().__init__("Pick_and_Place")
        self.line = line
        self.current_container: Container | None = None
        self.timer = 0.0
        self.release_timer = 0.0
        self.base_cycle = 1.8
        self.cap_magazine_count = 120
        self.nominal_torque_nm = 1.2
        self.inputs = {
            "entry_sensor": False,
            "container_position_sensor": False,
            "cap_present_sensor": True,
            "vacuum_ok": False,
            "rotation_complete_sensor": False,
            "exit_sensor": False,
            "stopper_home_sensor": True,
            "stopper_extended_sensor": False,
        }
        self.outputs = {
            "conveyor_motor": False,
            "stopper_cylinder": False,
            "axis_x": False,
            "axis_y": False,
            "axis_z": False,
            "vacuum_ejector": False,
            "cap_rotation_motor": False,
        }
        self.baseline = {
            "cycle_time_s": self.base_cycle,
            "vacuum_pressure_bar": -0.8,
            "cap_torque_nm": self.nominal_torque_nm,
            "cap_success": True,
        }

    def step(self, dt: float, now: float) -> None:
        self.inputs["entry_sensor"] = bool(self.line.filled_queue) and self.current_container is None
        self.inputs["exit_sensor"] = False
        self.inputs["cap_present_sensor"] = self.cap_magazine_count > 0

        if self.current_container is None:
            if not self.line.filled_queue:
                self.process_state = "IDLE"
                self.memory["cycle_active"] = False
                self.outputs["conveyor_motor"] = False
                return
            if self.line.buffer_full("pnp_queue"):
                self.process_state = "BLOCKED"
                self.memory["cycle_active"] = False
                self.outputs["conveyor_motor"] = False
                return

            if self.cap_magazine_count <= 0:
                self.cap_magazine_count = 120
                self.emit("CAP_MAGAZINE_REFILLED", severity="INFO", component="cap_magazine")

            self.current_container = self.line.filled_queue.pop(0)
            self.current_container.move_to("pick_place_cap_position", now)
            self.current_container.state = "CAP_POSITIONING"
            self.timer = 0.0
            self.release_timer = 0.0
            self.cycle_id += 1
            self.memory["cycle_active"] = True
            self.process_state = "POSITIONING"

        self.memory["cycle_active"] = True
        if self.process_state == "POSITIONING":
            self.outputs["conveyor_motor"] = True
            self.timer += dt
            if self.timer < 0.3:
                return
            self.outputs["conveyor_motor"] = False
            self.outputs["stopper_cylinder"] = True
            self.inputs["container_position_sensor"] = True
            self.inputs["stopper_home_sensor"] = False
            self.inputs["stopper_extended_sensor"] = True
            self.timer = 0.0
            self.current_container.state = "CAPPING"
            self.process_state = "PICKING_CAP"
            return

        if self.process_state in {"PICKING_CAP", "PLACING_CAP", "ROTATING_CAP"}:
            fault = self.fault_severity("vacuum_leak")
            target = self.base_cycle * (1.0 + self.degradation_score * 0.35 + fault * 0.75)
            self.timer += dt
            progress = min(1.0, self.timer / target)
            self.outputs["stopper_cylinder"] = True
            self.inputs["container_position_sensor"] = True
            self.outputs["vacuum_ejector"] = progress < 0.72
            self.outputs["axis_x"] = progress < 0.35
            self.outputs["axis_y"] = progress < 0.72
            self.outputs["axis_z"] = 0.18 < progress < 0.82
            self.outputs["cap_rotation_motor"] = progress >= 0.62
            self.process_state = (
                "PICKING_CAP" if progress < 0.35
                else "PLACING_CAP" if progress < 0.62
                else "ROTATING_CAP"
            )

            vacuum_pressure = -0.8 + fault * 0.38 + random.gauss(0.0, 0.012)
            self.inputs["vacuum_ok"] = vacuum_pressure <= -0.55
            self.measurements["vacuum_pressure_bar"] = round(vacuum_pressure, 3)
            if self.timer < target:
                return

            self.cap_magazine_count -= 1
            cap_pick_ok = random.random() > (0.004 + fault ** 2 * 0.55)
            torque = self.nominal_torque_nm * (1.0 - self.degradation_score * 0.07 - fault * 0.22)
            torque += random.gauss(0.0, 0.018 + fault * 0.035)
            torque_ok = 0.9 <= torque <= 1.45
            cap_ok = cap_pick_ok and torque_ok

            self.current_container.cap_installed = cap_ok
            self.inputs["rotation_complete_sensor"] = cap_ok
            self.measurements["cycle_time_s"] = round(self.timer + 0.3, 3)
            self.measurements["cap_torque_nm"] = round(torque, 3)
            self.measurements["cap_success"] = cap_ok
            if not cap_ok:
                self.current_container.quality_ok = False
                defect = "CAP_PICK_FAILURE" if not cap_pick_ok else "CAP_TORQUE_OUT_OF_RANGE"
                self.current_container.defects.append(defect)
                self.emit(defect, severity="WARNING", component="pneumatic_head")

            self.current_container.state = "CAPPED" if cap_ok else "CAP_DEFECT"
            self.process_state = "RELEASING"
            self.timer = 0.0
            self.outputs["axis_x"] = False
            self.outputs["axis_y"] = False
            self.outputs["axis_z"] = False
            self.outputs["vacuum_ejector"] = False
            self.outputs["cap_rotation_motor"] = False
            return

        if self.process_state == "RELEASING":
            self.outputs["stopper_cylinder"] = False
            self.outputs["conveyor_motor"] = True
            self.inputs["container_position_sensor"] = False
            self.inputs["stopper_home_sensor"] = True
            self.inputs["stopper_extended_sensor"] = False
            self.release_timer += dt
            if self.release_timer < 0.3:
                return

            self.current_container.move_to("sorting", now)
            self.line.pnp_queue.append(self.current_container)
            self.inputs["exit_sensor"] = True
            self.memory["cycle_count"] += 1
            self.memory["cycle_active"] = False
            self.current_container = None
            self.release_timer = 0.0
            self.outputs["conveyor_motor"] = False
            self.inputs["vacuum_ok"] = False
            self.inputs["rotation_complete_sensor"] = False
            self.process_state = "IDLE"

    def component_health(self) -> dict[str, float]:
        vacuum = max(0.0, 1.0 - self.degradation_score - self.fault_severity("vacuum_leak"))
        return {
            "pneumatic_axes": round(self.health_score, 3),
            "vacuum_head": round(vacuum, 3),
            "rotation_head": round(max(0.0, self.health_score - 0.03), 3),
            "stopper_cylinder": round(self.health_score, 3),
            "conveyor": round(self.health_score, 3),
        }
