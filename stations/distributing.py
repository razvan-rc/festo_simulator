from __future__ import annotations

import random

from stations.base import StationBase
from simulation.models import ProductionLine


class DistributingStation(StationBase):
    """Picks one empty container from the magazine and places it on the first conveyor."""

    def __init__(self, line: ProductionLine):
        super().__init__("Distributing")
        self.line = line
        self.magazine_count = 30
        self.timer = 0.0
        self.base_cycle = 1.1
        self.outputs = {
            "vertical_axis_motor": False,
            "transfer_axis_motor": False,
            "vacuum_ejector": False,
            "separating_conveyor_request": False,
        }
        self.inputs = {
            "container_available": False,
            "rod_home": True,
            "rod_extended": False,
            "vacuum_ok": False,
            "container_exit_sensor": False,
        }
        self.baseline = {
            "cycle_time_s": self.base_cycle,
            "actuator_response_time_s": 0.28,
            "vacuum_pressure_bar": -0.72,
        }

    def step(self, dt: float, now: float) -> None:
        self.inputs["container_available"] = self.magazine_count > 0
        self.inputs["container_exit_sensor"] = False
        self.outputs["separating_conveyor_request"] = False

        if self.line.buffer_full("part_queue"):
            self.process_state = "BLOCKED"
            self.memory["cycle_active"] = False
            self.timer = 0.0
            return

        if self.magazine_count <= 0:
            self.magazine_count = 30
            self.emit("MAGAZINE_REFILLED", severity="INFO", component="container_magazine")

        self.timer += dt
        target = self.base_cycle * (
            1.0 + self.degradation_score * 0.5 + self.fault_severity("cylinder_slowdown") * 1.2
        )
        progress = min(1.0, self.timer / target)
        self.memory["cycle_active"] = True
        self.inputs["rod_home"] = False
        self.outputs["vacuum_ejector"] = True

        if progress < 0.35:
            self.process_state = "VACUUM_PICK"
            self.outputs["vertical_axis_motor"] = True
            self.inputs["rod_extended"] = True
        elif progress < 0.8:
            self.process_state = "TRANSFER_TO_CONVEYOR"
            self.outputs["vertical_axis_motor"] = False
            self.outputs["transfer_axis_motor"] = True
            self.inputs["rod_extended"] = False
        else:
            self.process_state = "RELEASING"

        vacuum_pressure = -0.72 + self.degradation_score * 0.08 + random.gauss(0.0, 0.008)
        self.inputs["vacuum_ok"] = vacuum_pressure <= -0.55
        self.measurements["vacuum_pressure_bar"] = round(vacuum_pressure, 3)

        if self.timer < target:
            return

        container = self.line.create_container()
        container.state = "EMPTY"
        container.move_to("separating", now)
        self.line.part_queue.append(container)
        self.magazine_count -= 1

        self.inputs["container_exit_sensor"] = True
        self.outputs["separating_conveyor_request"] = True
        self.measurements["cycle_time_s"] = round(self.timer, 3)
        self.measurements["actuator_response_time_s"] = round(
            0.28 + self.degradation_score * 0.22
            + self.fault_severity("cylinder_slowdown") * 0.42
            + random.uniform(0.0, 0.02),
            3,
        )
        self.memory["cycle_count"] += 1
        self.timer = 0.0
        self.memory["cycle_active"] = False
        self.inputs["rod_home"] = True
        self.inputs["rod_extended"] = False
        self.inputs["vacuum_ok"] = False
        self.outputs["vertical_axis_motor"] = False
        self.outputs["transfer_axis_motor"] = False
        self.outputs["vacuum_ejector"] = False
        self.process_state = "IDLE"

    def component_health(self) -> dict[str, float]:
        actuator = max(0.0, 1.0 - self.degradation_score - self.fault_severity("cylinder_slowdown"))
        vacuum = max(0.0, 1.0 - self.degradation_score * 0.65)
        return {
            "vacuum_head": round(vacuum, 3),
            "vertical_axis": round(actuator, 3),
            "transfer_axis": round(actuator, 3),
        }
