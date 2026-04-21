// Multi-agent coordinator. Dispatches iterations across agents, drains
// thread-post interventions into thread.md before each iteration, and stops
// when all agents are done, a stop_team intervention arrives, or time's up.

import path from "node:path";
import {
  getAgentState,
  updateAgentState,
  appendThreadPost,
  readInterventions,
} from "./state.js";
import { runIteration } from "./iteration.js";

// Drain interventions that need orchestrator action (thread_post, stop_*).
// Direct messages are read lazily inside runIteration from the JSONL.
function drainGlobalInterventions(runDir, sinceTs) {
  const events = readInterventions({ runDir, afterTs: sinceTs });
  const control = {
    stopTeam: false,
    stopAgents: new Set(),
    pauseAgents: new Set(),
    resumeAgents: new Set(),
  };
  for (const e of events) {
    switch (e.type) {
      case "thread_post":
        appendThreadPost({
          runDir,
          author: e.author || "developer",
          content: e.content || "",
        });
        break;
      case "stop_team":
        control.stopTeam = true;
        break;
      case "stop_agent":
        if (e.target) control.stopAgents.add(e.target);
        break;
      case "pause_agent":
        if (e.target) control.pauseAgents.add(e.target);
        break;
      case "resume_agent":
        if (e.target) control.resumeAgents.add(e.target);
        break;
      default:
        break;
    }
  }
  return control;
}

// Is the agent in a state that still wants another iteration?
function agentWantsMore(state) {
  return state.status !== "done" && state.status !== "error" && state.status !== "stopped";
}

export async function runTeam({
  runDir,
  config,
  worktreePaths, // { agentName: path }
  signal, // AbortSignal (optional, used by the CLI for Ctrl-C)
}) {
  const threadPath = path.join(runDir, "thread.md");
  const maxIterations = config.max_iterations_per_agent || 10;
  const maxTotalMinutes = config.max_total_minutes || 30;
  const deadline = Date.now() + maxTotalMinutes * 60 * 1000;
  const parallel = config.parallel !== false;

  let lastInterventionTs = null;

  const runOne = async (agent) => {
    while (true) {
      if (signal?.aborted) return { agent: agent.name, stopped: "abort_signal" };

      const control = drainGlobalInterventions(runDir, lastInterventionTs);
      lastInterventionTs = new Date().toISOString();

      if (control.stopTeam) {
        updateAgentState({
          runDir,
          agentName: agent.name,
          updates: { status: "stopped", done_reason: "stop_team" },
        });
        return { agent: agent.name, stopped: "stop_team" };
      }
      if (control.stopAgents.has(agent.name)) {
        updateAgentState({
          runDir,
          agentName: agent.name,
          updates: { status: "stopped", done_reason: "stop_agent" },
        });
        return { agent: agent.name, stopped: "stop_agent" };
      }
      if (control.pauseAgents.has(agent.name)) {
        updateAgentState({
          runDir,
          agentName: agent.name,
          updates: { status: "paused" },
        });
        // Simple pause loop: poll for resume
        while (true) {
          await new Promise((r) => setTimeout(r, 2000));
          const fresh = drainGlobalInterventions(runDir, lastInterventionTs);
          lastInterventionTs = new Date().toISOString();
          if (fresh.stopTeam || fresh.stopAgents.has(agent.name)) {
            updateAgentState({
              runDir,
              agentName: agent.name,
              updates: { status: "stopped" },
            });
            return { agent: agent.name, stopped: "stop_after_pause" };
          }
          if (fresh.resumeAgents.has(agent.name)) {
            updateAgentState({
              runDir,
              agentName: agent.name,
              updates: { status: "idle" },
            });
            break;
          }
          if (Date.now() > deadline) {
            return { agent: agent.name, stopped: "deadline_during_pause" };
          }
        }
      }

      const state = getAgentState({ runDir, agentName: agent.name });
      if (!agentWantsMore(state)) {
        return { agent: agent.name, stopped: state.done_reason || state.status };
      }
      if (state.iteration >= maxIterations) {
        updateAgentState({
          runDir,
          agentName: agent.name,
          updates: { status: "stopped", done_reason: "max_iterations" },
        });
        return { agent: agent.name, stopped: "max_iterations" };
      }
      if (Date.now() > deadline) {
        updateAgentState({
          runDir,
          agentName: agent.name,
          updates: { status: "stopped", done_reason: "deadline" },
        });
        return { agent: agent.name, stopped: "deadline" };
      }

      try {
        const { done } = await runIteration({
          runDir,
          agent,
          worktreePath: worktreePaths[agent.name],
          threadPath,
        });
        if (done) return { agent: agent.name, stopped: "agent_done" };
      } catch (err) {
        // runIteration already wrote error state
        return { agent: agent.name, stopped: `error: ${err.message}` };
      }
    }
  };

  if (parallel) {
    return await Promise.all(config.agents.map((a) => runOne(a)));
  } else {
    const results = [];
    for (const agent of config.agents) {
      results.push(await runOne(agent));
    }
    return results;
  }
}
