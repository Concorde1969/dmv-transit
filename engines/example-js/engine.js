#!/usr/bin/env node
/**
 * Example scheduling engine in JavaScript (Node 18+) — a template, not a real
 * scheduler. Zero dependencies.
 *
 * Enable it in .env:
 *     PLANNER_ENGINE=process
 *     PLANNER_COMMAND=node engines/example-js/engine.js
 *
 * Protocol: one JSON object per line on stdin; reply with one JSON object per line
 * on stdout, echoing the request's "id":
 *     -> {"id": 1, "method": "describe", "params": {}}
 *     <- {"id": 1, "result": {"name": "...", "version": "..."}}
 *     -> {"id": 2, "method": "plan", "params": <TripRequest>}
 *     <- {"id": 2, "result": <TripPlan>}
 *     on failure: {"id": 2, "error": {"message": "..."}}
 *
 * IMPORTANT: stdout is the reply channel. Log with console.error(), never
 * console.log(). Full contract: docs/planner-schema/*.schema.json
 *
 * To plug in your own scheduler, replace planTrip() below. The request gives you:
 *   request.origin / request.destination   -> {label, lat, lon, nearby_stops[]}
 *   request.day / depart_after / arrive_by -> what the rider asked for
 *   request.context.feeds[].path           -> raw GTFS .zip per agency
 */
"use strict";

const fs = require("node:fs");
const readline = require("node:readline");

const NAME = "example-js";
const VERSION = "0.1.0";

function describe() {
  return {
    name: NAME,
    version: VERSION,
    language: "javascript",
    capabilities: [],
    notes: "Template engine: echoes the nearest stops; computes no schedules.",
  };
}

function planTrip(request) {
  const { origin, destination, context } = request;
  const readable = (context.feeds || []).filter(
    (f) => f.path && fs.existsSync(f.path)
  );
  console.error(`[${NAME}] planning ${origin.label} -> ${destination.label}`);

  const oStop = (origin.nearby_stops || [])[0];
  const dStop = (destination.nearby_stops || [])[0];
  const legs = [];
  if (oStop && dStop) {
    legs.push({
      mode: "ride",
      agency: oStop.agency,
      board: oStop.name,
      board_stop_id: oStop.stop_id,
      alight: dStop.agency === oStop.agency ? dStop.name : null,
      note: "Placeholder leg — a real engine chooses routes and times here.",
    });
  }

  return {
    engine: NAME,
    origin: origin.label,
    destination: destination.label,
    day: request.day ?? null,
    options: [
      {
        summary: `Example plan from ${origin.label} to ${destination.label}`,
        legs,
        feasible: null,
        feasibility: "unknown — example engine does not read schedules",
      },
    ],
    warnings: [
      `Example engine: received ${readable.length} GTFS archive(s) ` +
        `(${readable.map((f) => f.agency).join(", ") || "none"}) but computed no ` +
        "schedule. Replace engines/example-js/engine.js with your scheduler.",
    ],
  };
}

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on("line", (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg;
  try {
    if (method === "describe") reply({ id, result: describe() });
    else if (method === "plan") reply({ id, result: planTrip(params || {}) });
    else reply({ id, error: { message: `unknown method ${method}` } });
  } catch (err) {
    reply({ id, error: { message: String(err && err.message ? err.message : err) } });
  }
});
