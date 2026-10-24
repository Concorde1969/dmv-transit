# Scheduling engines

Templates for plugging your own trip scheduler into the assistant. Both examples are
fully working adapters that **compute no real schedules** — they show the request you
receive and the reply shape you must return. Replace their `plan` functions with your
engine.

| Folder | Language | How to enable (in `.env`) |
| --- | --- | --- |
| `example-js/` | Node.js 18+ | `PLANNER_ENGINE=process` and `PLANNER_COMMAND=node engines/example-js/engine.js` |
| `example_python/` | Python | `PLANNER_ENGINE=python` and `PLANNER_PYTHON=engines/example_python/engine.py:create` |

Try one without changing `.env`:

```bash
.venv\Scripts\transit-mcp.exe planner test "Wiehle-Reston East" "Pentagon" --engine process --command "node engines/example-js/engine.js"
```

Full contract, protocol and testing workflow: [docs/EXTENDING.md](../docs/EXTENDING.md#3-plug-in-a-scheduling-engine).
