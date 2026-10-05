"use client";

import {
  Check,
  ClipboardList,
  Clock3,
  Copy,
  Eye,
  ExternalLink,
  Lightbulb,
  Link2,
  ListChecks,
  Plus,
  Power,
  Search,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { ActivityEvent, API_URL, api, LinkItem, saveAccessToken, Workflow } from "@/lib/api";

const scenarios = {
  greenfield:
    "Create a URL shortener with custom aliases and daily click analytics.",
  brownfield:
    "Add expiring links to the URL shortener baseline, preserving aliases and daily analytics.",
  ambiguous: "Make shortened links smarter and safer.",
};

const helpfulTips = [
  {
    title: "What is a click?",
    text: "A click is counted each time someone opens one of your short URLs.",
  },
  {
    title: "Short URLs are easier to share",
    text: "Copy the short URL from the Actions column and use it in messages, documents, or posts.",
  },
  {
    title: "Custom names improve recognition",
    text: "Use a memorable custom short name so recipients understand what the link is for.",
  },
  {
    title: "Destinations remain unchanged",
    text: "A short URL redirects visitors to the full destination shown in the table.",
  },
  {
    title: "Click totals show usage",
    text: "Higher click counts indicate which short URLs are being used most often.",
  },
];

function Badge({ value }: { value: string }) {
  return <span className={`badge badge-${value}`}>{value.replaceAll("_", " ")}</span>;
}

export default function Dashboard() {
  const [links, setLinks] = useState<LinkItem[]>([]);
  const [runs, setRuns] = useState<Workflow[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [tab, setTab] = useState<"links" | "workflows" | "activity">("links");
  const [modal, setModal] = useState<"link" | "workflow" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tipIndex, setTipIndex] = useState(0);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [principal, setPrincipal] = useState<{ actor: string; role: string } | null>(null);
  const [workflowMetrics, setWorkflowMetrics] = useState<{
    success_rate: number;
    retry_count: number;
    rollback_count: number;
    mttr_seconds: number | null;
    average_latency_seconds: number | null;
  } | null>(null);
  const [analytics, setAnalytics] = useState<{
    alias: string;
    total_clicks: number;
    daily: Array<{ date: string; clicks: number }>;
  } | null>(null);

  const load = useCallback(async () => {
    if (!window.sessionStorage.getItem("linkdesk_token")) return;
    try {
      const [linkData, runData, activityData, metricData] = await Promise.all([
        api<LinkItem[]>("/api/v1/links"),
        api<Workflow[]>("/api/v1/workflows"),
        api<ActivityEvent[]>("/api/v1/activity"),
        api<typeof workflowMetrics>("/api/v1/workflows/summary/metrics"),
      ]);
      setLinks(linkData);
      setRuns(runData);
      setActivity(activityData);
      setWorkflowMetrics(metricData);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Unable to load the dashboard");
    }
  }, []);

  useEffect(() => {
    void load();
    void api<{ actor: string; role: string }>("/api/v1/auth/me").then(setPrincipal).catch(() => null);
    setTipIndex(Math.floor(Math.random() * helpfulTips.length));
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => { void load(); }, 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function setAccess() {
    const token = window.prompt("Enter the requester or reviewer access token");
    if (!token) return;
    saveAccessToken(token);
    try {
      const identity = await api<{ actor: string; role: string }>("/api/v1/auth/me");
      setPrincipal(identity);
      await load();
      setNotice(`Signed in as ${identity.actor} (${identity.role}).`);
    } catch (error) {
      window.sessionStorage.removeItem("linkdesk_token");
      setPrincipal(null);
      setNotice(error instanceof Error ? error.message : "Invalid access token");
    }
  }

  const clicks = useMemo(() => links.reduce((sum, link) => sum + link.click_count, 0), [links]);
  const activeLinks = links.filter((link) => link.status === "active").length;
  const averageClicks = links.length ? Math.round(clicks / links.length) : 0;
  const filteredLinks = links.filter((link) => {
    const searchText = `${link.title ?? ""} ${link.alias} ${link.target_url}`.toLowerCase();
    return (
      searchText.includes(query.toLowerCase()) &&
      (statusFilter === "all" || link.status === statusFilter)
    );
  });

  async function createLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await api("/api/v1/links", {
        method: "POST",
        body: JSON.stringify({
          target_url: data.get("target_url"),
          custom_alias: data.get("custom_alias") || null,
          title: data.get("title") || null,
          expires_at: data.get("expires_at")
            ? new Date(String(data.get("expires_at"))).toISOString()
            : null,
          redirect_type: Number(data.get("redirect_type") || 302),
        }),
      });
      setModal(null);
      setNotice("Short link created. It is now listed below.");
      await load();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Link creation failed");
    } finally {
      setBusy(false);
    }
  }

  async function createWorkflow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await api("/api/v1/workflows", {
        method: "POST",
        body: JSON.stringify({
          scenario: data.get("scenario"),
          requirement: data.get("requirement"),
          risk_level: data.get("risk_level"),
        }),
      });
      setModal(null);
      setNotice("Workflow queued. Open its details to follow progress and review checkpoints.");
      await load();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Workflow failed");
    } finally {
      setBusy(false);
    }
  }

  async function deleteLink(link: LinkItem) {
    const confirmed = window.confirm(
      `Delete “${link.title || link.alias}”? Its short URL will stop working.`,
    );
    if (!confirmed) return;
    setBusy(true);
    try {
      await api<void>(`/api/v1/links/${link.id}`, { method: "DELETE" });
      setNotice(`“${link.title || link.alias}” was deleted.`);
      await load();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Unable to delete the link");
    } finally {
      setBusy(false);
    }
  }

  async function toggleLink(link: LinkItem) {
    const nextStatus = link.status === "active" ? "disabled" : "active";
    setBusy(true);
    try {
      await api(`/api/v1/links/${link.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: nextStatus, version: link.version }),
      });
      setNotice(
        `“${link.title || link.alias}” is now ${nextStatus === "active" ? "enabled" : "disabled"}.`,
      );
      await load();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Unable to update the link");
    } finally {
      setBusy(false);
    }
  }

  async function copyShortUrl(link: LinkItem) {
    const shortUrl = `${API_URL}/${link.alias}`;
    try {
      await navigator.clipboard.writeText(shortUrl);
      setNotice(`Link copied: ${shortUrl}`);
    } catch {
      setNotice("The link could not be copied. Please copy it directly from the table.");
    }
  }

  async function showAnalytics(link: LinkItem) {
    try {
      const result = await api<{
        total_clicks: number;
        daily: Array<{ date: string; clicks: number }>;
      }>(`/api/v1/links/${link.id}/analytics`);
      setAnalytics({ alias: link.alias, ...result });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Unable to load analytics");
    }
  }

  const pageTitle =
    tab === "links" ? "Links" : tab === "workflows" ? "Engineering workflows" : "Activity log";
  const pageDescription =
    tab === "links"
      ? "Create short URLs, see where they lead, and understand how often they are used."
      : tab === "workflows"
        ? ""
        : "Review recorded link and workflow operations for troubleshooting and accountability.";

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><Link2 size={19} /></span>
          <span>Orbit</span>
        </div>
        <nav aria-label="Main navigation">
          <button className={tab === "links" ? "active" : ""} onClick={() => setTab("links")}>
            <Link2 size={18} />Links
          </button>
          <button className={tab === "workflows" ? "active" : ""} onClick={() => setTab("workflows")}>
            <ListChecks size={18} />Workflows
          </button>
          <button className={tab === "activity" ? "active" : ""} onClick={() => setTab("activity")}>
            <ClipboardList size={18} />Activity log
          </button>
        </nav>
        <div className="sidebar-help">
          <Lightbulb size={18} />
          <div>
            <strong>{helpfulTips[tipIndex].title}</strong>
            <p>{helpfulTips[tipIndex].text}</p>
          </div>
        </div>
      </aside>

      <section className="workspace">
        <header>
          <div>
            <h1>{pageTitle}</h1>
            <p className="page-description">{pageDescription}</p>
          </div>
          {tab !== "activity" && (
            <div className="header-actions">
              <button className="access-button" onClick={setAccess}>
                {principal ? `${principal.actor} · ${principal.role}` : "Set access token"}
              </button>
              {principal?.role === "requester" && <button
                className="primary"
                onClick={() => setModal(tab === "workflows" ? "workflow" : "link")}
              >
                <Plus size={17} />{tab === "workflows" ? "New workflow" : "Create link"}
              </button>}
            </div>
          )}
        </header>

        {notice && (
          <div className="notice">
            <Check size={16} />{notice}
            <button onClick={() => setNotice(null)} aria-label="Dismiss message"><X size={15} /></button>
          </div>
        )}

        {tab === "links" && (
          <>
            <div className="metric-grid link-metrics">
              <Metric label="Total links" value={links.length.toString()} detail="Short URLs you have created" />
              <Metric label="Active" value={activeLinks.toString()} detail="Links currently available" />
              <Metric label="Total clicks" value={clicks.toLocaleString()} detail="Combined visits to all short URLs" />
              <Metric label="Average clicks" value={averageClicks.toLocaleString()} detail="Average visits per link" />
            </div>

            <div className="links-content">
              <section className="panel links-panel">
                <div className="panel-head">
                  <div>
                    <h2>All links</h2>
                    <p>Use the short URL to send visitors to the full destination URL.</p>
                  </div>
                  <span className="count">{links.length} {links.length === 1 ? "link" : "links"}</span>
                </div>
                <div className="table-tools">
                  <label className="search-box">
                    <Search size={15} />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Search by name, short URL, or destination"
                      aria-label="Search links"
                    />
                  </label>
                  <select
                    value={statusFilter}
                    onChange={(event) => setStatusFilter(event.target.value)}
                    aria-label="Filter links by status"
                  >
                    <option value="all">All statuses</option>
                    <option value="active">Active</option>
                    <option value="disabled">Disabled</option>
                  </select>
                </div>
                <LinksTable
                  links={filteredLinks}
                  onDelete={deleteLink}
                  onToggle={toggleLink}
                  onCopy={copyShortUrl}
                  onAnalytics={showAnalytics}
                  busy={busy}
                  canManage={principal?.role === "requester"}
                />
              </section>
            </div>
          </>
        )}

        {tab === "workflows" && (
          <div className="workflow-layout">
            <section className="panel">
              {workflowMetrics && <div className="metric-grid workflow-metrics">
                <Metric label="Success rate" value={`${Math.round(workflowMetrics.success_rate * 100)}%`} detail="Terminal workflows completed" />
                <Metric label="Retries" value={workflowMetrics.retry_count.toString()} detail="Additional task attempts" />
                <Metric label="Rollbacks" value={workflowMetrics.rollback_count.toString()} detail="Governed recovery actions" />
                <Metric label="MTTR / latency" value={`${workflowMetrics.mttr_seconds ?? "—"}s / ${workflowMetrics.average_latency_seconds ?? "—"}s`} detail="Recovery and end-to-end average" />
              </div>}
              <div className="panel-head">
                <div><h2>Workflow history</h2><p>Requirements processed for the assessment demonstration.</p></div>
                <span className="count">{runs.length} runs</span>
              </div>
              <WorkflowTable runs={runs} />
            </section>
            <section className="panel process-panel">
              <h2>How a workflow runs</h2>
              <p>Each requirement follows these reviewable engineering steps.</p>
              <ol>
                {["Understand requirement", "Break down work", "Review architecture and risk", "Approve the design", "Prepare implementation and tests", "Final approval"].map((step) => <li key={step}>{step}</li>)}
              </ol>
            </section>
          </div>
        )}

        {tab === "activity" && (
          <section className="panel activity-panel">
            <div className="panel-head">
              <div><h2>Recorded operations</h2><p>Newest events appear first. Application logs remain available through Docker.</p></div>
              <span className="count">{activity.length} events</span>
            </div>
            <div className="table-wrap">
              <table className="activity-table">
                <thead><tr><th>When</th><th>Event</th><th>Link or workflow</th><th>Action</th><th>Result</th></tr></thead>
                <tbody>
                  {activity.map((event) => (
                    <tr key={event.id}>
                      <td>{new Date(event.created_at).toLocaleString()}</td>
                      <td><strong>{event.action_label}</strong><span className="cell-note">{event.actor}</span></td>
                      <td>
                        <strong>{event.resource_label}</strong>
                        {event.alias && <span className="cell-note">/{event.alias}</span>}
                      </td>
                      <td>
                        <span className="activity-description">{event.description}</span>
                        {event.destination_url && (
                          <a
                            className="activity-destination"
                            href={event.destination_url}
                            target="_blank"
                            rel="noreferrer"
                            title={event.destination_url}
                          >
                            {event.destination_url}<ExternalLink size={11} />
                          </a>
                        )}
                      </td>
                      <td><Badge value={event.outcome} /></td>
                    </tr>
                  ))}
                  {!activity.length && <tr><td colSpan={5}><Empty title="No activity yet" copy="Create or use a link to record the first event." /></td></tr>}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </section>

      {modal === "link" && (
        <Modal
          title="Create a short link"
          subtitle="Enter the page visitors should reach. The new short URL will appear in the Links table."
          close={() => setModal(null)}
        >
          <form onSubmit={createLink}>
            <label>Destination URL
              <small>The complete web address visitors should be sent to.</small>
              <input required name="target_url" type="url" placeholder="https://example.com/documentation" />
            </label>
            <label>Link name <span>optional</span>
              <small>A recognizable label shown only in this dashboard.</small>
              <input name="title" placeholder="Product documentation" />
            </label>
            <label>Custom short name <span>optional</span>
              <small>Creates a short URL such as localhost:8000/product-docs.</small>
              <input name="custom_alias" minLength={4} placeholder="product-docs" />
            </label>
            <label>Expiration <span>optional</span>
              <small>After this date and time, the short URL stops redirecting.</small>
              <input name="expires_at" type="datetime-local" />
            </label>
            <label>Redirect behavior
              <small>Temporary is safest when the destination may change.</small>
              <select name="redirect_type" defaultValue="302">
                <option value="302">302 — Temporary</option>
                <option value="307">307 — Temporary, preserve method</option>
                <option value="301">301 — Permanent</option>
              </select>
            </label>
            <button className="primary submit" disabled={busy}>
              {busy ? "Creating…" : "Create link"}<Link2 size={17} />
            </button>
          </form>
        </Modal>
      )}

      {modal === "workflow" && (
        <Modal
          title="Start an engineering workflow"
          subtitle="This demonstrates requirement analysis and approval steps. It does not create a short URL."
          close={() => setModal(null)}
        >
          <form onSubmit={createWorkflow}>
            <label>Scenario
              <select
                name="scenario"
                defaultValue="greenfield"
                onChange={(event) => {
                  const area = event.currentTarget.form?.elements.namedItem("requirement") as HTMLTextAreaElement;
                  if (area) area.value = scenarios[event.target.value as keyof typeof scenarios] ?? "";
                }}
              >
                <option value="greenfield">Greenfield build</option>
                <option value="brownfield">Brownfield change</option>
                <option value="ambiguous">Ambiguous requirement</option>
                <option value="custom">Custom</option>
              </select>
            </label>
            <label>Requirement
              <textarea required name="requirement" defaultValue={scenarios.greenfield} rows={5} />
            </label>
            <label>Risk classification
              <select name="risk_level" defaultValue="medium">
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
              </select>
            </label>
            <button className="primary submit" disabled={busy}>
              {busy ? "Starting…" : "Start workflow"}<ListChecks size={17} />
            </button>
          </form>
        </Modal>
      )}
      {analytics && (
        <Modal title={`Analytics for /${analytics.alias}`} subtitle={`${analytics.total_clicks} total clicks`} close={() => setAnalytics(null)}>
          <div className="analytics-list">
            {analytics.daily.map((day) => <div key={day.date}><span>{day.date}</span><strong>{day.clicks}</strong></div>)}
            {!analytics.daily.length && <p>No clicks have been recorded yet.</p>}
          </div>
        </Modal>
      )}
    </main>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <article className="metric">
      <span className="metric-label">{label}</span>
      <strong>{value}</strong>
      <p>{detail}</p>
    </article>
  );
}

function LinksTable({
  links,
  onDelete,
  onToggle,
  onCopy,
  onAnalytics,
  busy,
  canManage,
}: {
  links: LinkItem[];
  onDelete: (link: LinkItem) => void;
  onToggle: (link: LinkItem) => void;
  onCopy: (link: LinkItem) => void;
  onAnalytics: (link: LinkItem) => void;
  busy: boolean;
  canManage: boolean;
}) {
  return (
    <div className="table-wrap">
      <table className="links-table">
        <thead><tr><th>Name and short URL</th><th>Full destination URL</th><th>Status</th><th>Clicks</th><th>Lifecycle</th><th>Actions</th></tr></thead>
        <tbody>
          {links.map((link) => {
            const shortUrl = `${API_URL}/${link.alias}`;
            const destinationHost = new URL(link.target_url).hostname;
            return (
              <tr key={link.id} className={link.status === "disabled" ? "row-disabled" : ""}>
                <td>
                  <div className="link-name"><strong>{link.title || link.alias}</strong><a href={shortUrl} target="_blank" rel="noreferrer">{shortUrl}<ExternalLink size={12} /></a></div>
                </td>
                <td>
                  <a
                    className="destination-summary"
                    href={link.target_url}
                    target="_blank"
                    rel="noreferrer"
                    title={link.target_url}
                  >
                    <strong>{destinationHost}</strong>
                    <span>{link.target_url}</span>
                  </a>
                </td>
                <td><Badge value={link.status} /></td>
                <td><strong>{link.click_count}</strong><span className="cell-note">visits</span></td>
                <td>
                  <span>Created {new Date(link.created_at).toLocaleDateString()}</span>
                  <span className="cell-note">
                    {link.expires_at
                      ? `Expires ${new Date(link.expires_at).toLocaleString()}`
                      : "No expiration"}
                  </span>
                </td>
                <td>
                  <div className="row-actions">
                    <button className="action-button" title="View analytics" aria-label={`View analytics for ${link.title || link.alias}`} onClick={() => onAnalytics(link)}><Eye size={15} /></button>
                    <button
                      className="action-button"
                      title="Copy short URL"
                      aria-label={`Copy short URL for ${link.title || link.alias}`}
                      onClick={() => onCopy(link)}
                    ><Copy size={15} /></button>
                    <a
                      className="action-button"
                      title="Open destination"
                      aria-label={`Open destination for ${link.title || link.alias}`}
                      href={link.target_url}
                      target="_blank"
                      rel="noreferrer"
                    ><ExternalLink size={15} /></a>
                    <button
                      className={`action-button ${link.status === "active" ? "" : "enable"}`}
                      title={link.status === "active" ? "Disable short URL" : "Enable short URL"}
                      aria-label={`${link.status === "active" ? "Disable" : "Enable"} ${link.title || link.alias}`}
                      disabled={busy || !canManage}
                      onClick={() => onToggle(link)}
                    ><Power size={15} /></button>
                    <button
                      className="action-button danger"
                      title="Delete link"
                      aria-label={`Delete ${link.title || link.alias}`}
                      disabled={busy || !canManage}
                      onClick={() => onDelete(link)}
                    ><Trash2 size={15} /></button>
                  </div>
                </td>
              </tr>
            );
          })}
          {!links.length && (
            <tr><td colSpan={6}><Empty title="No links yet" copy="Select “Create link” to make your first short URL." /></td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function WorkflowTable({ runs }: { runs: Workflow[] }) {
  return (
    <div className="run-list">
      {runs.map((run) => (
        <article key={run.id}>
          <div className="run-icon"><ListChecks size={17} /></div>
          <div className="run-copy"><strong>{run.requirement}</strong><span>{run.scenario} scenario · {run.risk_level} risk</span></div>
          <Badge value={run.status} />
          <div className="run-time"><Clock3 size={14} />{new Date(run.created_at).toLocaleDateString()}</div>
          <Link className="view-workflow" href={`/workflows/${run.id}`}><Eye size={14} />View details</Link>
        </article>
      ))}
      {!runs.length && <Empty title="No workflows yet" copy="Start a workflow to demonstrate the assessment process." />}
    </div>
  );
}

function Empty({ title, copy }: { title: string; copy: string }) {
  return <div className="empty"><strong>{title}</strong><p>{copy}</p></div>;
}

function Modal({ title, subtitle, close, children }: { title: string; subtitle: string; close: () => void; children: React.ReactNode }) {
  return (
    <div className="modal-backdrop" onMouseDown={close}>
      <section className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <button className="modal-close" onClick={close} aria-label="Close"><X size={18} /></button>
        <h2>{title}</h2>
        <p className="modal-subtitle">{subtitle}</p>
        {children}
      </section>
    </div>
  );
}
