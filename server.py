#!/usr/bin/env python3
"""
JIRA Dashboard server.
Tries PAT auth first; falls back to static data.json if auth fails.

Setup (optional — enables live data):
  export JIRA_TOKEN=your-personal-access-token

Run:
  python3 server.py

Open:
  http://localhost:7432
"""
import http.server, urllib.request, urllib.parse, urllib.error
import json, os, re, ssl, threading, math, subprocess, sys
from pathlib import Path
from datetime import date, datetime, timedelta

DEBUG = False
JIRA   = os.environ.get("JIRA_URL", "https://jira.example.com").rstrip("/")
PORT   = 7432
FIELDS = ["summary","status","priority",
          "customfield_10403",   # story points
          "customfield_11501",   # sprint
          "customfield_12003",   # epic link
          "parent",              # parent issue (new hierarchy - may include epic)
          "subtasks",
          "components","created","updated","resolutiondate"]

TEAMMATES = [
    {"name": "Alice Johnson",  "user": "ajohnson"},
    {"name": "Bob Smith",      "user": "bsmith"},
    {"name": "Carol Lee",      "user": "clee"},
    {"name": "David Chen",     "user": "dchen"},
    {"name": "Eva Martinez",   "user": "emartinez"},
]

def ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def parse_sprint(raw):
    s = str(raw)
    m = re.search(r'name=([^,\]]+)', s)
    name = m.group(1).strip() if m else None
    m2 = re.search(r'state=(\w+)', s)
    state = m2.group(1).strip() if m2 else None
    return name, state

def active_sprint(sprint_list):
    if not sprint_list:
        return None
    best = None
    for raw in sprint_list:
        name, state = parse_sprint(raw)
        if state == "ACTIVE":
            return name
        best = name
    return best

def normalize_status(raw):
    return {"in progress":"In Progress","closed":"Closed","done":"Closed",
            "resolved":"Closed","on hold":"On Hold","blocked":"On Hold"}.get(raw.lower(), "Assigned")

def jira_search(jql, token, max_results=50):
    params = urllib.parse.urlencode({"jql":jql,"maxResults":max_results,"fields":",".join(FIELDS)})
    req = urllib.request.Request(f"{JIRA}/rest/api/2/search?{params}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=15) as r:
        return json.loads(r.read())

def debug_ticket_fields(ticket_key, token):
    """Fetch ALL fields for a specific ticket to debug which field contains epic."""
    try:
        req = urllib.request.Request(f"{JIRA}/rest/api/2/issue/{ticket_key}")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, context=ssl_ctx(), timeout=15) as r:
            data = json.loads(r.read())
            fields = data.get("fields", {})
            
            print(f"\n  DEBUG: All fields for {ticket_key}:")
            print(f"  Summary: {fields.get('summary', 'N/A')}")
            print(f"  Component: {fields.get('components', [])}")
            print(f"\n  Checking for epic-related fields:")
            
            # Check all custom fields that might contain epic
            for key, value in fields.items():
                if 'epic' in key.lower() or key.startswith('customfield_'):
                    if value and value != [] and value != {}:
                        print(f"    {key}: {value}")
            
            # Also check parent
            if fields.get('parent'):
                print(f"    parent: {fields.get('parent')}")
            
            return fields
    except Exception as e:
        print(f"  Error fetching debug info: {e}")
        return None

def fetch_epic_name(epic_key, token):
    """Fetch the summary/name of an epic by its key."""
    try:
        req = urllib.request.Request(f"{JIRA}/rest/api/2/issue/{epic_key}?fields=summary")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, context=ssl_ctx(), timeout=10) as r:
            data = json.loads(r.read())
            return data["fields"].get("summary", epic_key)
    except Exception as e:
        print(f"    Warning: Could not fetch epic {epic_key}: {e}")
        return epic_key  # Return key as fallback

