"""
SimulationMetrics — lightweight tracker for simulation runs.

Records per-tick data and computes aggregate statistics.
"""

from dataclasses import dataclass, field


@dataclass
class TickRecord:
    agent_name: str
    tick: int
    atoms_created: int
    duplicates_merged: int
    hit_rate: float


class SimulationMetrics:
    def __init__(self):
        self.timeline: list[dict] = []

    def record_tick(
        self,
        agent_name: str,
        tick: int,
        atoms_created: int,
        duplicates_merged: int,
        hit_rate: float,
    ) -> None:
        self.timeline.append({
            "agent": agent_name,
            "tick": tick,
            "atoms_created": atoms_created,
            "duplicates_merged": duplicates_merged,
            "hit_rate": round(hit_rate, 4),
        })

    def hit_rate_by_tick(self, agent_name: str | None = None) -> list[float]:
        """Return hit rates over time, optionally filtered to one agent."""
        records = [
            r for r in self.timeline
            if agent_name is None or r["agent"] == agent_name
        ]
        return [r["hit_rate"] for r in records]

    def atoms_created_total(self) -> int:
        return sum(r["atoms_created"] for r in self.timeline)

    def duplicates_merged_total(self) -> int:
        return sum(r["duplicates_merged"] for r in self.timeline)

    def avg_hit_rate(self, agent_name: str | None = None) -> float:
        rates = self.hit_rate_by_tick(agent_name)
        return sum(rates) / len(rates) if rates else 0.0

    def summary(self) -> dict:
        return {
            "total_ticks": len(self.timeline),
            "total_atoms_created": self.atoms_created_total(),
            "total_duplicates_merged": self.duplicates_merged_total(),
            "overall_avg_hit_rate": round(self.avg_hit_rate(), 4),
        }

    def print_report(self) -> None:
        """Print a human-readable summary to stdout."""
        s = self.summary()
        print("── Simulation Metrics ──────────────────────────────")
        print(f"  Total ticks          : {s['total_ticks']}")
        print(f"  Atoms created        : {s['total_atoms_created']}")
        print(f"  Duplicates merged    : {s['total_duplicates_merged']}")
        print(f"  Avg retrieval hit    : {s['overall_avg_hit_rate']:.1%}")

        # Per-agent breakdown
        agents = sorted({r["agent"] for r in self.timeline})
        print("\n  Per-agent hit rate (first vs last 5 ticks):")
        for name in agents:
            rates = self.hit_rate_by_tick(name)
            first5 = rates[:5]
            last5 = rates[-5:]
            avg_first = sum(first5) / len(first5) if first5 else 0.0
            avg_last = sum(last5) / len(last5) if last5 else 0.0
            trend = "↑" if avg_last > avg_first else ("↓" if avg_last < avg_first else "→")
            print(
                f"    {name:<30} first={avg_first:.1%}  last={avg_last:.1%}  {trend}"
            )
