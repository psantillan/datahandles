"""A simulation whose states never change: one state forks into runs with different inputs."""
from datahandles import Client, Runtime


def test_two_forks_of_one_state_differ_and_the_state_they_start_from_stays(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    start = paul.register("twin", adapter="sim", source={"temperature": 18.0, "outside": 10.0})
    before = start.invoke("sim.state")
    heated = start.invoke("sim.step", {"steps": 30, "heater": True})
    cold = start.invoke("sim.step", {"steps": 30, "heater": False})
    hot_t, cold_t = heated.invoke("sim.state")["temperature"], cold.invoke("sim.state")["temperature"]
    assert hot_t > before["temperature"] > cold_t
    assert start.invoke("sim.state") == before
    # The same input on a new fork gives an equal state.
    again = start.invoke("sim.step", {"steps": 30, "heater": True})
    assert again.id != heated.id and again.invoke("sim.state") == heated.invoke("sim.state")
    assert heated.describe()["parents"] == [start.id] and heated.describe()["consistency"] == "snapshot"


def test_a_fork_can_start_from_a_measured_temperature(tmp_path):
    paul = Client(Runtime(tmp_path / "state"), "paul")
    start = paul.register("twin", adapter="sim", source={})
    measured = start.invoke("sim.set", {"temperature": 21.5})
    assert measured.invoke("sim.state")["temperature"] == 21.5 and start.invoke("sim.state")["temperature"] == 18.0
    assert measured.invoke("sim.step", {"steps": 1, "heater": False}).invoke("sim.state")["step"] == 1