def issue_to_dict(issue, token=None, epic_cache=None):
    """Convert JIRA issue to dict. Optionally fetch epic name if token provided."""
    f = issue["fields"]
    sp_val = f.get("customfield_10403")
    components = f.get("components") or []
    
    # Check for epic in parent field (new JIRA hierarchy) or epic link (legacy)
    epic_link = ""
    epic_name = ""
    
    # Try parent field first (newer JIRA)
    parent = f.get("parent")
    if parent and isinstance(parent, dict):
        epic_link = parent.get("key", "")
        # Parent field already includes summary, no need to fetch
        epic_name = parent.get("fields", {}).get("summary", "") if "fields" in parent else ""
        if DEBUG and epic_link:
            print(f"    {issue['key']}: Found epic in parent field: {epic_link} = {epic_name}")
    
    # Fall back to custom field epic link
    if not epic_link:
        epic_link = f.get("customfield_12003") or ""
        
        if DEBUG and epic_link:
            print(f"    {issue['key']}: Found epic in customfield_12003: {epic_link}")
        
        # Only fetch epic name if we have a link and no name yet
        if epic_link and token:
            # Check cache first
            if epic_cache is not None and epic_link in epic_cache:
                epic_name = epic_cache[epic_link]
            else:
                epic_name = fetch_epic_name(epic_link, token)
                if epic_cache is not None:
                    epic_cache[epic_link] = epic_name
    
    # For closed tickets without resolutiondate, use updated as fallback
    closed_date = ""
    if f.get("resolutiondate"):
        closed_date = f.get("resolutiondate")[:10]
    elif normalize_status(f.get("status",{}).get("name","")) == "Closed":
        # Fallback to updated date for closed tickets without resolution date
        closed_date = (f.get("updated") or "")[:10]
    
    # Subtask progress
    subtasks = f.get("subtasks") or []
    subtask_total = len(subtasks)
    subtask_done = sum(1 for st in subtasks
                       if (st.get("fields",{}).get("status",{}).get("name","") or "").lower()
                       in ("closed","done","resolved"))

    # Missing fields detector
    missing = []
    if sp_val is None:
        missing.append("Story Points")
    if not components:
        missing.append("Component")
    if not epic_link:
        missing.append("Epic")

    return {
        "key":       issue["key"],
        "summary":   f.get("summary",""),
        "status":    normalize_status(f.get("status",{}).get("name","")),
        "priority":  (f.get("priority") or {}).get("name","P5"),
        "sp":        int(sp_val) if sp_val is not None else None,
        "sprint":    active_sprint(f.get("customfield_11501") or []),
        "component": components[0].get("name","") if components else "",
        "epic":      epic_link,
        "epicName":  epic_name,
        "created":   (f.get("created") or "")[:10],
        "updated":   (f.get("updated") or "")[:10],
        "closed":    closed_date,
        "subtaskCount": subtask_total,
        "subtasksDone": subtask_done,
        "missingFields": missing,
    }

def sprint_stats(tickets):
    counts = {"Closed":0,"In Progress":0,"Assigned":0,"On Hold":0}
    sp     = {"Closed":0,"In Progress":0,"Assigned":0,"On Hold":0}
    for t in tickets:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1
        if t["sp"] is not None:
            sp[s] = sp.get(s, 0) + t["sp"]
    return {
        "total":        len(tickets),          "totalSP":      sum(sp.values()),
        "closed":       counts["Closed"],      "closedSP":     sp["Closed"],
        "inProgress":   counts["In Progress"], "inProgressSP": sp["In Progress"],
        "assigned":     counts["Assigned"],    "assignedSP":   sp["Assigned"],
        "onHold":       counts["On Hold"],     "onHoldSP":     sp["On Hold"],
    }

