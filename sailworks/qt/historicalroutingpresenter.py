"""Project a durable historical job record onto the results widgets."""


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


def apply(results, record, outcomes=None):
    results.set_rows(rows(record, outcomes))
    members = getattr(record, "members", {}) or {}
    has_failures = any(
        str(getattr(member, "outcome", "")) == "failed"
        or str(getattr(member, "state", "")) == "failed"
        for member in members.values()
    )
    results.set_actions_enabled(
        has_failures, bool(getattr(record, "excluded_years", ())), True
    )
