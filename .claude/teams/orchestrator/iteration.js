// Single-agent Ralph loop.
//
// Each iteration:
//   1. Read thread deltas since this agent's last_seen_ts
//   2. Drain direct messages from interventions
//   3. Build prompt: task (first iter) / continuation (later) + deltas + DMs
//   4. Spawn `claude -p <prompt>` with --output-format stream-json --verbose
//   5. Pipe every JSONL event line to events.jsonl (for live dashboard)
//   6. Detect AGENT_DONE sentinel in the final text; update state

import { spawn } from "node:child_process";
import { appendFileSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import path from "node:path";
import {
  getAgentState,
  updateAgentState,
  readThreadSince,
  readInterventions,
} from "./state.js";

const AGENT_DONE_SENTINEL = "AGENT_DONE";

function buildPrompt({
  agent,
  agentState,
  threadPosts,
  directMessages,
  worktreePath,
  threadPath,
  isFirstIteration,
}) {
  const s = [];

  if (isFirstIteration) {
    s.push("# Your Task\n");
    s.push(`**Your name:** ${agent.name}`);
    s.push(`**Your motivation:** ${agent.motivation}`);
    s.push(`**Your worktree (write code here):** ${worktreePath}`);
    s.push(`**Shared thread (append findings here):** ${threadPath}`);
    s.push("");
    s.push(agent.prompt);
    s.push("");

    // Context dir (staged by index.js from team + per-agent context_files).
    // Paths inside preserve repo-relative structure — an agent referencing
    // ".claude/teams/orchestrator/state.js" in its prompt can Read
    // `<contextDir>/.claude/teams/orchestrator/state.js`.
    const runDir = path.dirname(threadPath);
    const contextDir = path.join(runDir, "context");
    if (existsSync(contextDir)) {
      try {
        const hasFiles = readdirSync(contextDir).length > 0;
        if (hasFiles) {
          s.push("## Context Files (staged — read these with Read, don't try to cd outside your worktree)");
          s.push(`Location: \`${contextDir}/\``);
          s.push("Structure: repo-relative paths preserved (e.g., `.claude/teams/orchestrator/state.js`).");
          if (agent.context_files?.length) {
            s.push("Files specifically for you:");
            for (const f of agent.context_files) s.push(`- \`${contextDir}/${f}\``);
          }
          s.push("");
        }
      } catch {
        // context dir exists but unreadable — skip
      }
    }

    s.push("## How to Coordinate with Your Team");
    s.push("");
    s.push(
      `You're one of several agents working in parallel. There's a shared thread at \`${threadPath}\` where all agents post findings and coordinate.`,
    );
    s.push("");
    s.push("**When to post to the thread (use Write or Edit to append):**");
    s.push("- After a significant finding");
    s.push("- Before making a decision that could affect other agents");
    s.push("- To ask a peer for input (@mention their name)");
    s.push("- When you reach a milestone worth announcing");
    s.push("");
    s.push("**Thread post format:**");
    s.push("```");
    s.push(`## [<ISO timestamp>] ${agent.name}`);
    s.push("");
    s.push("Your finding, question, or update.");
    s.push("```");
    s.push("");
    s.push(
      `**When you are fully finished with your task**, put the literal string \`${AGENT_DONE_SENTINEL}\` on its own line in your final response (not inside prose, not inside a code block). Anything else is treated as "still working".`,
    );
    s.push("");
  } else {
    s.push(`# Continuing Your Work (iteration ${agentState.iteration + 1})`);
    s.push("");
    s.push(`**Your name:** ${agent.name} — ${agent.motivation}`);
    s.push(`**Your worktree:** ${worktreePath}`);
    s.push(`**Shared thread:** ${threadPath}`);
    s.push("");
    s.push("## Your Notes from Previous Iteration");
    s.push(agentState.last_notes || "(none yet)");
    s.push("");
  }

  if (threadPosts.length > 0) {
    s.push("## Thread Updates Since Your Last Iteration");
    s.push("");
    for (const post of threadPosts) {
      s.push(`**[${post.ts}] ${post.author}:**`);
      s.push(post.content);
      s.push("");
    }
  } else if (!isFirstIteration) {
    s.push("## Thread Updates");
    s.push("(no new posts since your last iteration)");
    s.push("");
  }

  if (directMessages.length > 0) {
    s.push("## Direct Messages from Developer");
    s.push("");
    for (const msg of directMessages) {
      s.push(`**[${msg.ts}]:** ${msg.content}`);
      s.push("");
    }
  }

  s.push("---");
  s.push("");
  s.push(
    `Continue your work. Read files, analyze, write code in your worktree, and post to the thread when appropriate. End this iteration with a brief summary of what you did and what you plan next — unless you are finished, in which case put \`${AGENT_DONE_SENTINEL}\` on its own line as the last thing in your response.`,
  );

  return s.join("\n");
}

function spawnClaude({
  prompt,
  cwd,
  model = "sonnet",
  outputLog,
  eventsLog,
  additionalPermissions = [],
  extraDirs = [],
}) {
  return new Promise((resolve, reject) => {
    const args = [
      "-p",
      prompt,
      "--output-format",
      "stream-json",
      "--verbose",
      "--model",
      model,
      "--permission-mode",
      "acceptEdits",
    ];
    for (const dir of extraDirs) args.push("--add-dir", dir);
    for (const perm of additionalPermissions) args.push("--allowed-tools", perm);

    const proc = spawn("claude", args, {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, CASCADE_TEAM_AGENT: "true" },
    });

    let buffer = "";
    let stderr = "";
    const events = [];
    const textChunks = [];
    let resultText = null;

    const processLine = (line) => {
      if (!line.trim()) return;
      if (eventsLog) appendFileSync(eventsLog, line + "\n");
      try {
        const event = JSON.parse(line);
        events.push(event);
        if (event.type === "assistant" && event.message?.content) {
          for (const block of event.message.content) {
            if (block.type === "text" && block.text) textChunks.push(block.text);
          }
        }
        if (event.type === "result" && event.result) {
          resultText = event.result;
        }
      } catch {
        // mid-chunk line; ignore until we have a whole one
      }
    };

    proc.stdout.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) processLine(line);
      if (outputLog) appendFileSync(outputLog, chunk.toString());
    });
    proc.stderr.on("data", (chunk) => {
      const s = chunk.toString();
      stderr += s;
      if (outputLog) appendFileSync(outputLog, `[stderr] ${s}`);
    });

    proc.on("close", (code) => {
      if (buffer.trim()) processLine(buffer);
      if (code !== 0) {
        reject(new Error(`claude exited with code ${code}\nstderr: ${stderr}`));
        return;
      }
      const responseText = resultText || textChunks.join("\n");
      resolve({ responseText, events });
    });

    proc.on("error", reject);
  });
}

