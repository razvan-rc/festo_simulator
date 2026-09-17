from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Container:
    container_id: int
    material: str
    color: str
    height_mm: float
    capacity_ml: float = 500.0
    volume_ml: float = 0.0
    cap_installed: bool = False
    quality_ok: bool = True
    defects: list[str] = field(default_factory=list)
    state: str = "EMPTY"
    station: str = "distributing"
    history: list[dict] = field(default_factory=list)

    @property
    def part_id(self) -> int:
        return self.container_id

    @property
    def bottle_id(self) -> int:
        return self.container_id

    def move_to(self, station: str, timestamp: float, result: str = "OK") -> None:
        self.history.append({
            "station": self.station,
            "next_station": station,
            "timestamp": timestamp,
            "result": result,
        })
        self.station = station


# Compatibility aliases for code and historical terminology.
Part = Container
Bottle = Container


class ProductionLine:
    def __init__(self) -> None:
        self.part_queue: list[Container] = []
        self.separated_queue: list[Container] = []
        self.filled_queue: list[Container] = []
        self.pnp_queue: list[Container] = []
        self.diverted_parts: list[Container] = []
        self.completed_parts: list[Container] = []
        self.completed_bottles = self.completed_parts
        self.next_container_id = 1
        self.total_produced = 0
        self.total_good = 0
        self.total_rejects = 0
        self.total_diverted = 0
        self.buffer_capacities = {
            "part_queue": 12,
            "separated_queue": 10,
            "filled_queue": 8,
            "pnp_queue": 8,
        }
        self.operational_state = "STARTUP"
        self.state_seconds: dict[str, float] = {}
        self.planned_seconds = 0.0
        self.available_seconds = 0.0

    def buffer_full(self, name: str) -> bool:
        return len(getattr(self, name)) >= self.buffer_capacities[name]

    def wip(self) -> dict[str, int]:
        counts = {
            "distributing_to_separating": len(self.part_queue),
            "separating_to_bottling": len(self.separated_queue),
            "bottling_to_pick_place": len(self.filled_queue),
            "pick_place_to_sorting": len(self.pnp_queue),
        }
        counts["total"] = sum(counts.values())
        return counts

    def update_operational_state(self, stations: list, dt: float) -> None:
        states = {station.state for station in stations}
        process_states = {station.process_state for station in stations}
        if "MAINTENANCE" in states:
            state = "PLANNED_STOP"
        else:
            self.planned_seconds += dt
            if states.intersection({"FAULT", "FAILURE"}):
                state = "FAULT"
            elif any(station.memory["cycle_active"] for station in stations):
                state = "RUNNING"
                self.available_seconds += dt
            elif "BLOCKED" in process_states:
                state = "BLOCKED"
                self.available_seconds += dt
            elif self.wip()["total"] == 0:
                state = "STARVED"
                self.available_seconds += dt
            else:
                state = "IDLE"
                self.available_seconds += dt
        self.operational_state = state
        self.state_seconds[state] = self.state_seconds.get(state, 0.0) + dt

    def payload(self) -> dict:
        total = self.total_good + self.total_rejects
        availability = self.available_seconds / self.planned_seconds if self.planned_seconds else 1.0
        return {
            "wip": self.wip(),
            "production": {
                "total": total,
                "good": self.total_good,
                "rejects": self.total_rejects,
                "diverted": self.total_diverted,
                "quality_rate": round(self.total_good / total, 3) if total else 1.0,
            },
            "operational": {
                "state": self.operational_state,
                "availability_pct": round(availability * 100.0, 2),
                "planned_seconds": round(self.planned_seconds, 2),
                "state_seconds": {key: round(value, 2) for key, value in self.state_seconds.items()},
            },
        }

    def create_container(self) -> Container:
        patterns = [
            ("metal", "silver", 25.0),
            ("plastic", "red", 20.0),
            ("plastic", "black", 30.0),
            ("plastic", "red", 25.0),
            ("metal", "silver", 30.0),
            ("plastic", "blue", 20.0),
        ]
        material, color, height = patterns[(self.next_container_id - 1) % len(patterns)]
        container = Container(self.next_container_id, material, color, height)
        self.next_container_id += 1
        return container

    def create_part(self) -> Container:
        return self.create_container()
