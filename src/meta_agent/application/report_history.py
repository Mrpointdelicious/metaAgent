"""Keep stable report references in chat and read images from authorized turns."""

import re
from urllib.parse import parse_qs

REPORT = re.compile(
    r"(?P<base>https?://[A-Za-z0-9.:\[\]-]+/(?:[\w-]+/)*ai/patients/report-artifacts/"
    r"(?P<ref>irego_scope_v2_[a-f0-9]{64})/content)(?:\?(?P<query>[^\s<>\[\]()\"'\\]*))?"
)

class ReportReadUnavailable(RuntimeError):
    """The report backend is temporarily unavailable."""


def original_grant(match):
    values = parse_qs(match["query"] or "").get("access_grant", [])
    if len(values) == 1 and re.fullmatch(r"patient_artifact_v2\.[\w-]+\.[\w-]{43}", values[0]):
        return values[0]
    return ""


def report_turn(user, answer, created_at):
    bindings = {}

    def remember(match):
        grant = original_grant(match)
        if grant:
            bindings[match["ref"]] = grant
            return match["base"]
        return match[0]

    turn = {"user": user, "assistant": REPORT.sub(remember, answer), "created_at": created_at}
    if bindings:
        turn["report_bindings"] = bindings
    return turn


def historical_images(rows, normalize):
    """Expose stable display references, keeping original report ownership data server-side."""
    for row in rows:
        row.pop("report_bindings", None)
        refs = []
        def stable(match):
            if match["ref"] not in refs:
                refs.append(match["ref"])
            return normalize(match["base"])
        row["answer"] = REPORT.sub(stable, row["answer"])
        if refs:
            row["report_artifacts"] = refs


async def read_history_image(rows, ref, message_id, scope, client, conversation_id):
    """Read only the reference emitted in this visible assistant turn."""
    if not message_id or not re.fullmatch(r"irego_scope_v2_[a-f0-9]{64}", ref):
        return None
    for row in rows:
        if row["id"] != message_id or row.get("conversation_id", conversation_id) != conversation_id:
            continue
        for match in REPORT.finditer(row["answer"]):
            if match["ref"] == ref:
                binding = row.get("report_bindings", {}).get(ref) or original_grant(match)
                if binding:
                    return await client.read_report_image(ref, binding, scope, conversation_id)
    return None
