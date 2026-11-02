"""Real core server, exclusively synthetic in-memory Canvas HTTP responses."""
import copy
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
from canvas_connector.server import create_stdio_server
from canvas_py import Canvas
from canvas_py._config import CanvasConfig

folder, mode = Path(sys.argv[1]), sys.argv[2]
folder.mkdir(parents=True, exist_ok=True)
(folder / "child.pid").write_text(str(os.getpid()))
(folder / "environment.json").write_text(json.dumps(sorted(os.environ)))


def deny_network(*args, **kwargs):
    raise AssertionError("Synthetic child prohibits external network access")


socket.getaddrinfo = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
httpx.HTTPTransport.handle_request = deny_network
positions_file = folder / "positions.json"
positions = (json.loads(positions_file.read_text()) if positions_file.exists()
             else {"course_2": "-1", "course_1": "0", "group_19": " +7 "})


def control():
    path = folder / "controls.json"
    return json.loads(path.read_text()) if path.exists() else {}


def handler(request):
    assert request.url.host == "school.example"
    assert request.headers["authorization"] == "Bearer synthetic-token"
    with (folder / "requests.jsonl").open("a") as stream:
        stream.write(json.dumps({"method": request.method, "path": request.url.path}) + "\n")
    options = control()
    if options.get("denied"):
        return httpx.Response(403, json={"message": "synthetic-upstream-private-secret"})
    if request.method == "PUT":
        assert request.url.path == "/api/v1/users/self/dashboard_positions"
        positions.update(json.loads(request.content)["dashboard_positions"])
        positions_file.write_text(json.dumps(positions))
        (folder / "mutation_applied").write_text("1")
        if options.get("pause_after_write"):
            time.sleep(options["pause_after_write"])
        if options.get("lose_response"):
            raise httpx.ReadError("synthetic response lost after apply", request=request)
        return httpx.Response(200, json={"dashboard_positions": copy.deepcopy(positions)})
    assert request.method == "GET"
    if request.url.path == "/api/v1/users/self/profile":
        return httpx.Response(200, json={"id": 7, "k5_user": False,
            "name": "synthetic-student-" * (10000 if options.get("large_name") else 1)})
    if request.url.path == "/api/v1/users/self/dashboard_positions":
        return httpx.Response(200, json={"dashboard_positions": positions})
    if request.url.path == "/api/v1/dashboard/dashboard_cards":
        cards = [{"id": i, "assetString": f"course_{i}", "shortName": f"Course {i}",
                  "longName": f"Long course {i}", "originalName": f"Course {i}",
                  "courseCode": f"C{i}", "enrollmentType": "StudentEnrollment",
                  "enrollmentState": "active", "isFavorited": False,
                  "isK5Subject": False, "isHomeroom": False, "published": True,
                  "href": f"/courses/{i}", "image": "https://untrusted.invalid/never-fetch",
                  "position": int(positions[f"course_{i}"]) if f"course_{i}" in positions else None}
                 for i in [2, 1, 3]]
        return httpx.Response(200, json=sorted(cards,
            key=lambda row: 1001 if row["position"] is None else row["position"]))
    raise AssertionError(request.url.path)


with (httpx.Client(transport=httpx.MockTransport(handler)) as http,
      Canvas(CanvasConfig("https://school.example", "synthetic-token"), http_client=http) as canvas):
    create_stdio_server(canvas=canvas, allow_writes=mode == "writes").run(transport="stdio")

