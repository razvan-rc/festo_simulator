from __future__ import annotations

import random

from stations.base import StationBase
from simulation.models import ProductionLine


class SeparatingStation(StationBase):
    """Detects the incoming container and diverts only unsupported colors."""

    def __init__(self, line: ProductionLine):
        super().__init__("Separating")
        self.line = line
        self.timer = 0.0
        self.base_process = 1.0
        self.accepted_colors = {"silver", "red", "black"}
        self.inputs = {
            "entry_sensor": False,
            "color_sensor": False,
            "main_exit_sensor": False,
            "diverted_lane_sensor": False,
            "diverter_home": True,
            "diverter_extended": False,
        }
        self.outputs = {
            "main_conveyor_motor": False,
            "diverted_conveyor_motor": False,
            "diverter_motor": False,
        }
        self.baseline = {
            "cycle_time_s": self.base_process,
            "color_confidence_pct": 99.2,
        }

    def step(self, dt: float, now: float) -> None:
        self.inputs["entry_sensor"] = bool(self.line.part_queue)
        self.inputs["color_sensor"] = False
        self.inputs["main_exit_sensor"] = False
        self.inputs["diverted_lane_sensor"] = False
        self.outputs["main_conveyor_motor"] = bool(self.line.part_queue)
        self.outputs["diverted_conveyor_motor"] = False
        self.outputs["diverter_motor"] = False

        if not self.line.part_queue:
            self.process_state = "IDLE"
            self.memory["cycle_active"] = False
            self.timer = 0.0
            return

        front = self.line.part_queue[0]
        should_divert = front.color not in self.accepted_colors
        if not should_divert and self.line.buffer_full("separated_queue"):
            self.process_state = "BLOCKED"
            self.memory["cycle_active"] = False
            self.outputs["main_conveyor_motor"] = False
            self.timer = 0.0
            return

        self.timer += dt
        target = self.base_process * (
            1.0 + self.degradation_score * 0.45 + self.fault_severity("sensor_drift") * 0.55
        )
        self.process_state = "COLOR_INSPECTION"
        self.memory["cycle_active"] = True
        self.inputs["color_sensor"] = self.timer >= target * 0.35
        if self.timer < target:
            return

        container = self.line.part_queue.pop(0)
        misread_probability = 0.002 + self.fault_severity("sensor_drift") ** 2 * 0.45
        misread = random.random() < misread_probability
        detected_color = container.color
        if misread:
            detected_color = "blue" if container.color in self.accepted_colors else "black"
            self.emit("COLOR_SENSOR_MISREAD", component="color_sensor")

        detected_accepted = detected_color in self.accepted_colors
        confidence = max(
            45.0,
            99.2 - self.degradation_score * 8.0
            - self.fault_severity("sensor_drift") * 38.0
            + random.gauss(0.0, 0.4),
        )
        self.measurements["detected_color"] = detected_color
        self.measurements["color_confidence_pct"] = round(confidence, 2)
        self.measurements["cycle_time_s"] = round(self.timer, 3)
        self.measurements["diverted"] = not detected_accepted

        if detected_accepted:
            if container.color not in self.accepted_colors:
                container.quality_ok = False
                container.defects.append("SEPARATION_MISROUTE")
            container.move_to("bottling", now)
            self.line.separated_queue.append(container)
            self.inputs["main_exit_sensor"] = True
        else:
            self.process_state = "DIVERTING"
            self.outputs["diverter_motor"] = True
            self.outputs["diverted_conveyor_motor"] = True
            self.inputs["diverter_home"] = False
            self.inputs["diverter_extended"] = True
            self.inputs["diverted_lane_sensor"] = True
            if container.color in self.accepted_colors:
                container.quality_ok = False
                container.defects.append("SEPARATION_MISROUTE")
                self.line.total_rejects += 1
            else:
                self.line.total_diverted += 1
            container.move_to("separated_lane", now, "DIVERTED")
            if not container.quality_ok:
                self.line.total_produced += 1
                self.line.completed_parts.append(container)
            self.line.diverted_parts.append(container)

        self.memory["cycle_count"] += 1
        self.memory["cycle_active"] = False
        self.timer = 0.0
        self.inputs["diverter_home"] = True
        self.inputs["diverter_extended"] = False
        self.process_state = "IDLE"

    def component_health(self) -> dict[str, float]:
        sensor = max(0.0, 1.0 - self.degradation_score - self.fault_severity("sensor_drift"))
        diverter = max(0.0, 1.0 - self.degradation_score * 0.8)
        return {
            "entry_sensor": round(self.health_score, 3),
            "color_sensor": round(sensor, 3),
            "diverter_motor": round(diverter, 3),
            "conveyors": round(self.health_score, 3),
        }
