"""Stable reason codes shared by invoice execution and review reporting."""

from __future__ import annotations

from invoice_workflow.contracts import Stage


def classify_failure(stage: str, detail: str) -> str:
    normalized = detail.lower()
    if "subject/pdf po mismatch" in normalized:
        return "PO_SUBJECT_PDF_MISMATCH"
    if "subject does not contain one exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "pdf does not contain one exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "expected exactly one pdf po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "invalid exact po number" in normalized:
        return "PO_FORMAT_INVALID"
    if "vin mismatch" in normalized:
        return "VIN_MISMATCH"
    if "authorized amount mismatch" in normalized:
        return "AMOUNT_MISMATCH"
    if "mva mismatch" in normalized:
        return "MVA_MISMATCH"
    if stage == "capture":
        return "CAPTURE_FAILED"
    if stage == str(Stage.OUTLOOK):
        return "OUTLOOK_FAILED"
    return "IDENTITY_FAILED"