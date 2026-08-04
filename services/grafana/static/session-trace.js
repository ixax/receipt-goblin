(function () {
  "use strict";

  var root = document.getElementById("root");

  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin) return;
    var payload = event.data;
    if (!payload || typeof payload !== "object") return;
    render(payload);
  });

  function render(payload) {
    root.textContent = "";
    if (payload.stats) root.appendChild(renderStats(payload.stats));
    var lines = document.createElement("div");
    lines.className = "st-lines";
    (payload.lines || []).forEach(function (line) {
      var el = renderLine(line);
      if (el) lines.appendChild(el);
    });
    root.appendChild(lines);
  }

  function fmtDuration(sec) {
    sec = sec || 0;
    if (sec >= 86400) return Math.floor(sec / 86400) + "d " + Math.floor((sec % 86400) / 3600) + "h";
    if (sec >= 3600) return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
    if (sec >= 60) return Math.floor(sec / 60) + "m " + (sec % 60) + "s";
    return sec + "s";
  }

  function fmtCount(n) {
    n = n || 0;
    if (n >= 1000000) return round1(n / 1000000) + "m";
    if (n >= 1000) return round1(n / 1000) + "k";
    return String(n);
  }

  function round1(n) {
    return Math.round(n * 10) / 10;
  }

  function statRow(label, value) {
    var wrap = document.createElement("div");
    var l = document.createElement("span");
    l.className = "st-stat-label";
    l.textContent = label + ":";
    var v = document.createElement("span");
    v.className = "st-stat-value";
    v.textContent = value;
    wrap.appendChild(l);
    wrap.appendChild(v);
    return wrap;
  }

  function renderStats(s) {
    var box = document.createElement("div");
    box.className = "st-stats";
    box.appendChild(statRow("Started", s.started_at || ""));
    box.appendChild(statRow("Duration", fmtDuration(s.duration_sec)));
    box.appendChild(statRow("Cost", "$" + round1(s.cost || 0).toFixed ? (s.cost || 0).toFixed(2) : s.cost));
    box.appendChild(statRow("Tokens", fmtCount(s.tokens_in) + " in / " + fmtCount(s.tokens_out) + " out"));
    box.appendChild(statRow("Model(s)", (s.models || []).join(", ") || "none"));
    box.appendChild(statRow("Prompts", s.prompts != null ? s.prompts : 0));
    box.appendChild(statRow("Tool calls", s.tool_calls != null ? s.tool_calls : 0));
    box.appendChild(statRow("Agents", (s.agents || []).join(", ") || "none"));
    box.appendChild(statRow("Skills", (s.skills || []).join(", ") || "none"));
    box.appendChild(statRow("Git", s.git_repo || s.git_branch ? (s.git_repo || "?") + ":" + (s.git_branch || "?") : "unknown"));
    return box;
  }

  // Safe "markdown-lite" renderer: **bold** and `code`, newlines -> <br>.
  // Builds DOM nodes directly (textContent only) so raw DB text can never
  // become live HTML/script, regardless of the panel's own sanitizer state.
  function appendRichText(parent, text) {
    if (!text) return;
    var re = /\*\*([^*\n]+?)\*\*|`([^`\n]+?)`|\n/g;
    var last = 0;
    var m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[0] === "\n") {
        parent.appendChild(document.createElement("br"));
      } else if (m[1] !== undefined) {
        var b = document.createElement("b");
        b.textContent = m[1];
        parent.appendChild(b);
      } else if (m[2] !== undefined) {
        var c = document.createElement("span");
        c.className = "st-code";
        c.textContent = m[2];
        parent.appendChild(c);
      }
      last = re.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function marker(kind, line) {
    if (kind === "prompt") {
      if (line.is_agent_task) return "○"; // ○
      if (line.is_webpage) return "◉"; // ◉
      if (line.is_real) return "▯"; // ▯
      return "○";
    }
    if (kind === "reply" || kind === "commentary") return "○─●"; // reply bullet
    if (kind === "tool") return "├─"; // ├─
    if (kind === "agent_spawn") return "├─";
    if (kind === "failure" || kind === "api_error") return "🚨"; // 🚨
    return "○";
  }

  function tail(line) {
    var t = document.createElement("div");
    t.className = "st-tail";
    if (line.duration_ms != null) {
      var d = document.createElement("span");
      d.textContent = line.duration_ms > 100 ? round1(line.duration_ms / 1000) + "s" : line.duration_ms + "ms";
      t.appendChild(d);
    }
    if (line.tokens != null && line.tokens > 0) {
      var tok = document.createElement("span");
      tok.textContent = fmtCount(line.tokens);
      t.appendChild(tok);
    }
    if (line.cost != null && line.cost > 0) {
      var cost = document.createElement("span");
      cost.className = "cost";
      cost.textContent = "$" + line.cost.toFixed(2);
      t.appendChild(cost);
    }
    return t.childNodes.length ? t : null;
  }

  function renderLine(line) {
    var kind = line.kind || "unknown";
    var row = document.createElement("div");
    row.className = "st-line st-kind-" + kind + (line.nested ? " st-nested" : "");

    var ts = document.createElement("span");
    ts.className = "st-ts";
    ts.textContent = line.ts ? line.ts.slice(11, 19) || line.ts : "";
    row.appendChild(ts);

    var mk = document.createElement("span");
    mk.className = "st-marker";
    mk.textContent = marker(kind, line);
    row.appendChild(mk);

    var body = document.createElement("span");
    body.className = "st-body";

    switch (kind) {
      case "prompt":
        appendRichText(body, line.text);
        if (line.display_arg) {
          var arg = document.createElement("span");
          arg.className = "st-arg";
          arg.textContent = line.display_arg;
          body.appendChild(arg);
        }
        if (line.is_judge && line.has_judge_ok) {
          var jv = document.createElement("span");
          jv.className = line.judge_ok ? "st-judge-ok" : "st-judge-bad";
          jv.textContent = " " + (line.judge_ok ? "✓" : "✗");
          body.appendChild(jv);
        }
        break;
      case "collab_mode":
      case "judge_reason":
      case "stop_hook_reason":
      case "ask_user_question":
        appendRichText(body, line.text);
        break;
      case "tool": {
        var tn = document.createElement("span");
        tn.className = "st-tool-name";
        tn.textContent = line.tool_name || "";
        body.appendChild(tn);
        if (line.tool_args) {
          var ta = document.createElement("span");
          ta.className = "st-arg";
          ta.title = line.tool_args_full || "";
          ta.textContent = line.tool_args;
          body.appendChild(ta);
        }
        break;
      }
      case "agent_spawn": {
        body.appendChild(document.createTextNode("Agent spawned: "));
        var an = document.createElement("b");
        an.textContent = line.agent_spawn_name || "";
        body.appendChild(an);
        if (line.agent_spawn_description) {
          var desc = document.createElement("span");
          desc.className = "st-arg";
          desc.textContent = line.agent_spawn_description;
          body.appendChild(desc);
        }
        break;
      }
      case "reply":
      case "commentary":
        appendRichText(body, line.reply_text || line.commentary_text || line.text);
        break;
      case "failure":
        body.appendChild(document.createTextNode((line.failed_tool_name || "tool") + " failed: "));
        var err = document.createElement("span");
        err.className = "st-arg";
        err.textContent = line.failed_tool_error || "";
        body.appendChild(err);
        break;
      case "api_error":
        body.appendChild(document.createTextNode("API error: "));
        var et = document.createElement("span");
        et.className = "st-arg";
        et.textContent = line.api_error_type || "unknown";
        body.appendChild(et);
        break;
      default:
        appendRichText(body, line.text || "");
    }
    row.appendChild(body);

    var t = tail(line);
    if (t) row.appendChild(t);

    return row;
  }
})();