def fetch_live(token):
    epic_cache = {}  # Cache epic names to avoid duplicate API calls
    
    print("  → Fetching open sprint…")
    open_data    = jira_search("assignee = currentUser() AND sprint in openSprints()", token, 50)
    open_tickets = [issue_to_dict(i, token, epic_cache) for i in open_data.get("issues",[])]
    current_name = next((t["sprint"] for t in open_tickets if t["sprint"]), "Current")

    print("  → Fetching closed sprints…")
    closed_data  = jira_search(
        "assignee = currentUser() AND sprint in closedSprints() ORDER BY updated DESC", token, 60)
    all_closed   = [issue_to_dict(i, token, epic_cache) for i in closed_data.get("issues",[])]

    buckets = {}
    for t in all_closed:
        buckets.setdefault(t["sprint"] or "Unknown", []).append(t)

    # Filter out current sprint when selecting prev sprints, but keep the bucket for allTickets
    sorted_names  = sorted([k for k in buckets.keys() if k != current_name], reverse=True)
    prev1_name    = sorted_names[0] if len(sorted_names) > 0 else None
    prev2_name    = sorted_names[1] if len(sorted_names) > 1 else None
    prev1_tickets = buckets.get(prev1_name, [])
    prev2_tickets = buckets.get(prev2_name, [])

    return {
        "lastUpdated": str(date.today()),
        "stale": False,
        "sprints": {
            "current": {"name":current_name, "label":"Active",  "state":"open",
                        "tickets":open_tickets,  **sprint_stats(open_tickets)},
            "prev1":   {"name":prev1_name or "—", "label":"Closed", "state":"closed",
                        "tickets":prev1_tickets, **sprint_stats(prev1_tickets)},
            "prev2":   {"name":prev2_name or "—", "label":"Closed", "state":"closed",
                        "tickets":prev2_tickets, **sprint_stats(prev2_tickets)},
        }
    }

