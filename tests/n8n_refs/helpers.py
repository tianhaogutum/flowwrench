from copy import deepcopy
from pathlib import Path

TEST_TMP = Path(__file__).resolve().parent / ".cache" / "tests"
TEST_TMP.mkdir(parents=True, exist_ok=True)


def node(name, value=None, kind="set", **extra):
    parameters = {} if value is None else {"text": value}
    return {"id": "synthetic-" + name, "name": name, "type": "n8n-nodes-base." + kind,
            "typeVersion": 1, "position": [0, 0], "parameters": parameters, **extra}


def workflow(nodes, links=()):
    connections = {}
    for link in links:
        source, target, *ports = link
        out, incoming = ports if ports else (0, 0)
        outputs = connections.setdefault(source, {"main": []})["main"]
        while len(outputs) <= out:
            outputs.append([])
        outputs[out].append({"node": target, "type": "main", "index": incoming})
    return {"name": "Synthetic redistributable test", "nodes": deepcopy(nodes), "connections": connections,
            "active": False, "settings": {"executionOrder": "v1"}, "pinData": {}}
