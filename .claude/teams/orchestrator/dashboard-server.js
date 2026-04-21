// Dashboard HTTP server — Express + SSE. Serves the SPA and streams live updates.

import express from "express";
import path from "node:path";
import {
  readFileSync,
  existsSync,
  watchFile,
  unwatchFile,
  statSync,
  createReadStream,
} from "node:fs";
import {
  appendIntervention,
  getAgentState,
  readThreadSince,
} from "./state.js";
import { validateName } from "./worktree.js";

// Validate :name path params against the same allowlist used for worktree
// ops. Without this, Express's default :param match is [^/]+ — `..` passes
// through and routes like /api/agents/../thread.md/output would resolve to
// files outside the agents/<name>/ directory.
function requireSafeAgentName(req, res, next) {
  try {
    validateName(req.params.name, "agent name");
    next();
  } catch {
    res.status(400).json({ error: "invalid agent name" });
  }
}

const ORCHESTRATOR_DIR = path.dirname(new URL(import.meta.url).pathname);
const PUBLIC_DIR = path.join(ORCHESTRATOR_DIR, "..", "dashboard", "public");

export function startDashboard({ runDir, port = 0 }) {
  const app = express();
  app.use(express.json());
  app.use(express.static(PUBLIC_DIR));

  app.get("/", (req, res) => {
    res.sendFile(path.join(PUBLIC_DIR, "index.html"));
  });

  app.get("/api/config", (req, res) => {
    const configPath = path.join(runDir, "config.json");
    if (!existsSync(configPath)) return res.status(404).json({ error: "no run" });
    res.json(JSON.parse(readFileSync(configPath, "utf8")));
  });

  app.get("/api/agents", (req, res) => {
    const configPath = path.join(runDir, "config.json");
    if (!existsSync(configPath)) return res.status(404).json({ error: "no run" });
    const config = JSON.parse(readFileSync(configPath, "utf8"));
    const agents = config.agents.map((a) => ({
      ...a,
      state: getAgentState({ runDir, agentName: a.name }),
    }));
    res.json(agents);
  });

  app.get("/api/thread", (req, res) => {
    const sinceTs = req.query.since || null;
    res.json(readThreadSince({ runDir, sinceTs }));
  });

  app.get("/api/thread.md", (req, res) => {
    const threadPath = path.join(runDir, "thread.md");
    if (!existsSync(threadPath)) return res.status(404).send("no thread");
    res.type("text/markdown").send(readFileSync(threadPath, "utf8"));
  });

  app.get("/api/agents/:name/output", requireSafeAgentName, (req, res) => {
    const logPath = path.join(runDir, "agents", req.params.name, "output.log");
    if (!existsSync(logPath)) return res.status(404).send("no output yet");
    res.type("text/plain").send(readFileSync(logPath, "utf8"));
  });

  // Per-agent SSE stream of events.jsonl lines as they're appended
  app.get("/api/agents/:name/events/stream", requireSafeAgentName, (req, res) => {
    const eventsPath = path.join(runDir, "agents", req.params.name, "events.jsonl");
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.flushHeaders();

    let lastSize = 0;
    let partialLine = "";

    const sendNew = () => {
      if (!existsSync(eventsPath)) return;
      const size = statSync(eventsPath).size;
      // HIGH bug from self-review: if the file is rotated/truncated, size <
      // lastSize forever and the stream stalls silently. Detect rollback and
      // resume from the beginning of the new file.
      if (size < lastSize) {
        lastSize = 0;
        partialLine = "";
      }
      if (size <= lastSize) return;
      const stream = createReadStream(eventsPath, {
        start: lastSize,
        end: size - 1,
        encoding: "utf8",
      });
      let chunk = "";
      stream.on("data", (d) => {
        chunk += d;
      });
      stream.on("end", () => {
        lastSize = size;
        const combined = partialLine + chunk;
        const lines = combined.split("\n");
        partialLine = lines.pop();
        for (const line of lines) {
          if (line.trim()) {
            res.write(`event: agent_event\ndata: ${line}\n\n`);
          }
        }
      });
      stream.on("error", () => {});
    };

    sendNew();
    const watcher = () => sendNew();
    watchFile(eventsPath, { interval: 300 }, watcher);

    req.on("close", () => unwatchFile(eventsPath, watcher));
  });

  // Full event log (for late-joiners catching up)
  app.get("/api/agents/:name/events", requireSafeAgentName, (req, res) => {
    const eventsPath = path.join(runDir, "agents", req.params.name, "events.jsonl");
    if (!existsSync(eventsPath)) return res.json([]);
    const content = readFileSync(eventsPath, "utf8").trim();
    if (!content) return res.json([]);
    const events = content
      .split("\n")
      .filter((l) => l.trim())
      .map((l) => {
        try {
          return JSON.parse(l);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
    res.json(events);
  });

  // Intervention endpoints — dashboard POSTs → orchestrator drains before dispatch.
  // MEDIUM bug from self-review: generic `{ type, ...body }` spread let a body
  // with its own "type" override the intended event type (e.g., POST to
  // stop-agent with {"type":"stop_team"} killed the whole team). Fix:
  // whitelist fields explicitly per endpoint; never accept a client-supplied
  // `type`.
  const validateTarget = (target) => {
    try {
      validateName(target, "target");
      return null;
    } catch (e) {
      return e.message;
    }
  };
  const targetOnly = (type) => (req, res) => {
    const target = req.body?.target;
    const err = validateTarget(target);
    if (err) return res.status(400).json({ error: err });
    const entry = appendIntervention({ runDir, event: { type, target } });
    res.json({ ok: true, entry });
  };

  app.post("/api/interventions/thread-post", (req, res) => {
    const content = req.body?.content;
    if (typeof content !== "string" || !content.trim())
      return res.status(400).json({ error: "content required" });
    const entry = appendIntervention({
      runDir,
      event: { type: "thread_post", author: "developer", content },
    });
    res.json({ ok: true, entry });
  });
  app.post("/api/interventions/direct-message", (req, res) => {
    const target = req.body?.target;
    const content = req.body?.content;
    const err = validateTarget(target);
    if (err) return res.status(400).json({ error: err });
    if (typeof content !== "string" || !content.trim())
      return res.status(400).json({ error: "content required" });
    const entry = appendIntervention({
      runDir,
      event: { type: "direct_message", target, content },
    });
    res.json({ ok: true, entry });
  });
  app.post("/api/interventions/stop-agent", targetOnly("stop_agent"));
  app.post("/api/interventions/pause-agent", targetOnly("pause_agent"));
  app.post("/api/interventions/resume-agent", targetOnly("resume_agent"));
  app.post("/api/interventions/stop-team", (req, res) => {
    const entry = appendIntervention({ runDir, event: { type: "stop_team" } });
    res.json({ ok: true, entry });
  });

  // Team state SSE
  app.get("/api/stream", (req, res) => {
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.flushHeaders();

    const sendState = () => {
      try {
        const configPath = path.join(runDir, "config.json");
        if (!existsSync(configPath)) return;
        const config = JSON.parse(readFileSync(configPath, "utf8"));
        const agents = config.agents.map((a) => ({
          name: a.name,
          motivation: a.motivation,
          model: a.model || "sonnet",
          state: getAgentState({ runDir, agentName: a.name }),
        }));
        const thread = readThreadSince({ runDir, sinceTs: null });
        res.write(`event: state\ndata: ${JSON.stringify({ agents, thread })}\n\n`);
      } catch {
        // files mid-write; next tick will catch up
      }
    };

    sendState();

    const files = [
      path.join(runDir, "thread.md"),
      path.join(runDir, "interventions.jsonl"),
    ];
    const agentStateFiles = [];
    try {
      const config = JSON.parse(
        readFileSync(path.join(runDir, "config.json"), "utf8"),
      );
      for (const a of config.agents) {
        agentStateFiles.push(path.join(runDir, "agents", a.name, "state.json"));
      }
    } catch {
      // ignore — dashboard will retry
    }

    const allFiles = [...files, ...agentStateFiles];
    for (const f of allFiles) watchFile(f, { interval: 500 }, sendState);

    req.on("close", () => {
      for (const f of allFiles) unwatchFile(f, sendState);
    });
  });

  return new Promise((resolve) => {
    const server = app.listen(port, "localhost", () => {
      const actualPort = server.address().port;
      resolve({ server, port: actualPort, url: `http://localhost:${actualPort}` });
    });
  });
}