def fetch_history(token, months=6):
    """Fetch all tickets resolved/updated by current user in past N months."""
    print(f"  → Fetching {months}-month history…")
    epic_cache = {}
    # Use 'resolved' to get tickets actually closed in window; fall back via updated
    jql = (
        f"assignee = currentUser() AND "
        f"(resolved >= -{months*30}d OR (status = Closed AND updated >= -{months*30}d)) "
        f"ORDER BY resolved DESC"
    )
    data = jira_search(jql, token, 500)
    tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
    closed = [t for t in tickets if t["status"] == "Closed"]

    # Aggregate stats
    total_closed = len(closed)
    total_sp = sum(t["sp"] or 0 for t in closed)
    by_priority = {"P1":0, "P2":0, "P3":0, "P5":0}
    by_component = {}
    by_epic = {}
    by_sprint = {}
    for t in closed:
        p = t["priority"][:2] if t["priority"] else "P5"
        if p not in by_priority: p = "P5"
        by_priority[p] += 1
        comp = t["component"] or "Uncategorized"
        by_component[comp] = by_component.get(comp, 0) + 1
        if t["epicName"]:
            by_epic[t["epicName"]] = by_epic.get(t["epicName"], 0) + 1
        if t["sprint"]:
            s = by_sprint.setdefault(t["sprint"], {"closed":0, "sp":0})
            s["closed"] += 1
            s["sp"] += t["sp"] or 0

    # Sort sprints by name (chronological-ish)
    sprint_list = [{"name":n, **v} for n,v in sorted(by_sprint.items(), reverse=True)]
    avg_sp_per_sprint = round(total_sp / len(sprint_list)) if sprint_list else 0
    avg_closed_per_sprint = round(total_closed / len(sprint_list), 1) if sprint_list else 0

    # Top P1/P2 highlights
    p1_tickets = [{"key":t["key"], "summary":t["summary"], "closed":t["closed"]}
                  for t in closed if t["priority"].startswith("P1")]
    p2_tickets = [{"key":t["key"], "summary":t["summary"], "closed":t["closed"]}
                  for t in closed if t["priority"].startswith("P2")]

    # Top components/epics
    top_components = sorted(by_component.items(), key=lambda x: -x[1])[:10]
    top_epics      = sorted(by_epic.items(),      key=lambda x: -x[1])[:10]

    # Velocity history per sprint (for prediction)
    velocity_history = []
    for s in sprint_list:
        velocity_history.append({"sprint": s["name"], "sp": s["sp"], "closed": s["closed"]})

    # SLA metrics: avg resolution time for P1/P2
    sla = {"p1Avg": 0, "p2Avg": 0, "p1Breaches": 0, "p2Breaches": 0,
           "p1Target": 3, "p2Target": 7}
    p1_days, p2_days = [], []
    for t in closed:
        if not t["created"] or not t["closed"]:
            continue
        try:
            created_dt = datetime.strptime(t["created"], "%Y-%m-%d")
            closed_dt = datetime.strptime(t["closed"], "%Y-%m-%d")
            days = (closed_dt - created_dt).days
        except ValueError:
            continue
        if t["priority"].startswith("P1"):
            p1_days.append(days)
        elif t["priority"].startswith("P2"):
            p2_days.append(days)
    if p1_days:
        sla["p1Avg"] = round(sum(p1_days) / len(p1_days), 1)
        sla["p1Breaches"] = sum(1 for d in p1_days if d > sla["p1Target"])
    if p2_days:
        sla["p2Avg"] = round(sum(p2_days) / len(p2_days), 1)
        sla["p2Breaches"] = sum(1 for d in p2_days if d > sla["p2Target"])

    # Cycle time by week (for trend line)
    cycle_by_week = {}
    for t in closed:
        if not t["created"] or not t["closed"]:
            continue
        try:
            created_dt = datetime.strptime(t["created"], "%Y-%m-%d")
            closed_dt = datetime.strptime(t["closed"], "%Y-%m-%d")
            days = (closed_dt - created_dt).days
            week_key = closed_dt.strftime("%Y-W%W")
        except ValueError:
            continue
        cycle_by_week.setdefault(week_key, []).append(days)
    # Filter outliers using IQR method
    all_cycle_days = [d for ds in cycle_by_week.values() for d in ds]
    if all_cycle_days:
        all_cycle_days.sort()
        q1 = all_cycle_days[len(all_cycle_days) // 4]
        q3 = all_cycle_days[3 * len(all_cycle_days) // 4]
        iqr = q3 - q1
        upper_bound = q3 + 1.5 * iqr
        cap = max(upper_bound, 30)  # never cap below 30 days
    else:
        cap = 999
    cycle_time_trend = []
    for w, ds in sorted(cycle_by_week.items()):
        filtered = [d for d in ds if d <= cap]
        if filtered:
            cycle_time_trend.append({"week": w, "avgDays": round(sum(filtered) / len(filtered), 1), "count": len(filtered), "excluded": len(ds) - len(filtered)})

    # Performance export summary
    perf_export = {
        "period": f"Last {months} months",
        "totalClosed": total_closed,
        "totalSP": total_sp,
        "byPriority": by_priority,
        "avgSpPerSprint": avg_sp_per_sprint,
        "topComponents": [{"name":n, "count":c} for n,c in top_components[:5]],
        "topEpics": [{"name":n, "count":c} for n,c in top_epics[:5]],
        "p1Count": len(p1_tickets),
        "p2Count": len(p2_tickets),
        "sla": sla,
    }

    return {
        "lastUpdated": str(date.today()),
        "months": months,
        "totalClosed": total_closed,
        "totalSP": total_sp,
        "byPriority": by_priority,
        "sprintCount": len(sprint_list),
        "avgSpPerSprint": avg_sp_per_sprint,
        "avgClosedPerSprint": avg_closed_per_sprint,
        "sprints": sprint_list,
        "topComponents": [{"name":n, "count":c} for n,c in top_components],
        "topEpics":      [{"name":n, "count":c} for n,c in top_epics],
        "p1Tickets": p1_tickets,
        "p2Tickets": p2_tickets,
        "velocityHistory": velocity_history,
        "slaMetrics": sla,
        "cycleTimeByWeek": cycle_time_trend,
        "performanceExport": perf_export,
    }

def fetch_teammate(user, token):
    """Fetch current sprint stats for one teammate."""
    try:
        epic_cache = {}  # Cache for this teammate's epics
        jql = f'assignee = "{user}" AND sprint in openSprints()'
        data = jira_search(jql, token, 100)
        tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
        stats = sprint_stats(tickets)

        # P1 count in current sprint
        p1_count = sum(1 for t in tickets if t["priority"].startswith("P1"))

        # closed all-time (last 60 tickets across closed sprints)
        closed_jql = f'assignee = "{user}" AND sprint in closedSprints() ORDER BY updated DESC'
        closed_data = jira_search(closed_jql, token, 60)
        closed_tickets = [issue_to_dict(i, token, epic_cache) for i in closed_data.get("issues", [])]
        all_time_closed = sum(1 for t in closed_tickets if t["status"] == "Closed")

        return {**stats, "p1Current": p1_count, "allTimeClosed": all_time_closed, "error": False}
    except Exception as e:
        print(f"    ✗ Failed to fetch {user}: {e}")
        return {"total":0,"totalSP":0,"closed":0,"closedSP":0,"inProgress":0,
                "inProgressSP":0,"assigned":0,"assignedSP":0,"onHold":0,"onHoldSP":0,
                "p1Current":0,"allTimeClosed":0,"error": True}

def fetch_team(token):
    """Fetch current sprint stats for all teammates in parallel."""
    print("  → Fetching team data…")
    results = [None] * len(TEAMMATES)

    def worker(idx, teammate):
        results[idx] = fetch_teammate(teammate["user"], token)

    threads = []
    for i, tm in enumerate(TEAMMATES):
        t = threading.Thread(target=worker, args=(i, tm))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    team_list = [
        {"name": TEAMMATES[i]["name"], "user": TEAMMATES[i]["user"], **results[i]}
        for i in range(len(TEAMMATES))
    ]

    # Leaderboard rankings
    valid = [t for t in team_list if not t.get("error")]
    for metric in ["closedSP", "closed", "allTimeClosed", "p1Current"]:
        ranked = sorted(valid, key=lambda x: -(x.get(metric) or 0))
        for rank, t in enumerate(ranked):
            t[f"rank_{metric}"] = rank + 1

    return {
        "lastUpdated": str(date.today()),
        "stale": False,
        "team": team_list,
    }

def fetch_myself(token):
    """Fetch the current user's display name and username from Jira."""
    req = urllib.request.Request(f"{JIRA}/rest/api/2/myself")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=10) as r:
        data = json.loads(r.read())
    return {
        "displayName": data.get("displayName", ""),
        "username": data.get("name", ""),
    }

def fetch_changes(token, since_date):
    """Fetch tickets changed since a given date."""
    jql = (f'assignee = currentUser() AND updated >= "{since_date}" ORDER BY updated DESC')
    data = jira_search(jql, token, 100)
    epic_cache = {}
    tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
    new_tickets = [t for t in tickets if t["created"] >= since_date]
    closed_since = [t for t in tickets if t["status"] == "Closed" and t["closed"] >= since_date]
    status_changes = [t for t in tickets if t["updated"] >= since_date and t not in new_tickets]
    return {
        "since": since_date,
        "newTickets": new_tickets,
        "closedSince": closed_since,
        "statusChanges": status_changes,
        "totalChanges": len(tickets),
    }

def fetch_standup(token):
    """Generate standup data: yesterday's done, today's WIP, blockers."""
    today = date.today()
    if today.weekday() == 0:  # Monday — look back to Friday
        since = (today - timedelta(days=3)).isoformat()
    else:
        since = (today - timedelta(days=1)).isoformat()
    # Use day before 'since' with AFTER so that tickets closed ON 'since' are included
    after_date = (datetime.strptime(since, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    epic_cache = {}
    results = {"yesterday": [], "today": [], "blockers": []}
    errors = []

    def _fetch(key, jql):
        try:
            data = jira_search(jql, token, 30)
            results[key] = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
        except Exception as e:
            errors.append(str(e))

    threads = [
        threading.Thread(target=_fetch, args=("yesterday",
            f'assignee = currentUser() AND status changed TO (Closed, Done, Resolved) AFTER "{after_date}"')),
        threading.Thread(target=_fetch, args=("today",
            'assignee = currentUser() AND status = "In Progress"')),
        threading.Thread(target=_fetch, args=("blockers",
            'assignee = currentUser() AND (status = "On Hold" OR status = "Blocked")')),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results

def fetch_stale(token):
    """Find tickets still open but assigned to closed sprints."""
    jql = ('assignee = currentUser() AND sprint in closedSprints() '
           'AND status NOT IN (Closed, Done, Resolved) ORDER BY updated ASC')
    data = jira_search(jql, token, 100)
    epic_cache = {}
    tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
    return {
        "staleTickets": tickets,
        "totalCount": len(tickets),
        "totalSP": sum(t["sp"] or 0 for t in tickets),
    }

def fetch_comments(token, ticket_key):
    """Fetch latest comments for a ticket."""
    try:
        req = urllib.request.Request(
            f"{JIRA}/rest/api/2/issue/{ticket_key}/comment?orderBy=-created&maxResults=5")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, context=ssl_ctx(), timeout=10) as r:
            data = json.loads(r.read())
        comments = []
        for c in data.get("comments", []):
            comments.append({
                "author": c.get("author", {}).get("displayName", "Unknown"),
                "body": c.get("body", "")[:500],
                "created": (c.get("created") or "")[:16],
            })
        return {"ticket": ticket_key, "comments": comments}
    except Exception as e:
        return {"ticket": ticket_key, "comments": [], "error": str(e)}

def fetch_mentions(token, limit=20):
    """Find tickets where current user is mentioned in comments (last 30d)."""
    jql = ('comment ~ currentUser() AND updated >= -30d '
           'AND assignee != currentUser() ORDER BY updated DESC')
    try:
        data = jira_search(jql, token, limit)
        epic_cache = {}
        tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
        return {"mentions": tickets, "count": len(tickets)}
    except Exception:
        return {"mentions": [], "count": 0}

def fetch_duplicates(token):
    """Detect potential duplicate tickets in current sprint using Jaccard similarity."""
    jql = 'assignee = currentUser() AND sprint in openSprints()'
    data = jira_search(jql, token, 100)
    tickets = []
    for i in data.get("issues", []):
        summary = i["fields"].get("summary", "")
        words = set(re.findall(r'\w{3,}', summary.lower()))
        tickets.append({"key": i["key"], "summary": summary, "words": words})

    pairs = []
    for a in range(len(tickets)):
        for b in range(a + 1, len(tickets)):
            wa, wb = tickets[a]["words"], tickets[b]["words"]
            if not wa or not wb:
                continue
            jaccard = len(wa & wb) / len(wa | wb)
            if jaccard > 0.5:
                pairs.append({
                    "ticketA": tickets[a]["key"],
                    "summaryA": tickets[a]["summary"],
                    "ticketB": tickets[b]["key"],
                    "summaryB": tickets[b]["summary"],
                    "similarity": round(jaccard * 100),
                })
    pairs.sort(key=lambda x: -x["similarity"])
    return {"pairs": pairs, "count": len(pairs)}

def fetch_manager_teammate(user, token):
    """Fetch current sprint tickets and recently closed tickets for one teammate."""
    try:
        epic_cache = {}
        current_jql = f'assignee = "{user}" AND sprint in openSprints()'
        current_data = jira_search(current_jql, token, 100)
        current_tickets = [issue_to_dict(i, token, epic_cache) for i in current_data.get("issues", [])]

        closed_jql = (f'assignee = "{user}" AND status in (Closed, Done, Resolved) '
                      f'AND resolved >= -30d ORDER BY resolved DESC')
        closed_data = jira_search(closed_jql, token, 50)
        closed_tickets = [issue_to_dict(i, token, epic_cache) for i in closed_data.get("issues", [])]

        stats = sprint_stats(current_tickets)
        return {
            **stats,
            "currentTickets": current_tickets,
            "recentlyClosed": closed_tickets,
            "error": False,
        }
    except Exception as e:
        print(f"    x Failed to fetch manager data for {user}: {e}")
        return {
            "total": 0, "totalSP": 0, "closed": 0, "closedSP": 0,
            "inProgress": 0, "inProgressSP": 0, "assigned": 0, "assignedSP": 0,
            "onHold": 0, "onHoldSP": 0,
            "currentTickets": [], "recentlyClosed": [], "error": True,
        }

def fetch_manager(token):
    """Fetch detailed ticket data for all teammates (manager view)."""
    print("  -> Fetching manager view data...")
    results = [None] * len(TEAMMATES)

    def worker(idx, teammate):
        results[idx] = fetch_manager_teammate(teammate["user"], token)

    threads = []
    for i, tm in enumerate(TEAMMATES):
        t = threading.Thread(target=worker, args=(i, tm))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    team_list = [
        {"name": TEAMMATES[i]["name"], "user": TEAMMATES[i]["user"], **results[i]}
        for i in range(len(TEAMMATES))
    ]

    return {
        "lastUpdated": str(date.today()),
        "stale": False,
        "team": team_list,
    }

def fetch_manager_changes(token, since_date):
    """Fetch ticket changes for all teammates since a given date."""
    print(f"  -> Fetching manager changes since {since_date}...")
    results = [None] * len(TEAMMATES)

    def worker(idx, teammate):
        try:
            jql = (f'assignee = "{teammate["user"]}" AND updated >= "{since_date}" '
                   f'ORDER BY updated DESC')
            data = jira_search(jql, token, 50)
            epic_cache = {}
            tickets = [issue_to_dict(i, token, epic_cache) for i in data.get("issues", [])]
            new_tickets = [t for t in tickets if t["created"] >= since_date]
            closed_since = [t for t in tickets if t["status"] == "Closed" and (t["closed"] or "") >= since_date]
            status_changes = [t for t in tickets if t["updated"] >= since_date and t not in new_tickets]
            results[idx] = {
                "newTickets": new_tickets,
                "closedSince": closed_since,
                "statusChanges": status_changes,
            }
        except Exception as e:
            print(f"    x Failed to fetch changes for {teammate['user']}: {e}")
            results[idx] = {"newTickets": [], "closedSince": [], "statusChanges": []}

    threads = []
    for i, tm in enumerate(TEAMMATES):
        t = threading.Thread(target=worker, args=(i, tm))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    team_changes = []
    for i in range(len(TEAMMATES)):
        r = results[i]
        total = len(r["newTickets"]) + len(r["closedSince"]) + len(r["statusChanges"])
        if total > 0:
            team_changes.append({
                "name": TEAMMATES[i]["name"],
                "user": TEAMMATES[i]["user"],
                **r,
                "totalChanges": total,
            })

    team_changes.sort(key=lambda x: -x["totalChanges"])
    return {
        "since": since_date,
        "stale": False,
        "team": team_changes,
        "totalChanges": sum(c["totalChanges"] for c in team_changes),
    }

def load_static():
    data = json.loads(Path("data.json").read_text())
    data["stale"] = True
    return data

class Handler(http.server.SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Jira-Token, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/update":
            self._serve_update()
        else:
            self._json(404, {"error": "Not found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        routes = {
            "/api/tickets":    self._serve_tickets,
            "/api/team":       self._serve_team,
            "/api/history":    self._serve_history,
            "/api/changes":    self._serve_changes,
            "/api/standup":    self._serve_standup,
            "/api/stale":      self._serve_stale,
            "/api/comments":   self._serve_comments,
            "/api/mentions":   self._serve_mentions,
            "/api/duplicates": self._serve_duplicates,
            "/api/config":     self._serve_config,
            "/api/myself":     self._serve_myself,
            "/api/manager":    self._serve_manager,
            "/api/manager-changes": self._serve_manager_changes,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            super().do_GET()

    def _serve_tickets(self):
        token = self.headers.get("X-Jira-Token", "").strip() or os.environ.get("JIRA_TOKEN","").strip()
        stale_reason = None
        data = None

        if token:
            try:
                data = fetch_live(token)
                print(f"  ✓ Live data fetched — {data['sprints']['current']['total']} current sprint tickets")
            except urllib.error.HTTPError as e:
                stale_reason = f"JIRA error {e.code} — showing cached data"
                print(f"  ✗ {stale_reason}")
            except Exception as e:
                stale_reason = f"Connection error — showing cached data ({e})"
                print(f"  ✗ {stale_reason}")
        else:
            stale_reason = "No JIRA_TOKEN set — showing cached data"
            print(f"  ℹ  {stale_reason}")

        if data is None:
            try:
                data = load_static()
                data["staleReason"] = stale_reason
            except Exception as e:
                self._json(500, {"error": f"No data available: {e}"})
                return

        self._json(200, data)

    def _serve_team(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_team(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_history(self):
        token = self._require_token()
        if not token:
            return
        try:
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            months = int(qs.get("months", ["6"])[0])
        except Exception:
            months = 6
        try:
            self._json(200, fetch_history(token, months=months))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _require_token(self):
        token = self.headers.get("X-Jira-Token", "").strip()
        if not token:
            token = os.environ.get("JIRA_TOKEN", "").strip()
        if not token:
            self._json(200, {"stale": True, "staleReason": "No JIRA token configured. Open Settings to add your token."})
            return None
        return token

    def _serve_update(self):
        repo_dir = Path(__file__).parent
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=repo_dir, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if result.returncode != 0:
                self._json(500, {"error": result.stderr.strip() or "git pull failed"})
                return
            already_up_to_date = "Already up to date" in output
            self._json(200, {
                "updated": not already_up_to_date,
                "message": output,
                "restarting": not already_up_to_date,
            })
            if not already_up_to_date:
                threading.Thread(target=self._restart_server, daemon=True).start()
        except subprocess.TimeoutExpired:
            self._json(500, {"error": "git pull timed out"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    @staticmethod
    def _restart_server():
        import time
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _serve_config(self):
        self._json(200, {"jiraUrl": JIRA})

    def _serve_myself(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_myself(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_manager(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_manager(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_manager_changes(self):
        token = self._require_token()
        if not token:
            return
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        since = qs.get("since", [(date.today() - timedelta(days=1)).isoformat()])[0]
        try:
            self._json(200, fetch_manager_changes(token, since))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_changes(self):
        token = self._require_token()
        if not token:
            return
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        since = qs.get("since", [(date.today() - timedelta(days=1)).isoformat()])[0]
        try:
            self._json(200, fetch_changes(token, since))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_standup(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_standup(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_stale(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_stale(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_comments(self):
        token = self._require_token()
        if not token:
            return
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        ticket = qs.get("ticket", [None])[0]
        if not ticket:
            self._json(400, {"error": "Missing ?ticket=KEY parameter"})
            return
        try:
            self._json(200, fetch_comments(token, ticket))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_mentions(self):
        token = self._require_token()
        if not token:
            return
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        limit = int(qs.get("limit", ["20"])[0])
        try:
            self._json(200, fetch_mentions(token, limit))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_duplicates(self):
        token = self._require_token()
        if not token:
            return
        try:
            self._json(200, fetch_duplicates(token))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent)
    token = os.environ.get("JIRA_TOKEN","").strip()
    print("=" * 48)
    print("  JIRA Sprint Dashboard")
    print("=" * 48)
    print(f"  JIRA URL: {JIRA}")
    if token:
        print(f"  ✓  PAT found — will attempt live data")
    else:
        print("  ℹ  No JIRA_TOKEN set — using cached data")
        print("     To enable live refresh:")
        print("     export JIRA_URL=https://your-jira-instance.com")
        print("     export JIRA_TOKEN=your-personal-access-token")
    print(f"\n  Open: http://localhost:{PORT}")
    print("=" * 48)
    try:
        http.server.HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