function extractNotes(response) {
  const paragraphs = response.trim().split(/\n\n+/);
  const last = paragraphs[paragraphs.length - 1] || "";
  return last.length > 500 ? "..." + last.slice(-500) : last;
}

export async function runIteration({ runDir, agent, worktreePath, threadPath }) {
  let agentState = getAgentState({ runDir, agentName: agent.name });
  const isFirstIteration = agentState.iteration === 0;

  // HIGH bug from self-review: capture the iteration-start ts BEFORE reading
  // interventions/thread, so anything arriving during Claude's execution
  // window is picked up on the next iteration rather than silently dropped.
  const iterationStartTs = new Date().toISOString();

  const threadPosts = readThreadSince({
    runDir,
    sinceTs: agentState.last_seen_ts,
  }).filter((p) => p.author !== agent.name);

  const allInterventions = readInterventions({
    runDir,
    afterTs: agentState.last_seen_ts,
  });
  const directMessages = allInterventions
    .filter((e) => e.type === "direct_message" && e.target === agent.name)
    .map((e) => ({ ts: e.ts, content: e.content }));

  const prompt = buildPrompt({
    agent,
    agentState,
    threadPosts,
    directMessages,
    worktreePath,
    threadPath,
    isFirstIteration,
  });

  const agentDir = path.join(runDir, "agents", agent.name);
  writeFileSync(path.join(agentDir, "prompt.txt"), prompt);

  const outputLog = path.join(agentDir, "output.log");
  const eventsLog = path.join(agentDir, "events.jsonl");
  appendFileSync(
    outputLog,
    `\n\n===== ITERATION ${agentState.iteration + 1} (${new Date().toISOString()}) =====\n\n`,
  );
  appendFileSync(
    eventsLog,
    JSON.stringify({
      type: "iteration_start",
      iteration: agentState.iteration + 1,
      ts: new Date().toISOString(),
    }) + "\n",
  );

  updateAgentState({
    runDir,
    agentName: agent.name,
    updates: {
      status: "running",
      started_at: agentState.started_at || new Date().toISOString(),
    },
  });

  let response;
  try {
    const result = await spawnClaude({
      prompt,
      cwd: worktreePath,
      model: agent.model || "sonnet",
      outputLog,
      eventsLog,
      additionalPermissions: agent.permissions || [],
      extraDirs: [runDir],
    });
    response = result.responseText;
  } catch (err) {
    updateAgentState({
      runDir,
      agentName: agent.name,
      updates: {
        status: "error",
        ended_at: new Date().toISOString(),
        done_reason: `error: ${err.message}`,
      },
    });
    throw err;
  }

  // HIGH bug from self-review: `response.includes(AGENT_DONE_SENTINEL)` was
  // a raw substring check that triggered on any mention — inside a code
  // block, quoted in prose, etc. Require the sentinel to appear as a
  // standalone line with no surrounding backticks or code fences.
  const done = hasAgentDoneSignal(response);

  const newIteration = agentState.iteration + 1;
  const endTs = new Date().toISOString();

  updateAgentState({
    runDir,
    agentName: agent.name,
    updates: {
      iteration: newIteration,
      // Use iteration-start ts, not end-of-iteration ts, so DMs/thread posts
      // that arrived during the run aren't lost on the next cursor advance.
      last_seen_ts: iterationStartTs,
      last_notes: extractNotes(response),
      status: done ? "done" : "idle",
      ended_at: done ? endTs : null,
      done_reason: done ? "agent_declared_done" : null,
    },
  });

  return { response, done, iteration: newIteration };
}

// Detect AGENT_DONE only when it appears as the SOLE content of a line
// (possibly with leading/trailing whitespace, not inside a fenced block or
// inline backtick span). Prose mentions like "I should include AGENT_DONE"
// or "the sentinel `AGENT_DONE`" do NOT trigger — those would prematurely
// terminate agents that discuss the protocol.
function hasAgentDoneSignal(text) {
  if (!text) return false;
  const lines = text.split("\n");
  let inFence = false;
  for (const raw of lines) {
    const trimmed = raw.trim();
    if (trimmed.startsWith("```")) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    // Accept the sentinel on its own line, with or without surrounding
    // backticks (`AGENT_DONE` or AGENT_DONE alone).
    if (trimmed === AGENT_DONE_SENTINEL) return true;
    if (trimmed === `\`${AGENT_DONE_SENTINEL}\``) return true;
  }
  return false;
}
