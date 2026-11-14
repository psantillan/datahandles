"""Pull four public APIs through handles: Open-Meteo (weather), USGS (earthquakes), GitHub, and NASA.

The first three need no key. NASA needs one: the adapter reads it from the environment variable
NASA_API_KEY and sends it in a header. Without that variable, this example uses the demo key that
NASA publishes. Each API is one handle with a fixed base URL. A live answer comes from
api.get. api.fetch stores a response as a handle that never changes, and a caller reads it in
parts, so a 30 KB earthquake feed never goes to the caller as a whole.

Run: .venv/bin/python examples/public_apis.py
"""
import os
import tempfile

from datahandles import connect


def main() -> None:
    client = connect(tempfile.mkdtemp(prefix="datahandles-apis-"), principal="analyst")
    weather = client.register("weather", adapter="api", source={"base_url": "https://api.open-meteo.com/v1", "timeout_ms": 15000})
    quakes = client.register("quakes", adapter="api", source={"base_url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary", "timeout_ms": 15000})
    github = client.register("github", adapter="api", source={"base_url": "https://api.github.com", "timeout_ms": 15000})

    # A small live answer: the current weather in San Diego.
    now = weather.invoke("api.get", {"path": "/forecast", "query": {
        "latitude": 32.72, "longitude": -117.16, "current": "temperature_2m,wind_speed_10m", "timezone": "America/Los_Angeles"}})
    current = now["json"]["current"]
    print(f"San Diego at {current['time']}: {current['temperature_2m']} °C, wind {current['wind_speed_10m']} km/h")

    # A larger answer: store it, then read only the parts that the question needs.
    feed = quakes.invoke("api.fetch", {"path": "/2.5_day.geojson"})
    status = feed.invoke("response.status")
    count = feed.invoke("response.keys", {"path": ["features"]})["length"]
    latest = feed.invoke("response.get", {"path": ["features", 0, "properties"]})
    print(f"USGS: {count} earthquakes of magnitude 2.5 or more in the last day ({status['bytes']:,} bytes stored, none sent here)")
    print(f"  the most recent: magnitude {latest['mag']}, {latest['place']}")

    repo = github.invoke("api.fetch", {"path": "/repos/psantillan/datahandles"})
    print("GitHub: psantillan/datahandles has", repo.invoke("response.get", {"path": ["stargazers_count"]}), "stars,",
          repo.invoke("response.get", {"path": ["open_issues_count"]}), "open issues, default branch",
          repo.invoke("response.get", {"path": ["default_branch"]}))

    # An API with a key. The key stays in this process: no handle, registry row, or answer holds it.
    os.environ.setdefault("NASA_API_KEY", "DEMO_KEY")
    nasa = client.register("nasa", adapter="api", source={"base_url": "https://api.nasa.gov", "timeout_ms": 20000,
                                                          "token_env": "NASA_API_KEY", "token_header": "X-Api-Key"})
    day = nasa.invoke("api.get", {"path": "/neo/rest/v1/feed", "query": {"start_date": "2024-01-01", "end_date": "2024-01-01"}})
    if day["status"] == 200:
        print(f"NASA: {day['json']['element_count']} asteroids passed near Earth on 2024-01-01, for example",
              day["json"]["near_earth_objects"]["2024-01-01"][0]["name"])
    else:
        print("NASA: status", day["status"], "(the demo key has a low rate limit; set NASA_API_KEY to your own key)")

    # A graph over the stored feed replays: the second run reuses each node.
    graph = {"id": "g", "execution": {"strategy": "lazy", "consistency": "latest"},
             "nodes": [{"id": "feed", "kind": "resource", "reference": feed.id},
                       {"id": "count", "kind": "operation", "operation": {"capability": "response.keys", "input": {"path": ["features"]}}},
                       {"id": "title", "kind": "operation", "operation": {"capability": "response.get", "input": {"path": ["metadata", "title"]}}}],
             "edges": [{"from": "feed", "to": "count"}, {"from": "feed", "to": "title"}]}
    first, again = client.submit(graph), client.submit(graph)
    print("Graph over the stored feed:", first["results"]["title"]["output"], "-",
          "second run reused every node:", all(r["metadata"].get("reused") for r in again["results"].values()))


if __name__ == "__main__":
    main()
