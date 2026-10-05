from prometheus_client import Counter, Histogram

WORKFLOW_RUNS = Counter(
    "workflow_runs_total",
    "Workflow terminal transitions",
    ["scenario", "status"],
)
WORKFLOW_DURATION = Histogram(
    "workflow_end_to_end_duration_seconds",
    "Workflow time from first execution to completion",
    ["scenario"],
)
TASK_ATTEMPTS = Counter(
    "workflow_task_attempts_total",
    "Workflow task attempts",
    ["node", "provider", "status"],
)
RECOVERY_ACTIONS = Counter(
    "workflow_recovery_actions_total",
    "Workflow recovery operations",
    ["action", "outcome"],
)
RECOVERY_DURATION = Histogram(
    "workflow_recovery_duration_seconds",
    "Time from the latest failed attempt to successful workflow completion",
    ["scenario"],
)
