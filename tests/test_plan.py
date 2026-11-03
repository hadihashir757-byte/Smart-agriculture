import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import irrigation_plan as ip


class FakeIo(ip.Io):
    def __init__(self, soils, tank=80, demo=True, refuse_at=None):
        self.soils, self.tank_v, self.demo, self.refuse_at = list(soils), tank, demo, refuse_at
        self.events, self.said = [], []

    def water(self, pump, seconds):
        self.events.append(("water", pump, seconds))
        if self.refuse_at is not None and len([e for e in self.events if e[0] == "water"]) == self.refuse_at:
            return {"accepted": False, "reason": "tank_low"}
        return {"accepted": True}

    def measure_soil(self):
        return self.soils.pop(0) if self.soils else None

    def tank(self):
        return self.tank_v

    def demo_on(self):
        return self.demo

    def say(self, text, **extra):
        self.said.append(text)

    def sleep(self, s):
        self.events.append(("sleep", s))


def plan(soil, rain=0.0, **kw):
    a = dict(zone_id=2, pump=2, soil=soil, rain_mm=rain, rain_detected=False, tank_percent=80)
    a.update(kw)
    return ip.make_plan(**a)


def test_dry_plan_is_3s_wait5_x3_to_80():
    p = plan(30)
    assert p["run"] and (p["pulse_s"], p["wait_s"], p["pulses"], p["target"]) == (3, 5, 3, 80)
    assert "three times" in p["text"] and "80%" in p["text"]


def test_rain_plan_is_5s_wait8_x2_to_40():
    p = plan(25, rain=8)
    assert p["run"] and (p["pulse_s"], p["wait_s"], p["pulses"], p["target"]) == (5, 8, 2, 40)
    assert "Rain is expected" in p["text"] and "twice" in p["text"] and "40%" in p["text"]


def test_rain_and_soil_already_40_is_skipped():
    p = plan(45, rain=8)
    assert not p["run"] and "No watering needed" in p["text"]


def test_no_plan_when_raining_tank_low_or_no_reading():
    assert not plan(30, rain_detected=True)["run"]
    assert not plan(30, tank_percent=10)["run"]
    assert not plan(30, tank_percent=None)["run"]
    assert not plan(None)["run"]


def test_pulses_then_reaches_target_after_pulses():
    io = FakeIo([85])
    r = ip.run_plan(plan(30), io)
    assert r["outcome"] == "reached"
    waters = [e for e in io.events if e[0] == "water"]
    assert waters == [("water", 2, 3)] * 3            # exactly three 3 s pulses, no extra burst


def test_continues_until_target_and_stops_there():
    io = FakeIo([50, 62, 83])
    r = ip.run_plan(plan(30), io)
    assert r["outcome"] == "reached" and r["soil"] == 83
    waters = [e for e in io.events if e[0] == "water"]
    assert waters == [("water", 2, 3)] * 3 + [("water", 2, 5)] * 2


def test_rain_plan_stops_at_40():
    io = FakeIo([36, 41])
    r = ip.run_plan(plan(20, rain=9), io)
    assert r["outcome"] == "reached"
    waters = [e for e in io.events if e[0] == "water"]
    assert waters == [("water", 2, 5)] * 2 + [("water", 2, 5)]      # 2 pulses of 5 s + 1 burst
    assert ("sleep", 8) in io.events


def test_cap_on_extra_bursts():
    io = FakeIo([50] * 30)
    r = ip.run_plan(plan(30), io)
    assert r["outcome"] == "gave_up"
    assert len([e for e in io.events if e == ("water", 2, 5)]) == ip.MAX_EXTRA_BURSTS


def test_no_soil_reading_stops():
    r = ip.run_plan(plan(30), FakeIo([]))
    assert r["outcome"] == "no_reading"


def test_tank_low_mid_plan_stops():
    r = ip.run_plan(plan(30), FakeIo([50, 55], tank=10))
    assert r["outcome"] == "tank_low"


def test_station_refusal_stops():
    io = FakeIo([50], refuse_at=2)
    r = ip.run_plan(plan(30), io)
    assert r["outcome"] == "refused" and len([e for e in io.events if e[0] == "water"]) == 2


def test_demo_switched_off_stops_before_any_water():
    io = FakeIo([50], demo=False)
    r = ip.run_plan(plan(30), io)
    assert r["outcome"] == "stopped" and not [e for e in io.events if e[0] == "water"]
