"use client";

import {
  ArrowLeft,
  Check,
  CheckCircle2,
  Clock3,
  History,
  ListChecks,
  RotateCcw,
  ShieldAlert,
  Square,
  X,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, downloadBundle, saveAccessToken } from "@/lib/api";

type Task = {
  id: string;
  key: string;
  type: string;
  status: string;
  risk_level: string;
  attempt_count: number;
  output: Record<string, unknown> | null;
};

type WorkflowDetail = {
  id: string;
  scenario: string;
  requirement: string;
  graph_version: string;
  revision: number;
  status: string;
  risk_level: string;
  provider: string;
  created_at: string;
  tasks: Task[];
  last_error: string | null;
};

type Approval = {
  id: string;
  action: string;
  risk_level: string;
  status: string;
  reviewer: string | null;
  comment: string | null;
  requested_at: string;
};

type Artifact = {
  id: string;
  kind: string;
  version: number;
  content: string;
  sha256: string;
  created_at: string;
  status: string;
  metadata: Record<string, unknown>;
};

type Graph = { nodes: Array<{ id: string; status: string }>; edges: Array<{ source: string; target: string }> };
type Checkpoint = { id: string; revision: number; reason: string; created_at: string };

type Attempt = {
  id: string;
  task: string;
  attempt_number: number;
  provider: string;
  status: string;
  duration_ms: number;
  error_code: string | null;
  created_at: string;
};

const stageNames: Record<string, string> = {
  intake: "Understand the requirement",
  normalize: "Clarify engineering intent",
  clarification_approval: "Requirement clarification",
  decompose: "Break down the work",
  codebase_analysis: "Inspect the existing codebase",
  architecture: "Prepare architecture",
  risk: "Identify risks",
  design_approval: "Design review",
  implementation: "Prepare implementation proposal",
  tests: "Define testing approach",
  security: "Review security controls",
  documentation: "Prepare documentation",
  release: "Assess release readiness",
  final_approval: "Final review",
};

