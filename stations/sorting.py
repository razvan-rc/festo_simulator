from __future__ import annotations

import random

from stations.base import StationBase
from simulation.models import ProductionLine


class SortingStation(StationBase):
    """Final module: detects material/color and routes the container to one of three lanes."""

    def __init__(self, line: ProductionLine):
        super().__init__("Sorting")
        self.line = line
        self.timer = 0.0
        self.base_cycle = 0.95
        self.inputs = {
            "entry_sensor": False,
            "material_sensor": False,
            "color_sensor": False,
            "lane_1_sensor": False,
            "lane_2_sensor": False,
            "lane_3_sensor": False,
            "stopper_1_home": True,
            "stopper_2_home": True,
        }
        self.outputs = {
            "conveyor_motor": False,
            "stopper_1": False,
            "stopper_2": False,
        }
        self.baseline = {
            "sorting_time_s": self.base_cycle,
            "classification_confidence_pct": 99.0,
            "classification_correct": True,
        }

    @staticmethod
    def _expected_destination(material: str, color: str) -> str:
        if material == "metal":
            return "lane_1"
        if color == "red":
            return "lane_2"
        return "lane_3"

    def step(self, dt: float, now: float) -> None:
        self.inputs["entry_sensor"] = bool(self.line.pnp_queue)
        self.inputs["material_sensor"] = False
        self.inputs["color_sensor"] = False
        self.inputs["lane_1_sensor"] = False
        self.inputs["lane_2_sensor"] = False
        self.inputs["lane_3_sensor"] = False
        self.outputs["conveyor_motor"] = bool(self.line.pnp_queue)
        self.outputs["stopper_1"] = False
        self.outputs["stopper_2"] = False

        if not self.line.pnp_queue:
            self.process_state = "IDLE"
            self.memory["cycle_active"] = False
            self.timer = 0.0
            return

        self.timer += dt
        target = self.base_cycle * (
            1.0 + self.degradation_score * 0.55 + self.fault_severity("sensor_misread") * 0.45
        )
        self.process_state = "MATERIAL_DETECTION"
        self.memory["cycle_active"] = True
        self.inputs["material_sensor"] = self.timer >= target * 0.3
        self.inputs["color_sensor"] = self.timer >= target * 0.45
        if self.timer < target:
            return

        container = self.line.pnp_queue.pop(0)
        detected_material = container.material
        detected_color = container.color
        sensor_error = random.random() < (0.002 + self.fault_severity("sensor_misread") ** 2 * 0.5)
        if sensor_error:
            if container.material == "metal":
                detected_material, detected_color = "plastic", "red"
            else:
                detected_material, detected_color = "metal", "silver"
            self.emit("SORTING_SENSOR_MISREAD", component="material_sensor")

        destination = self._expected_destination(detected_material, detected_color)
        expected = self._expected_destination(container.material, container.color)
        correct = destination == expected
        self.process_state = "ROUTING"
        if destination == "lane_1":
            self.outputs["stopper_1"] = True
            self.inputs["stopper_1_home"] = False
        elif destination == "lane_2":
            self.outputs["stopper_2"] = True
            self.inputs["stopper_2_home"] = False
        self.inputs[f"{destination}_sensor"] = True

        confidence = max(
            40.0,
            99.0 - self.degradation_score * 9.0
            - self.fault_severity("sensor_misread") * 42.0
            + random.gauss(0.0, 0.5),
        )
        self.measurements["sorting_time_s"] = round(self.timer, 3)
        self.measurements["detected_material"] = detected_material
        self.measurements["detected_color"] = detected_color
        self.measurements["classification_confidence_pct"] = round(confidence, 2)
        self.measurements["classification_correct"] = correct
        self.measurements["destination"] = destination

        if not correct:
            container.quality_ok = False
            container.defects.append("SORTING_ERROR")
        container.move_to(destination, now, "OK" if correct else "SORTING_ERROR")
        container.state = "COMPLETED" if container.quality_ok else "REJECTED"
        self.line.total_produced += 1
        if container.quality_ok:
            self.line.total_good += 1
        else:
            self.line.total_rejects += 1
        self.line.completed_parts.append(container)

        self.memory["cycle_count"] += 1
        self.memory["cycle_active"] = False
        self.timer = 0.0
        self.outputs["conveyor_motor"] = False
        self.inputs["stopper_1_home"] = True
        self.inputs["stopper_2_home"] = True
        self.process_state = "IDLE"

    def component_health(self) -> dict[str, float]:
        sensor = max(0.0, 1.0 - self.degradation_score - self.fault_severity("sensor_misread"))
        stopper = max(0.0, 1.0 - self.degradation_score * 0.82)
        return {
            "material_sensor": round(sensor, 3),
            "color_sensor": round(sensor, 3),
            "sorting_stoppers": round(stopper, 3),
            "conveyor": round(self.health_score, 3),
        }
