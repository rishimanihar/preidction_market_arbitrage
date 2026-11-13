"""Conservative exception-table proposals from the two rules texts."""

from __future__ import annotations

import re

from pmarb.models import ExceptionRow, Market

_OVERTIME = re.compile(r"\b(overtime|extra innings)\b", re.IGNORECASE)
_SHOOTOUT = re.compile(r"\b(shootout|penalty shootout)\b", re.IGNORECASE)
_TIE = re.compile(r"\b(tie|tied|draw)\b", re.IGNORECASE)
_POSTPONEMENT = re.compile(r"\b(postpon(?:ed|ement)|reschedul(?:ed|ing))\b", re.IGNORECASE)
_CANCELLATION = re.compile(r"\b(cancel(?:led|ed|lation)?|abandon(?:ed|ment)?)\b", re.IGNORECASE)
_FAIR_PRICE = re.compile(r"\bfair\s+price\b", re.IGNORECASE)


def _ties_pay_half(text: str) -> bool:
    amount = r"(?:\$\s*0\.50|0\.50\s*(?:dollars?)?|50\s*cents?)"
    action = r"(?:pay|pays|paid|settle|settles|resolved?|receive|receives|return|returns)"
    # Decimal points are part of payout amounts, not sentence boundaries.
    for clause in re.split(r"(?<!\d)\.|\.(?!\d)|[;\n]", text):
        if not re.search(r"\b(?:tie|tied|draw)\b", clause, re.IGNORECASE):
            continue
        if re.search(r"\b(?:no|not|never|unless|excluding|except)\b", clause, re.IGNORECASE):
            continue
        if re.search(
            rf"\b(?:tie|tied|draw)\b.*\b{action}\b.*{amount}"
            rf"|{amount}.*\b{action}\b.*\b(?:tie|tied|draw)\b",
            clause,
            re.IGNORECASE,
        ):
            return True
    return False


def _window_days(text: str) -> int | None:
    if re.search(r"\b48\s*hours?\b", text, re.IGNORECASE):
        return 2
    if re.search(r"\btwo\s+days?\b", text, re.IGNORECASE):
        return 2
    if re.search(r"\b24\s*hours?\b|\bone\s+day\b", text, re.IGNORECASE):
        return 1
    if re.search(r"\b72\s*hours?\b|\bthree\s+days?\b", text, re.IGNORECASE):
        return 3
    return None


def default_exception_table(
    kalshi_market: Market,
    polyus_market: Market,
) -> tuple[ExceptionRow, ...]:
    """Suggest settlement exception rows; uncertain parity always stays false."""
    ktext = kalshi_market.rules_text or ""
    ptext = polyus_market.rules_text or ""
    kkey, pkey = kalshi_market.market_key, polyus_market.market_key
    rows: list[ExceptionRow] = []

    k_ot, p_ot = bool(_OVERTIME.search(ktext)), bool(_OVERTIME.search(ptext))
    if k_ot or p_ot:
        rows.append(
            ExceptionRow(
                outcome_case="overtime_or_extra_innings",
                payouts={
                    kkey: "overtime/extra innings mentioned" if k_ot else "unknown",
                    pkey: "overtime/extra innings mentioned" if p_ot else "unknown",
                },
                preserves_unit_payout=False,
            )
        )
    k_so, p_so = bool(_SHOOTOUT.search(ktext)), bool(_SHOOTOUT.search(ptext))
    if k_so or p_so:
        rows.append(
            ExceptionRow(
                outcome_case="shootout",
                payouts={
                    kkey: "shootout mentioned" if k_so else "unknown",
                    pkey: "shootout mentioned" if p_so else "unknown",
                },
                preserves_unit_payout=False,
            )
        )

    k_tie, p_tie = bool(_TIE.search(ktext)), bool(_TIE.search(ptext))
    if k_tie or p_tie:
        k_half, p_half = _ties_pay_half(ktext), _ties_pay_half(ptext)
        rows.append(
            ExceptionRow(
                outcome_case="tie",
                payouts={
                    kkey: "0.50" if k_half else "tie payout unknown",
                    pkey: "0.50" if p_half else "tie payout unknown",
                },
                preserves_unit_payout=bool(k_half and p_half),
            )
        )

    k_post, p_post = bool(_POSTPONEMENT.search(ktext)), bool(_POSTPONEMENT.search(ptext))
    if k_post or p_post:
        kd, pd = _window_days(ktext), _window_days(ptext)
        rows.append(
            ExceptionRow(
                outcome_case="postponement_window",
                payouts={kkey: _window_label(ktext, kd), pkey: _window_label(ptext, pd)},
                preserves_unit_payout=False,
            )
        )

    k_cancel, p_cancel = bool(_CANCELLATION.search(ktext)), bool(_CANCELLATION.search(ptext))
    if k_cancel or p_cancel:
        k_fair, p_fair = bool(_FAIR_PRICE.search(ktext)), bool(_FAIR_PRICE.search(ptext))
        rows.append(
            ExceptionRow(
                outcome_case="cancellation_or_abandonment",
                payouts={
                    kkey: "venue fair price" if k_cancel and k_fair else "unknown",
                    pkey: "venue fair price" if p_cancel and p_fair else "unknown",
                },
                # Equal labels do not establish equal fair prices: each venue sets its own.
                preserves_unit_payout=False,
            )
        )

    if not rows:
        rows.append(
            ExceptionRow(
                outcome_case="unclassified_exception",
                payouts={kkey: "unknown", pkey: "unknown"},
                preserves_unit_payout=False,
            )
        )
    return tuple(rows)


def _window_label(text: str, days: int | None) -> str:
    if days is None:
        return "postponement window unknown"
    if re.search(r"\b48\s*hours?\b", text, re.IGNORECASE):
        return "48 hours"
    if re.search(r"\btwo\s+days?\b", text, re.IGNORECASE):
        return "two days"
    if days == 1:
        return "24 hours / one day"
    return f"{days} days"
