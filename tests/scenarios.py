"""30 end-to-end scenarios for the AI watering brain (normal mode + demo mode).

Each scenario feeds realistic readings through rover_routes.evaluate_zone (the same code the real agent
uses), with fake weather / ThingsBoard / database, and checks the DECISION and the WHY text. Demo
scenarios also run the pump plan against a fake station and check exactly which pumps were switched.

  python tests/scenarios.py          -> prints a report table
  pytest tests/test_scenarios.py     -> asserts every scenario
"""
import os
import sys
import time

os.environ.setdefault("DEV_INSECURE_KEY", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import db  # noqa: E402
import irrigation_plan as ip  # noqa: E402
import rover_routes as rr  # noqa: E402
import thingsboard  # noqa: E402
import water_amount  # noqa: E402
from test_plan import FakeIo  # noqa: E402


class HoldIo(FakeIo):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.holds = []

    def hold(self, on):
        self.holds.append(on)


AT = {"rover_link_ok": True, "rover_at_zone": True, "rover_zone": 2}


def S(name, mode="normal", soil=25, tank=80, rain=0.0, raining=False, rover=None, crop="tomato", zone=2,
      age=10, last_water_ago=None, health=None, temp=26.0, hum=60.0,
      expect="water", why=(), run=None, outcome=None, waters=None, soils=None, refuse=None, tank_after=None,
      report_has=None, stage=None, total=None, turns=None, mode_run=None):
    return dict(locals())


SCENARIOS = [
    # ---------------- normal mode ----------------
    S("N01 dry soil, tank ok, no rain", soil=25, expect="water", why=("25",)),
    S("N02 soil already fine (55%)", soil=55, expect="skip", why=("55",)),
    S("N03 soil too wet (85%)", soil=85, expect="skip", why=("85",)),
    S("N04 critical dry + rain coming -> water anyway", soil=15, rain=8, expect="water", why=("critical",)),
    S("N05 dry 30% + rain coming -> short top-up", soil=30, rain=8, expect="water", why=("rain",)),
    S("N06 soil 45% + rain coming -> wait for rain", soil=45, rain=8, expect="skip", why=("rain",)),
    S("N07 tank low (10%)", tank=10, expect="skip", why=("tank",)),
    S("N08 tank level unknown", tank=None, expect="skip", why=("tank",)),
    S("N09 raining right now", raining=True, expect="skip", why=("rain",)),
    S("N10 soil reading 20 min old", age=1200, expect="skip", why=("old",)),
    S("N11 watered 10 min ago (cooldown)", last_water_ago=600, expect="skip", why=("min",)),
    S("N12 hot day 36 C, dry soil -> water", soil=25, temp=36, expect="water", why=("25",)),
    S("N13 fungal disease on the plant, soil 25%", soil=25, health={"label": "Tomato___Late_blight", "confidence": 0.9},
      expect="skip", why=("fung", "blight", "disease")),
    S("N14 fungal disease but soil critical (15%)", soil=15, health={"label": "Tomato___Late_blight", "confidence": 0.9},
      expect="water", why=("critical", "disease", "fung")),
    S("N15 plantain at 45% (mature plantain starts below 50%)", soil=45, crop="plantain", expect="water", why=("45",)),
    S("N16 zone not mapped to a pump", zone=9, expect="skip", why=("not mapped",)),
    S("N17 rover not at the zone -> skip, no pump", soil=25,
      rover={"rover_link_ok": True, "rover_at_zone": False, "rover_zone": 1}, expect="skip", run=False, why=("rover", "No pump")),
    S("N18 water-stressed plant, dry soil", soil=25, health={"label": "Tomato___water_stress", "confidence": 0.9},
      expect="water", why=("25",)),
    S("N19 rover link lost -> skip", soil=25,
      rover={"rover_link_ok": False, "rover_at_zone": True, "rover_zone": 2}, expect="skip", run=False, why=("link", "No pump")),
    # ---- normal mode: how many seconds / turns ----
    S("A01 tomato mature 25%, mild day -> 35 s in 2 turns", soil=25, expect="water", total=35, turns=[18, 17], why=("35 s", "2 turns")),
    S("A02 tomato seedling 25% -> shorter (x0.7), 2 turns", soil=25, stage="seedling", expect="water", total=28, turns=[14, 14], why=("seedling",)),
    S("A03 plantain mature 40% -> thirstier crop (x1.25)", soil=40, crop="plantain", expect="water", total=38, turns=[19, 19], why=("plantain", "1.25")),
    S("A04 pepper mature 20% -> x0.85", soil=22, crop="pepper", expect="water", total=32, turns=[16, 16], why=("pepper", "0.85")),
    S("A04b mango mature 22% -> target 50%, x1.1", soil=22, crop="mango", expect="water", total=31, turns=[16, 15], why=("mango", "1.1")),
    S("A04c mango mature 30% -> above its start point (25%), skip", soil=30, crop="mango", expect="skip", run=False, why=("30",)),
    S("A05 hot day 35 C -> x1.2", soil=25, temp=35, expect="water", total=42, turns=[14, 14, 14], why=("hot day", "1.2")),
    S("A06 cool humid day -> less water", soil=25, temp=15, hum=90, expect="water", total=27, turns=[14, 13], why=("cool day", "humid air")),
    S("A07 hot and dry air -> x1.2 x1.1", soil=25, temp=34, hum=25, expect="water", total=46, turns=[16, 15, 15], why=("dry air",)),
    S("A08 completely dry 0% -> 60 s (the maximum) in 3 turns", soil=0, expect="water", total=60, turns=[20, 20, 20], why=("60 s", "3 turns")),
    S("A09 rain coming, soil 30% -> short top-up in 1 turn", soil=30, rain=8, expect="water", total=15, turns=[15], why=("short top-up", "1 turn")),
    S("A10 water-stressed plant -> +5 s", soil=25, health={"label": "Tomato___water_stress", "confidence": 0.9},
      expect="water", total=40, turns=[20, 20], why=("water stress",)),
    S("A11 fungal disease but soil critical -> gentle", soil=15, health={"label": "Tomato___Late_blight", "confidence": 0.9},
      expect="water", total=15, turns=[15], why=("disease", "gentle", "fung")),
    # ---- normal mode: running the turns ----
    S("T01 two turns, soil still low after turn 1", soil=25, expect="water", mode_run="turns", soils=[45], outcome="done", waters=[18, 17], why=("turns",)),
    S("T02 soil reaches target after turn 1 -> stops early", soil=25, expect="water", mode_run="turns", soils=[62], outcome="reached", waters=[18], why=("turns",)),
    S("T03 tank runs low between turns -> stops", soil=10, expect="water", mode_run="turns", soils=[30], tank_after=10, outcome="tank_low", waters=[17], why=("turns",)),
    S("T04 station refuses turn 1 (rover moved)", soil=25, expect="water", mode_run="turns", refuse=1, outcome="refused", waters=[18], why=("turns",)),
    # ---------------- demo mode: plan ----------------
    S("D01 demo, dry 30%, no rain -> 3 s x3 then up to 80%", mode="demo", soil=30, expect="water", run=True,
      why=("3 s", "80%", "three times")),
    S("D02 demo, 30% + rain -> 5 s/8 s x2, only up to 40%", mode="demo", soil=30, rain=8, expect="water", run=True,
      why=("rain", "40%", "twice", "8 s")),
    S("D03 demo, rain coming and soil 45% -> no water", mode="demo", soil=45, rain=8, expect="skip", run=False, why=("40%", "No watering")),
    S("D04 demo, soil already 85%", mode="demo", soil=85, expect="skip", run=False, why=("85%",)),
    S("D05 demo, rover NOT at the zone -> no pump", mode="demo", soil=30,
      rover={"rover_link_ok": True, "rover_at_zone": False, "rover_zone": 1}, expect="skip", run=False, why=("rover", "No pump")),
    S("D06 demo, rover at a different zone -> no pump", mode="demo", soil=30,
      rover={"rover_link_ok": True, "rover_at_zone": True, "rover_zone": 1}, expect="skip", run=False, why=("rover", "No pump")),
    S("D07 demo, rover link lost -> no pump", mode="demo", soil=30,
      rover={"rover_link_ok": False, "rover_at_zone": True, "rover_zone": 2}, expect="skip", run=False, why=("link", "No pump")),
    S("D08 demo, tank low (10%) -> no pump", mode="demo", soil=30, tank=10, expect="skip", run=False, why=("tank",)),
    S("D09 demo, raining now -> no pump", mode="demo", soil=30, raining=True, expect="skip", run=False, why=("raining",)),
    S("D10 demo, tank level unknown -> no pump", mode="demo", soil=30, tank=None, expect="skip", run=False, why=("tank",)),
    # ---------------- demo mode: running the plan ----------------
    S("R01 plan reaches 80% after the pulses", mode="demo", soil=30, expect="water", run=True, why=("pump",),
      soils=[84], outcome="reached", waters=[3, 3, 3]),
    S("R02 plan needs two extra bursts", mode="demo", soil=30, expect="water", run=True, why=("pump",),
      soils=[50, 66, 82], outcome="reached", waters=[3, 3, 3, 5, 5]),
    S("R03 rain plan stops at 40%", mode="demo", soil=25, rain=9, expect="water", run=True, why=("pump",),
      soils=[33, 41], outcome="reached", waters=[5, 5, 5]),
    S("R04 soil never rises -> gives up after 10 bursts", mode="demo", soil=30, expect="water", run=True, why=("pump",),
      soils=[50] * 30, outcome="gave_up", waters=[3, 3, 3] + [5] * 10),
    S("R05 tank runs low during the plan", mode="demo", soil=30, expect="water", run=True, why=("pump",),
      soils=[50, 55], tank_after=10, outcome="tank_low", waters=[3, 3, 3]),
    S("R06 station refuses (rover left the zone)", mode="demo", soil=30, expect="water", run=True, why=("pump",),
      refuse=2, outcome="refused", waters=[3, 3]),
]


def run(sc, monkeypatch=None):
    """Runs one scenario. Returns a dict with the decision, the full report text and what the pumps did."""
    pushed = {}
    st = {"tank_level_percent": sc["tank"], "demo_mode": sc["mode"] == "demo"}
    st.update(sc["rover"] or AT)
    if sc["tank"] is None:
        st.pop("tank_level_percent")
    health = None
    if sc["health"] or sc["stage"]:
        h = sc["health"] or {"label": "Tomato___healthy", "confidence": 0.9}
        health = {"predicted_class": h["label"], "confidence": h["confidence"], "growth_stage": sc["stage"] or "mature"}
    patches = [
        (rr, "get_weather_forecast", lambda: {"rain_mm": sc["rain"]}),
        (rr.thingsboard, "configured", lambda: True),
        (rr.thingsboard, "latest_telemetry", lambda dev, keys: {k: v for k, v in st.items() if k in keys}),
        (rr.thingsboard, "push_ai_telemetry", lambda d: pushed.update(d)),
        (rr.thingsboard, "send_rpc", lambda *a, **k: {"accepted": True}),
        (rr.db, "latest_accepted_plant_health", lambda max_age_s=None, zone_id=None: health),
        (rr.db, "last_watering_ts", lambda z: (time.time() - sc["last_water_ago"]) if sc["last_water_ago"] else None),
        (rr.db, "insert_watering_decision", lambda *a, **k: 1),
        (rr.db, "update_watering_decision", lambda *a, **k: None),
        (rr, "AUTO_WATER", False),
    ]
    old = [(o, n, getattr(o, n)) for o, n, _ in patches]
    for o, n, v in patches:
        setattr(o, n, v)
    try:
        readings = {"soil_moisture_percent": sc["soil"], "temperature_c": sc["temp"], "humidity_percent": sc["hum"],
                    "light_percent": 70.0, "rain_detected": sc["raining"], "reading_ts": time.time() - sc["age"]}
        res = rr.evaluate_zone(sc["zone"], readings, sc["crop"])
    finally:
        for o, n, v in old:
            setattr(o, n, v)
    out = {"decision": res["decision"], "report": pushed.get("ai_report", ""), "reason": res["reason"],
           "seconds": res["seconds"], "plan": res.get("plan"), "io": None, "result": None}
    if res.get("plan") and sc["soils"] is not None or (res.get("plan") and sc["refuse"]):
        io = HoldIo(sc["soils"] or [], tank=80, refuse_at=sc["refuse"])
        if sc["tank_after"] is not None:
            orig = io.measure_soil
            def measure():
                io.tank_v = sc["tank_after"]
                return orig()
            io.measure_soil = measure
        out["result"] = (water_amount.run_turns if res["plan"].get("kind") == "turns" else ip.run_plan)(res["plan"], io)
        out["io"] = io
    return out


def check(sc, out):
    """Returns a list of problems (empty = pass)."""
    bad = []
    if out["decision"] != sc["expect"]:
        bad.append(f"decision {out['decision']} (wanted {sc['expect']})")
    text = (out["report"] + " " + out["reason"]).lower()
    if not any(w.lower() in text for w in sc["why"]):
        bad.append(f"reason does not mention any of {sc['why']}")
    if sc["report_has"] and sc["report_has"].lower() not in text:
        bad.append(f"report lacks '{sc['report_has']}'")
    if "CHECKED:" not in out["report"] or "WHY:" not in out["report"]:
        bad.append("report has no CHECKED/WHY")
    if sc["run"] is True and not out["plan"]:
        bad.append("no plan")
    if sc["run"] is False and out["plan"]:
        bad.append("a pump plan was created but should not exist")
    if sc["total"] is not None:
        plan = out["plan"] or {}
        if plan.get("total_s") != sc["total"] or plan.get("turns") != sc["turns"]:
            bad.append(f"amount {plan.get('total_s')} s in turns {plan.get('turns')} (wanted {sc['total']} s, {sc['turns']})")
    if sc["outcome"]:
        if not out["result"] or out["result"]["outcome"] != sc["outcome"]:
            bad.append(f"outcome {out['result'] and out['result']['outcome']} (wanted {sc['outcome']})")
        else:
            secs = [e[2] for e in out["io"].events if e[0] == "water"]
            if secs != sc["waters"]:
                bad.append(f"pump runs {secs} (wanted {sc['waters']})")
            h = out["io"].holds
            if not h or h[-1] is not False or h[0] is not True or False in h[:-1]:
                bad.append(f"rover hold calls {h}")
    return bad


def main():
    rows, fails = [], 0
    for sc in SCENARIOS:
        out = run(sc)
        bad = check(sc, out)
        fails += bool(bad)
        why = out["plan"]["text"] if out["plan"] else out["reason"]
        extra = ""
        if out["result"]:
            secs = [e[2] for e in out["io"].events if e[0] == "water"]
            extra = f" -> {out['result']['outcome']}; pump runs {secs}"
        rows.append((sc["name"], out["decision"].upper(), why + extra, "PASS" if not bad else "FAIL: " + "; ".join(bad)))
    print("| # | Scenario | Decision | Reasoning the AI gives | Result |\n|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        print(f"| {i} | {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
    print(f"\n{len(rows) - fails}/{len(rows)} passed")
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
