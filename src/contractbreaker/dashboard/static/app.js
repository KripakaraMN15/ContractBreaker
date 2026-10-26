/* ContractBreaker dashboard client.
 * The browser never receives API keys, never executes code, and never
 * fabricates evidence: it renders exactly what the backend returns.
 */
(function () {
  "use strict";

  var API = {
    plan: "/api/plan",
    execute: "/api/execute",
    sources: "/api/sources",
    health: "/api/health"
  };

  var state = {
    plan: null,
    approvalToken: null,
    result: null,
    submitting: false,
    lastRequest: null
  };

  var el = function (id) { return document.getElementById(id); };

  var dom = {
    health: el("health-chip"),
    form: el("request-form"),
    request: el("request-input"),
    source: el("source-select"),
    sourceHelp: el("source-help"),
    scenario: el("scenario-select"),
    llm: el("llm-toggle"),
    llmNote: el("llm-note"),
    serverIdempotency: el("server-idempotency"),
    retryStatuses: el("retry-statuses"),
    executionTimeout: el("execution-timeout"),
    planButton: el("plan-button"),
    resetButton: el("reset-button"),
    formStatus: el("form-status"),
    analysisPanel: el("analysis-panel"),
    analysisSummary: el("analysis-summary"),
    readiness: el("readiness-banner"),
    readinessTitle: el("readiness-title"),
    readinessDetail: el("readiness-detail"),
    factCards: el("fact-cards"),
    blockersPanel: el("blockers-panel"),
    blockersList: el("blockers-list"),
    provenancePanel: el("provenance-panel"),
    provenanceBody: el("provenance-body"),
    approvalPanel: el("approval-panel"),
    approvalSummary: el("approval-summary"),
    approveButton: el("approve-button"),
    approvalStatus: el("approval-status"),
    resultsPanel: el("results-panel"),
    verdictCard: el("verdict-card"),
    verdictBadge: el("verdict-badge"),
    verdictEffects: el("verdict-effects"),
    verdictReason: el("verdict-reason"),
    timeline: el("timeline"),
    traceNote: el("trace-note"),
    explanationBody: el("explanation-body"),
    limitationsPanel: el("limitations-panel"),
    limitationsList: el("limitations-list"),
    reportPanel: el("report-panel"),
    reportJson: el("report-json"),
    reportDetails: el("report-details"),
    exportButton: el("export-button"),
    copyButton: el("copy-button")
  };

  function setStatus(node, message, tone) {
    node.textContent = message || "";
    if (tone) { node.setAttribute("data-tone", tone); }
    else { node.removeAttribute("data-tone"); }
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function text(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }

  function elFrom(tag, className, content) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (content !== undefined && content !== null) { node.textContent = content; }
    return node;
  }

  function api(path, body) {
    var options = { method: body === undefined ? "GET" : "POST", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok || data.ok === false) {
          var err = (data && data.error) || {};
          var message = err.message || ("request failed with status " + response.status);
          var error = new Error(message);
          error.code = err.code || "http_" + response.status;
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  /* ---------------- source discovery ---------------- */

  function loadSources() {
    return api(API.sources).then(function (data) {
      var current = dom.source.value;
      clear(dom.source);
      dom.source.appendChild(elFrom("option", null, "Select a Python file…"));
      dom.source.firstChild.value = "";
      (data.sources || []).forEach(function (name) {
        var option = elFrom("option", null, name);
        option.value = name;
        dom.source.appendChild(option);
      });
      if (current) { dom.source.value = current; }
      dom.sourceHelp.textContent =
        (data.sources || []).length + " file(s) available under the allowed source root.";
    }).catch(function (error) {
      dom.sourceHelp.textContent = "Could not list sources: " + error.message;
    });
  }

  function loadHealth() {
    return api(API.health).then(function (data) {
      dom.health.textContent = "backend ok · " + data.api_schema_version;
      dom.health.className = "chip chip--ok";
      if (!data.llm_fallback_available) {
        dom.llm.disabled = true;
        dom.llmNote.textContent = "Unavailable on the server (no credentials configured).";
      } else {
        dom.llmNote.textContent = "Server-side only; may call the [OI] API.";
      }
    }).catch(function () {
      dom.health.textContent = "backend unreachable";
      dom.health.className = "chip chip--err";
    });
  }

  /* ---------------- plan ---------------- */

  function buildPayload() {
    var payload = {
      request: dom.request.value,
      source: dom.source.value,
      scenario: dom.scenario.value || null,
      llm_fallback: dom.llm.checked
    };
    if (dom.serverIdempotency.value) { payload.server_idempotency = dom.serverIdempotency.value; }
    if (dom.retryStatuses.value) {
      payload.retry_statuses = dom.retryStatuses.value.split(",")
        .map(function (part) { return part.trim(); })
        .filter(function (part) { return part.length > 0; })
        .map(function (part) { return Number(part); });
    }
    if (dom.executionTimeout.value) { payload.execution_timeout = Number(dom.executionTimeout.value); }
    return payload;
  }

  function onPlan(event) {
    event.preventDefault();
    if (state.submitting) { return; }
    state.submitting = true;
    dom.planButton.disabled = true;
    setStatus(dom.formStatus, "Planning…", "busy");
    dom.approvalPanel.hidden = true;
    dom.resultsPanel.hidden = true;
    dom.reportPanel.hidden = true;

    var payload = buildPayload();
    state.lastRequest = payload;

    api(API.plan, payload).then(function (data) {
      state.plan = data.plan;
      state.approvalToken = data.approval_token;
      state.result = null;
      renderPlan(data);
      setStatus(dom.formStatus, "Plan ready. No code has been executed.", "ok");
    }).catch(function (error) {
      state.plan = null;
      state.approvalToken = null;
      renderError(error);
      setStatus(dom.formStatus, error.message, "error");
    }).then(function () {
      state.submitting = false;
      dom.planButton.disabled = false;
    });
  }

  /* ---------------- rendering ---------------- */

  var FACT_CARDS = [
    ["method", "Method"],
    ["endpoint", "Endpoint"],
    ["timeout", "Timeout"],
    ["retry_count", "Retry count"],
    ["retry_triggers", "Retry triggers"],
    ["idempotency_key_present", "Idempotency key"],
    ["idempotency_key_reused", "Key reuse"]
  ];

  function renderPlan(data) {
    var plan = data.plan;
    dom.analysisPanel.hidden = false;
    var hypothesis = plan.analysis_snapshot.hypothesis;
    var readiness = hypothesis.scenario_readiness;

    dom.analysisSummary.textContent =
      "Static analysis of " + (plan.request.source || "the selected client") +
      ". No code has been executed.";

    dom.readiness.setAttribute("data-state", data.ready ? "ready" : "blocked");
    if (data.ready) {
      dom.readinessTitle.textContent = "Runnable — approval required";
      dom.readinessDetail.textContent =
        "Scenario " + plan.request.scenario +
        " is ready. Review the facts below, then approve execution.";
    } else {
      dom.readinessTitle.textContent = "Blocked — execution is not available";
      dom.readinessDetail.textContent =
        "The workflow could not prepare a runnable scenario. Resolve the blockers below.";
    }

    renderFactCards(hypothesis, plan.analysis_snapshot);
    renderBlockers(readiness.blockers, readiness.unknowns);
    renderProvenance(hypothesis.fact_provenance || {});
    renderApproval(data, plan);

    dom.reportPanel.hidden = false;
    dom.reportJson.textContent = JSON.stringify(plan, null, 2);
  }

  function renderFactCards(hypothesis, snapshot) {
    clear(dom.factCards);
    FACT_CARDS.forEach(function (entry) {
      var key = entry[0];
      var fact = hypothesis[key];
      if (!fact) { return; }
      var card = elFrom("div", "card");
      card.appendChild(elFrom("span", "card__label", entry[1]));
      card.appendChild(elFrom("span", "card__value", formatFactValue(fact)));
      var status = elFrom("span", "card__status", fact.status);
      status.setAttribute("data-status", fact.status);
      card.appendChild(status);
      var origin = (hypothesis.fact_provenance || {})[key];
      if (origin) { card.appendChild(elFrom("span", "card__origin", origin)); }
      dom.factCards.appendChild(card);
    });
  }

  function formatFactValue(fact) {
    if (fact.status === "known") {
      if (Array.isArray(fact.value)) { return fact.value.join(", "); }
      if (fact.value === null) { return "null"; }
      return String(fact.value);
    }
    return "not established";
  }

  function renderBlockers(blockers, unknowns) {
    var all = (blockers || []).concat(unknowns || []);
    if (!all.length) {
      dom.blockersPanel.hidden = true;
      return;
    }
    dom.blockersPanel.hidden = false;
    clear(dom.blockersList);
    all.forEach(function (item) {
      dom.blockersList.appendChild(elFrom("li", null, item));
    });
  }

  function renderProvenance(provenance) {
    var keys = Object.keys(provenance);
    if (!keys.length) { dom.provenancePanel.hidden = true; return; }
    dom.provenancePanel.hidden = false;
    clear(dom.provenanceBody);
    keys.forEach(function (key) {
      var row = elFrom("tr");
      row.appendChild(elFrom("td", null, key));
      row.appendChild(elFrom("td", null, provenance[key]));
      dom.provenanceBody.appendChild(row);
    });
  }

  function renderApproval(data, plan) {
    dom.approvalPanel.hidden = false;
    clear(dom.approvalSummary);
    var settings = plan.analysis_snapshot.hypothesis;
    var items = [
      ["Scenario", plan.request.scenario],
      ["Scenario source", plan.request.scenario_source],
      ["Source", plan.request.source],
      ["Timeout", formatFactValue(settings.timeout)],
      ["Retry count", formatFactValue(settings.retry_count)],
      ["Idempotency key", formatFactValue(settings.idempotency_key_present)],
      ["LLM fallback", plan.request.llm_fallback ? "enabled" : "disabled"]
    ];
    items.forEach(function (entry) {
      var wrapper = elFrom("div", "summary__item");
      wrapper.appendChild(elFrom("p", "summary__label", entry[0]));
      wrapper.appendChild(elFrom("p", "summary__value", text(entry[1])));
      dom.approvalSummary.appendChild(wrapper);
    });
    dom.approveButton.disabled = !data.ready || !data.approval_token;
    setApproveLabel(data.ready ? "Approve and execute" : "Execution unavailable");
    setStatus(dom.approvalStatus, "");
  }

  function setApproveLabel(label) {
    while (dom.approveButton.firstChild) {
      dom.approveButton.removeChild(dom.approveButton.firstChild);
    }
    dom.approveButton.appendChild(iconApprove());
    dom.approveButton.appendChild(document.createTextNode(label));
  }

  function iconApprove() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("class", "button__icon");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M3 8.5 6.2 11.5 13 4.5");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.8");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  function renderError(error) {
    dom.analysisPanel.hidden = false;
    dom.readiness.setAttribute("data-state", "error");
    dom.readinessTitle.textContent = "Request rejected";
    dom.readinessDetail.textContent = error.message;
    clear(dom.factCards);
    dom.blockersPanel.hidden = true;
    dom.provenancePanel.hidden = true;
    dom.approvalPanel.hidden = true;
    dom.resultsPanel.hidden = true;
  }

  /* ---------------- execution ---------------- */

  function onApprove() {
    if (state.submitting) { return; }
    if (!state.approvalToken) { return; }
    state.submitting = true;
    dom.approveButton.disabled = true;
    setStatus(dom.approvalStatus, "Executing in an isolated subprocess…", "busy");

    api(API.execute, { approval_token: state.approvalToken, approved: true })
      .then(function (data) {
        state.result = data.result;
        renderResult(data.result);
        setStatus(dom.approvalStatus, "Execution complete.", "ok");
      })
      .catch(function (error) {
        setStatus(dom.approvalStatus, error.message, "error");
        dom.approvalStatus.setAttribute("data-code", error.code || "error");
      })
      .then(function () {
        state.submitting = false;
        if (state.approvalToken) { dom.approveButton.disabled = false; }
      });
  }

  var EVENT_GROUPS = {
    request_sent: "request",
    request_received: "request",
    side_effect_committed: "commit",
    response_dropped: "fault",
    retryable_error_returned: "fault",
    retry_scheduled: "fault",
    response_returned: "success",
    idempotent_replay: "success",
    retry_policy_configured: "request",
    scenario_end: "success"
  };

  function renderResult(result) {
    dom.resultsPanel.hidden = false;
    var analysis = result.analysis;
    var execution = analysis ? analysis.execution : null;
    var verdict = result.verdict || "inconclusive";

    dom.verdictCard.setAttribute("data-state", verdict);
    dom.verdictBadge.textContent = verdict;
    dom.verdictReason.textContent = result.verdict_reason || "";
    dom.verdictEffects.textContent = execution && execution.observed_side_effects !== null && execution.observed_side_effects !== undefined
      ? execution.observed_side_effects + " side effect(s) observed"
      : "no runtime evidence";

    renderTimeline(execution);
    dom.explanationBody.textContent = result.explanation || "No explanation available.";

    var limitations = result.limitations || [];
    if (limitations.length) {
      dom.limitationsPanel.hidden = false;
      clear(dom.limitationsList);
      limitations.forEach(function (item) {
        dom.limitationsList.appendChild(elFrom("li", null, item));
      });
    } else {
      dom.limitationsPanel.hidden = true;
    }

    dom.reportPanel.hidden = false;
    dom.reportJson.textContent = JSON.stringify(result, null, 2);
  }

  function renderTimeline(execution) {
    clear(dom.timeline);
    if (!execution || !execution.occurred) {
      dom.traceNote.textContent = "no runtime execution";
      dom.timeline.appendChild(elFrom("li", "timeline__empty", "No runtime events were observed."));
      return;
    }
    var events = execution.events || [];
    dom.traceNote.textContent = events.length + " event(s) observed, in order";
    if (!events.length) {
      dom.timeline.appendChild(elFrom("li", "timeline__empty", "The runtime produced no events."));
      return;
    }
    events.forEach(function (event, index) {
      var item = elFrom("li", "timeline__item");
      item.setAttribute("data-kind", EVENT_GROUPS[event.kind] || "request");
      item.appendChild(elFrom("span", "timeline__seq", String(event.seq)));
      var rail = elFrom("span", "timeline__rail");
      rail.appendChild(elFrom("span", "timeline__dot"));
      if (index < events.length - 1) { rail.appendChild(elFrom("span", "timeline__line")); }
      item.appendChild(rail);
      var body = elFrom("div", "timeline__body");
      body.appendChild(elFrom("div", "timeline__kind", event.kind));
      var meta = [];
      if (event.attempt_id) { meta.push("attempt=" + event.attempt_id); }
      if (event.operation_id) { meta.push("operation=" + event.operation_id); }
      var data = event.data || {};
      Object.keys(data).sort().forEach(function (key) {
        var value = data[key];
        if (value === null || typeof value === "object") { return; }
        meta.push(key + "=" + value);
      });
      body.appendChild(elFrom("div", "timeline__meta", meta.join("  ")));
      item.appendChild(body);
      dom.timeline.appendChild(item);
    });
  }

  /* ---------------- export ---------------- */

  function currentDocument() {
    return state.result || state.plan;
  }

  function onExport() {
    var document = currentDocument();
    if (!document) { return; }
    var blob = new Blob([JSON.stringify(document, null, 2) + "\n"], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = elFrom("a");
    link.href = url;
    link.download = "contractbreaker-report.json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  function onCopy() {
    var document = currentDocument();
    if (!document) { return; }
    var text = JSON.stringify(document, null, 2);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    }
  }

  function onReset() {
    state.plan = null;
    state.approvalToken = null;
    state.result = null;
    dom.form.reset();
    dom.analysisPanel.hidden = true;
    dom.approvalPanel.hidden = true;
    dom.resultsPanel.hidden = true;
    dom.reportPanel.hidden = true;
    setStatus(dom.formStatus, "");
    setStatus(dom.approvalStatus, "");
    loadSources();
  }

  dom.form.addEventListener("submit", onPlan);
  dom.approveButton.addEventListener("click", onApprove);
  dom.resetButton.addEventListener("click", onReset);
  dom.exportButton.addEventListener("click", onExport);
  dom.copyButton.addEventListener("click", onCopy);
  el("new-investigation").addEventListener("click", onReset);

  loadHealth();
  loadSources();
})();
