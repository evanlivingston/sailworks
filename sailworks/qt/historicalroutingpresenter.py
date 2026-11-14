"""Project a durable historical job record onto the results widgets."""


def summary_text(summary):
    if not summary:
        return ""
    parts = [
        f"{summary.get('complete_years', 0)} complete years",
        f"{summary.get('finished', 0)} finished",
        f"{summary.get('no_finish', 0)} no finish",
        f"{summary.get('failed', 0)} failed",
    ]
    label = summary.get("completion_label")
    if label:
        parts.append(str(label))
    return " · ".join(parts)


def rows(record, outcomes=None):
    outcomes = outcomes or {}
    result = []
    for member in (getattr(record, "members", {}) or {}).values():
        outcome = outcomes.get(str(member.member_id))
        result.append(
            {
                "member_id": str(member.member_id),
                "year": int(member.year),
                "offset_days": int(member.offset_days),
                "departure": str(member.departure_utc).replace("+00:00", ""),
                "outcome": str(getattr(member, "outcome", "") or member.state),
                "duration_hours": getattr(outcome, "duration_hours", None),
                "route_uid": str(getattr(member, "route_uid", "") or ""),
            }
        )
    return result


def strip(record):
    return [
        {
            "year": int(member.year),
            "offset_days": int(member.offset_days),
            "outcome": str(getattr(member, "outcome", "") or member.state),
        }
        for member in (getattr(record, "members", {}) or {}).values()
    ]


def apply(results, record, outcomes=None):
    summary = getattr(record, "spec", {}).get("summary") or {}
    results.set_summary(summary_text(summary))
    results.set_rows(rows(record, outcomes))
    results.set_curve(summary.get("completion_curve", []))
    results.set_strip(strip(record))
    members = getattr(record, "members", {}) or {}
    has_failures = any(
        str(getattr(member, "outcome", "")) == "failed"
        or str(getattr(member, "state", "")) == "failed"
        for member in members.values()
    )
    results.set_actions_enabled(
        has_failures, bool(getattr(record, "excluded_years", ())), True
    )