function Status({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{value.replaceAll("_", " ")}</span>;
}

export default function WorkflowDetails() {
  const params = useParams<{ id: string }>();
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [graph, setGraph] = useState<Graph>({ nodes: [], edges: [] });
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [selectedCheckpoint, setSelectedCheckpoint] = useState("");
  const [principal, setPrincipal] = useState<{ actor: string; role: string } | null>(null);
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);
  const [reviewComment, setReviewComment] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [detail, approvalData, artifactData, attemptData, graphData, checkpointData] = await Promise.all([
        api<WorkflowDetail>(`/api/v1/workflows/${params.id}`),
        api<Approval[]>(`/api/v1/workflows/${params.id}/approvals`),
        api<Artifact[]>(`/api/v1/workflows/${params.id}/artifacts`),
        api<Attempt[]>(`/api/v1/workflows/${params.id}/attempts`),
        api<Graph>(`/api/v1/workflows/${params.id}/graph`),
        api<Checkpoint[]>(`/api/v1/workflows/${params.id}/checkpoints`),
      ]);
      setWorkflow(detail);
      setApprovals(approvalData);
      setArtifacts(artifactData);
      setAttempts(attemptData);
      setGraph(graphData);
      setCheckpoints(checkpointData);
      setSelectedCheckpoint((current) => current || checkpointData[1]?.id || checkpointData[0]?.id || "");
      setSelectedArtifact((current) => current ?? artifactData.filter(a => a.status === "active").at(-1)?.id ?? null);
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to load workflow");
    }
  }, [params.id]);

  useEffect(() => {
    void load();
    void api<{ actor: string; role: string }>("/api/v1/auth/me").then(setPrincipal).catch(() => null);
  }, [load]);

  useEffect(() => {
    if (!workflow || !["pending", "running"].includes(workflow.status)) return;
    const timer = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(timer);
  }, [load, workflow]);

  const pendingApproval = approvals.find((approval) => approval.status === "pending");
  const currentArtifact = artifacts.find((artifact) => artifact.id === selectedArtifact);

  async function setAccess() {
    const token = window.prompt("Enter the reviewer access token");
    if (!token) return;
    saveAccessToken(token);
    try {
      const identity = await api<{ actor: string; role: string }>("/api/v1/auth/me");
      setPrincipal(identity);
      setMessage(`Signed in as ${identity.actor} (${identity.role}).`);
    } catch (accessError) {
      window.sessionStorage.removeItem("linkdesk_token");
      setPrincipal(null);
      setMessage(accessError instanceof Error ? accessError.message : "Invalid access token");
    }
  }

  async function decide(decision: "approved" | "rejected") {
    if (!pendingApproval || !workflow) return;
    setBusy(true);
    try {
      await api(`/api/v1/workflows/${workflow.id}/approvals/${pendingApproval.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          reviewer: "dashboard-reviewer",
          comment: reviewComment || null,
        }),
      });
      setMessage(
        decision === "approved"
          ? "Review approved. The next work has been queued."
          : "Review rejected. The workflow safe-stopped for correction.",
      );
      setReviewComment("");
      await load();
    } catch (approvalError) {
      setMessage(approvalError instanceof Error ? approvalError.message : "Approval failed");
    } finally {
      setBusy(false);
    }
  }

  async function reviseRequirement() {
    if (!workflow) return;
    const requirement = window.prompt("Revised requirement", workflow.requirement);
    if (!requirement || requirement === workflow.requirement) return;
    const reason = window.prompt("Why is this requirement changing?");
    if (!reason) return;
    setBusy(true);
    try {
      await api(`/api/v1/workflows/${workflow.id}/requirements`, {
        method: "POST",
        body: JSON.stringify({ requirement, reason, from_stage: "intake" }),
      });
      setMessage("Requirement revised. A new governed revision has started.");
      await load();
    } catch (revisionError) {
      setMessage(revisionError instanceof Error ? revisionError.message : "Revision failed");
    } finally {
      setBusy(false);
    }
  }

  async function workflowAction(action: "cancel" | "retry" | "rollback") {
    if (!workflow) return;
    if (action === "rollback" && !window.confirm("Roll back to the previous checkpoint?")) return;
    setBusy(true);
    try {
      await api(`/api/v1/workflows/${workflow.id}/${action}`, {
        method: "POST",
        body: action === "rollback"
          ? JSON.stringify({ checkpoint_id: selectedCheckpoint || null, reason: "Requested from workflow details." })
          : undefined,
      });
      setMessage(
        action === "cancel"
          ? "Workflow cancelled."
          : action === "retry"
            ? "Failed work was retried."
            : "Workflow restored to its previous checkpoint.",
      );
      await load();
    } catch (actionError) {
      setMessage(actionError instanceof Error ? actionError.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return <main className="detail-shell"><div className="detail-error"><ShieldAlert /><h1>Workflow unavailable</h1><p>{error}</p><Link href="/">Return to dashboard</Link></div></main>;
  }
  if (!workflow) {
    return <main className="detail-shell"><div className="detail-loading">Loading workflow details…</div></main>;
  }

  return (
    <main className="detail-shell">
      <div className="detail-topbar">
        <Link href="/" className="back-link"><ArrowLeft size={16} />Back to dashboard</Link>
        <div className="detail-access">
          <span>Workflow ID: {workflow.id}</span>
          <button className="access-button" onClick={setAccess}>
            {principal ? `${principal.actor} · ${principal.role}` : "Set reviewer access"}
          </button>
        </div>
      </div>

      <section className="detail-hero">
        <div>
          <span className="detail-kicker">{workflow.scenario} scenario</span>
          <h1>Workflow details</h1>
          <p>{workflow.requirement}</p>
        </div>
        <div className="detail-status">
          <Status value={workflow.status} />
          <span>{workflow.risk_level} risk · {workflow.provider} provider</span>
          <span>Created {new Date(workflow.created_at).toLocaleString()}</span>
          <div className="workflow-actions">
            {principal?.role === "requester" && !["running", "cancelled"].includes(workflow.status) && (
              <button onClick={reviseRequirement} disabled={busy}>
                <RotateCcw size={14} />Revise
              </button>
            )}
            {principal?.role === "requester" && ["safe_stopped", "failed"].includes(workflow.status) && (
              <button onClick={() => workflowAction("retry")} disabled={busy}>
                <RotateCcw size={14} />Retry
              </button>
            )}
            {principal?.role === "requester" && !["completed", "cancelled"].includes(workflow.status) && (
              <button onClick={() => workflowAction("cancel")} disabled={busy}>
                <Square size={13} />Cancel
              </button>
            )}
            {principal?.role === "reviewer" && (workflow.revision > 1 || attempts.length > 0) ? (
              <button onClick={() => workflowAction("rollback")} disabled={busy}>
                <History size={14} />Rollback
              </button>
            ) : null}
          </div>
        </div>
      </section>

      {workflow.last_error && <div className="notice" role="alert">{workflow.last_error}</div>}
      {message && <div className="notice"><Check size={16} />{message}</div>}

      <section className="artifact-top">
        <div>
          <strong>Generated artifacts</strong>
          {artifacts.some(a => a.kind === "implementation" && a.status === "active") &&
            <button className="access-button" onClick={() => downloadBundle(workflow.id).catch(e => setMessage(String(e)))}>Download review bundle</button>}
          <span>Inspect the output produced by a completed workflow stage.</span>
        </div>
        {artifacts.length > 0 ? (
          <label>
            <span>Artifact</span>
            <select
              value={selectedArtifact ?? ""}
              onChange={(event) => setSelectedArtifact(event.target.value)}
            >
              {artifacts.map((artifact) => (
                <option value={artifact.id} key={artifact.id}>
                  {stageNames[artifact.kind] ?? artifact.kind} · revision {artifact.version} · {artifact.status}
                </option>
              ))}
            </select>
          </label>
        ) : <span className="detail-empty">No artifacts have been generated yet.</span>}
      </section>

      {currentArtifact && (
        <details className="artifact-drawer">
          <summary>View {stageNames[currentArtifact.kind] ?? currentArtifact.kind} output</summary>
          <div className="artifact-drawer-meta">
            <span>Version {currentArtifact.version}</span>
            <Status value={currentArtifact.status} />
            <span title={currentArtifact.sha256}>Hash {currentArtifact.sha256.slice(0, 12)}…</span>
          </div>
          <pre>{formatArtifact(currentArtifact.content)}</pre>
        </details>
      )}

      {pendingApproval && (
        <section className="review-banner">
          <div><Clock3 size={21} /><div><strong>{pendingApproval.action}</strong><p>The workflow is paused until this checkpoint is reviewed.</p></div></div>
          <div className="review-controls">
            {principal?.role !== "reviewer" && (
              <span className="review-role-help">
                Reviewer access is required. Select <strong>Set reviewer access</strong> above and
                enter <code>local-reviewer-token</code>.
              </span>
            )}
            <input
              value={reviewComment}
              onChange={(event) => setReviewComment(event.target.value)}
              placeholder={pendingApproval.action.startsWith("Provide clarification") ? "Enter the measurable clarification (required)" : "Optional review note"}
              required={pendingApproval.action.startsWith("Provide clarification")}
            />
            <button
              className="primary"
              onClick={() => decide("approved")}
              disabled={busy || principal?.role !== "reviewer" || (pendingApproval.action.startsWith("Provide clarification") && !reviewComment.trim())}
            >
              {busy ? "Approving…" : "Review and approve"}<Check size={16} />
            </button>
            <button
              className="reject-review"
              onClick={() => decide("rejected")}
              disabled={busy || principal?.role !== "reviewer"}
            >
              <X size={15} />Reject
            </button>
          </div>
        </section>
      )}

      <section className="panel dag-panel">
        <div className="panel-head"><div><h2>Dependency graph</h2><p>Stored dependencies and synchronization gates.</p></div></div>
        <div className="dag-list">
          {graph.nodes.map((node) => {
            const dependencies = graph.edges.filter((edge) => edge.target === node.id).map((edge) => edge.source);
            return <article key={node.id}><strong>{stageNames[node.id] ?? node.id}</strong><Status value={node.status} /><span>{dependencies.length ? `After: ${dependencies.join(", ")}` : "Entry node"}</span></article>;
          })}
        </div>
      </section>

      {principal?.role === "reviewer" && checkpoints.length > 0 && (
        <section className="panel checkpoint-panel">
          <div className="panel-head"><div><h2>Recovery checkpoint</h2><p>Select the exact governed state used by rollback.</p></div></div>
          <select value={selectedCheckpoint} onChange={(event) => setSelectedCheckpoint(event.target.value)}>
            {checkpoints.map((item) => <option key={item.id} value={item.id}>Revision {item.revision} · {item.reason} · {new Date(item.created_at).toLocaleString()}</option>)}
          </select>
        </section>
      )}

      <section className="panel stages-panel detail-stages">
        <div className="panel-head"><div><h2>Workflow stages</h2><p>Each engineering stage, its status, and output.</p></div><ListChecks size={20} className="muted" /></div>
        <div className="stage-list">
          {workflow.tasks.map((task, index) => (
            <details
              key={task.id}
              className={`stage stage-${task.status}`}
            >
              <summary>
                <span className="stage-number">{task.status === "completed" ? <CheckCircle2 size={19} /> : index + 1}</span>
                <span className="stage-title">{stageNames[task.key] ?? task.key.replaceAll("_", " ")}</span>
                <Status value={task.status} />
              </summary>
              <div className="stage-details">
                <p>
                  {task.type === "approval"
                    ? "This is a human review checkpoint. The workflow continues only after approval."
                    : `${task.risk_level.charAt(0).toUpperCase() + task.risk_level.slice(1)} risk · ${task.attempt_count} execution attempt${task.attempt_count === 1 ? "" : "s"}.`}
                </p>
                {task.output ? (
                  <>
                    <strong>What this stage produced</strong>
                    <pre>{JSON.stringify(task.output, null, 2)}</pre>
                  </>
                ) : (
                  <p className="stage-no-output">
                    {task.status === "pending"
                      ? "This stage has not run yet."
                      : "No output has been recorded for this stage."}
                  </p>
                )}
              </div>
            </details>
          ))}
        </div>
      </section>

    </main>
  );
}

function formatArtifact(content: string) {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}
