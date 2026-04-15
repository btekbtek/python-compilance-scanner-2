#!/usr/bin/env python3
"""
Compliance auditor for ecommerce product pages: URL-based scans with JSON rules
plus optional custom keyword (regex) checks.

What it does
------------
- Scans one or many product URLs (or a sitemap).
- Applies rule-based checks (e.g. federal / state hemp–CBD patterns in ``rules/state_rules.json``).
- Optionally flags pages that match (or omit) user-supplied regex patterns via ``--keyword`` /
  ``--require-keyword``.
- Writes a PDF report with findings, severity, and source-backed recommendations.
- Optionally uploads the PDF to Google Drive using a service account.

Important
---------
This is a compliance triage tool, not legal advice.
State cannabis/hemp laws change often. Review the rules JSON regularly with counsel.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

DEFAULT_UA = "ContentComplianceScanner/1.0 (+regulatory keyword checks)"
TIMEOUT = 20

SEVERITY_SCORE = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}

@dataclass
class PageScan:
    url: str
    status_code: int
    title: str
    text: str
    html: str
    links: List[str] = field(default_factory=list)

@dataclass
class Finding:
    rule_id: str
    title: str
    severity: str
    status: str
    summary: str
    recommendation: str
    applies_to: List[str]
    sources: List[Dict[str, str]]

def load_rules(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def fetch_url(url: str) -> PageScan:
    resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=TIMEOUT)
    soup = BeautifulSoup(resp.text, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else url
    text = " ".join(soup.get_text(" ", strip=True).split())
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if href:
            links.append(href.lower())
    return PageScan(
        url=url,
        status_code=resp.status_code,
        title=title,
        text=text,
        html=resp.text,
        links=links,
    )

_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
_SITEMAP_NSMAP = {"sm": _SITEMAP_NS}


def _split_xml_tag(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _is_likely_product_page(url: str) -> bool:
    """Heuristic: skip storefront root and non-PDP URLs when harvesting product sitemaps."""
    try:
        path = urlparse(url).path.lower().rstrip("/")
    except ValueError:
        return False
    if not path or path == "/":
        return False
    return "/products/" in f"{path}/"


def _parse_sitemap_document(content: bytes) -> tuple[list[str], bool]:
    """Return (all loc hrefs, is_sitemap_index)."""
    root = ET.fromstring(content)
    locs = []
    for el in root.findall(".//sm:loc", _SITEMAP_NSMAP):
        if el.text:
            u = el.text.strip()
            if u:
                locs.append(u)
    return locs, _split_xml_tag(root.tag) == "sitemapindex"


def urls_from_sitemap(sitemap_url: str, limit: int = 100) -> List[str]:
    """
    Collect page URLs from a sitemap URL or sitemap index (nested sitemaps).

    Root ``sitemap.xml`` files often list child sitemaps (products, pages, …)
    rather than individual URLs. Those child documents are fetched until
    ``limit`` page URLs are collected. Product sitemaps are tried first when
    present (typical for Shopify-style indexes).
    """
    collected: list[str] = []
    seen_pages: set[str] = set()
    visited: set[str] = set()
    queue: deque[str] = deque([sitemap_url])

    while queue and len(collected) < limit:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        resp = requests.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=TIMEOUT)
        resp.raise_for_status()
        locs, is_index = _parse_sitemap_document(resp.content)
        if is_index:
            ordered = sorted(
                locs,
                key=lambda u: (
                    0 if "product" in u.lower() else 1,
                    u,
                ),
            )
            queue.extend(ordered)
        else:
            for u in locs:
                if not _is_likely_product_page(u):
                    continue
                if u not in seen_pages and len(collected) < limit:
                    seen_pages.add(u)
                    collected.append(u)

    return collected

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()

def any_regex(patterns: Iterable[str], haystack: str) -> bool:
    for pat in patterns:
        if re.search(pat, haystack, re.I | re.S):
            return True
    return False

def detect_product_type(page: PageScan, explicit: str) -> str:
    if explicit != "auto":
        return explicit
    t = normalize_text(page.text)
    if re.search(r"\b(delta[- ]?8|delta[- ]?9|delta[- ]?10|thc|tetrahydrocannabinol|marijuana|cannabis)\b", t):
        if re.search(r"\bcbd\b", t):
            return "mixed"
        return "thc"
    if re.search(r"\bcbd\b|\bhemp\b", t):
        return "cbd"
    return "unknown"

def evaluate_rule(rule: Dict[str, Any], page: PageScan, state: str, product_type: str) -> Finding | None:
    applies_states = rule.get("states", ["all"])
    applies_products = rule.get("products", ["all"])
    if "all" not in applies_states and state not in applies_states:
        return None
    if "all" not in applies_products and product_type not in applies_products:
        return None

    text_l = normalize_text(page.text)
    html_l = page.html.lower()
    links_joined = "\n".join(page.links)

    check_type = rule["check"]["type"]
    matched = False
    status = "pass"
    summary = rule.get("pass_summary", "No issue detected by this heuristic.")

    if check_type == "missing_any_text":
        matched = not any_regex(rule["check"]["patterns"], text_l)
        if matched:
            status = "flag"
            summary = rule["fail_summary"]
    elif check_type == "missing_any_link":
        matched = not any_regex(rule["check"]["patterns"], links_joined)
        if matched:
            status = "flag"
            summary = rule["fail_summary"]
    elif check_type == "risky_text_present":
        matched = any_regex(rule["check"]["patterns"], text_l)
        if matched:
            status = "flag"
            summary = rule["fail_summary"]
    elif check_type == "risky_html_present":
        matched = any_regex(rule["check"]["patterns"], html_l)
        if matched:
            status = "flag"
            summary = rule["fail_summary"]
    else:
        raise ValueError(f"Unsupported check type: {check_type}")

    return Finding(
        rule_id=rule["id"],
        title=rule["title"],
        severity=rule["severity"],
        status=status,
        summary=summary,
        recommendation=rule.get("recommendation", ""),
        applies_to=applies_products,
        sources=rule.get("sources", []),
    )

def _pattern_preview(pat: str, max_len: int = 48) -> str:
    s = pat if len(pat) <= max_len else pat[: max_len - 1] + "…"
    return s


def validate_regex_list(patterns: List[str], option_name: str) -> None:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            raise SystemExit(f"Invalid regex for {option_name}: {p!r} ({exc})")


def findings_from_cli_keywords(
    page: PageScan,
    risky_patterns: List[str],
    required_patterns: List[str],
) -> List[Finding]:
    """User-supplied regexes: flag risky terms when present; flag when required terms are absent."""
    text_l = normalize_text(page.text)
    out: List[Finding] = []
    for i, pat in enumerate(risky_patterns):
        if re.search(pat, text_l, re.I | re.S):
            preview = _pattern_preview(pat)
            out.append(
                Finding(
                    rule_id=f"cli-keyword-risky-{i}",
                    title=f"Keyword / pattern matched: {preview}",
                    severity="medium",
                    status="flag",
                    summary=f"The page text matches your custom pattern (regex): {_pattern_preview(pat, 120)}",
                    recommendation="Review whether this language is acceptable for your jurisdiction and sales channel; "
                    "remove or rephrase if it creates compliance exposure.",
                    applies_to=["all"],
                    sources=[],
                )
            )
    for i, pat in enumerate(required_patterns):
        if not re.search(pat, text_l, re.I | re.S):
            preview = _pattern_preview(pat)
            out.append(
                Finding(
                    rule_id=f"cli-keyword-required-{i}",
                    title=f"Required pattern missing: {preview}",
                    severity="high",
                    status="flag",
                    summary=f"No match for required pattern (regex): {_pattern_preview(pat, 120)}",
                    recommendation="Add the required disclosure or language so the page meets your policy.",
                    applies_to=["all"],
                    sources=[],
                )
            )
    return out


def scan_page(
    page: PageScan,
    rules: Dict[str, Any],
    state: str,
    explicit_product_type: str,
    *,
    keyword_risky: List[str] | None = None,
    keyword_required: List[str] | None = None,
) -> Dict[str, Any]:
    product_type = detect_product_type(page, explicit_product_type)
    findings: List[Finding] = []
    for rule in rules["rules"]:
        finding = evaluate_rule(rule, page, state, product_type)
        if finding:
            findings.append(finding)
    findings.extend(findings_from_cli_keywords(page, keyword_risky or [], keyword_required or []))

    findings_sorted = sorted(findings, key=lambda f: (-SEVERITY_SCORE.get(f.severity, 0), f.title.lower()))
    flagged = [f for f in findings_sorted if f.status == "flag"]
    risk_score = sum(SEVERITY_SCORE.get(f.severity, 0) for f in flagged)
    if risk_score >= 10:
        verdict = "High risk"
    elif risk_score >= 5:
        verdict = "Moderate risk"
    elif risk_score >= 1:
        verdict = "Low risk"
    else:
        verdict = "No heuristic flags"

    return {
        "url": page.url,
        "title": page.title,
        "http_status": page.status_code,
        "product_type": product_type,
        "verdict": verdict,
        "risk_score": risk_score,
        "findings": [f.__dict__ for f in findings_sorted],
    }

def build_pdf(report: Dict[str, Any], output_pdf: Path) -> None:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle",
        parent=styles["Title"],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#183153"),
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="SectionHead",
        parent=styles["Heading2"],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#183153"),
        spaceBefore=10,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="Small",
        parent=styles["BodyText"],
        fontSize=9,
        leading=12,
    ))
    styles.add(ParagraphStyle(
        name="Meta",
        parent=styles["BodyText"],
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#4a5568"),
    ))
    styles.add(ParagraphStyle(
        name="Finding",
        parent=styles["BodyText"],
        fontSize=10,
        leading=13,
        alignment=TA_LEFT,
    ))

    doc = SimpleDocTemplate(
        str(output_pdf),
        pagesize=LETTER,
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
    )
    story = []

    summary = report["summary"]
    story.append(Paragraph("Compliance audit report", styles["ReportTitle"]))
    story.append(Paragraph(
        f"Jurisdiction profile: <b>{report['state'].title()}</b> &nbsp;&nbsp; "
        f"Pages scanned: <b>{summary['pages_scanned']}</b> &nbsp;&nbsp; "
        f"Flagged findings: <b>{summary['flagged_findings']}</b>",
        styles["Meta"],
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "This report is a rule-based triage tool. It helps identify likely page-level compliance gaps, "
        "but it is not legal advice and does not replace counsel review.",
        styles["BodyText"],
    ))
    story.append(Spacer(1, 10))

    overall_items = [
        f"High risk pages: {summary['high_risk_pages']}",
        f"Moderate risk pages: {summary['moderate_risk_pages']}",
        f"Low/no-risk pages: {summary['low_risk_pages']}",
        f"Rules library version: {report.get('rules_version', 'n/a')}",
    ]
    story.append(Paragraph("Executive summary", styles["SectionHead"]))
    story.append(ListFlowable(
        [ListItem(Paragraph(item, styles["BodyText"])) for item in overall_items],
        bulletType="bullet",
        leftIndent=16,
    ))
    story.append(Spacer(1, 10))

    for page in report["pages"]:
        flagged = [f for f in page["findings"] if f["status"] == "flag"]
        story.append(Paragraph(page["title"], styles["SectionHead"]))
        story.append(Paragraph(
            f"<b>URL:</b> {page['url']}<br/>"
            f"<b>HTTP status:</b> {page['http_status']}<br/>"
            f"<b>Detected product type:</b> {page['product_type']}<br/>"
            f"<b>Verdict:</b> {page['verdict']} (score: {page['risk_score']})",
            styles["Meta"],
        ))
        story.append(Spacer(1, 6))

        if not flagged:
            story.append(Paragraph("No heuristic flags on the active rules for this page.", styles["BodyText"]))
            story.append(Spacer(1, 12))
            continue

        blocks = []
        for finding in flagged:
            sev_color = {
                "critical": "#9b1c1c",
                "high": "#b45309",
                "medium": "#1f4e79",
                "low": "#4a5568",
                "info": "#4a5568",
            }.get(finding["severity"], "#4a5568")
            source_lines = []
            for s in finding.get("sources", []):
                title = s.get("title", "Source")
                url = s.get("url", "")
                source_lines.append(f"• {title}: {url}")
            sources_html = "<br/>".join(source_lines) if source_lines else "• No source metadata attached."
            block = [
                Paragraph(
                    f"<b><font color='{sev_color}'>{finding['severity'].upper()}</font></b> — <b>{finding['title']}</b>",
                    styles["Finding"],
                ),
                Paragraph(finding["summary"], styles["Finding"]),
                Paragraph(f"<b>Recommended fix:</b> {finding['recommendation']}", styles["Finding"]),
                Paragraph(f"<b>Sources:</b><br/>{sources_html}", styles["Small"]),
                Spacer(1, 10),
            ]
            blocks.append(KeepTogether(block))
        story.extend(blocks)
        story.append(Spacer(1, 8))

    story.append(PageBreak())
    story.append(Paragraph("Method notes", styles["SectionHead"]))
    story.append(Paragraph(
        "Heuristics in this scanner check for missing disclosures, missing lab-result links, risky claim language, "
        "and California-specific THC warning patterns. Some requirements cannot be validated from a webpage alone, "
        "such as batch-linked COAs, manufacturing licenses, lab test authenticity, or package-only warnings.",
        styles["BodyText"],
    ))

    doc.build(story)

def upload_to_drive(pdf_path: Path, folder_id: str | None, service_account_json: str | None) -> str:
    if not service_account_json:
        raise ValueError("Google Drive upload requested, but --google-service-account was not provided.")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = service_account.Credentials.from_service_account_file(
        service_account_json,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    service = build("drive", "v3", credentials=creds)
    metadata = {"name": pdf_path.name}
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaFileUpload(str(pdf_path), mimetype="application/pdf")
    created = service.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()
    return created.get("webViewLink") or created["id"]

def make_report(pages: List[Dict[str, Any]], state: str, rules_version: str) -> Dict[str, Any]:
    flagged_findings = sum(1 for p in pages for f in p["findings"] if f["status"] == "flag")
    summary = {
        "pages_scanned": len(pages),
        "flagged_findings": flagged_findings,
        "high_risk_pages": sum(1 for p in pages if p["verdict"] == "High risk"),
        "moderate_risk_pages": sum(1 for p in pages if p["verdict"] == "Moderate risk"),
        "low_risk_pages": sum(1 for p in pages if p["verdict"] in ("Low risk", "No heuristic flags")),
    }
    return {
        "state": state,
        "rules_version": rules_version,
        "summary": summary,
        "pages": pages,
    }

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan product pages for compliance (JSON rules + optional regex keyword checks) and write PDF/JSON reports."
    )
    parser.add_argument("--state", required=True, help="State profile key from rules JSON, e.g. california")
    parser.add_argument("--product-type", default="auto", choices=["auto", "cbd", "thc", "mixed", "unknown"])
    parser.add_argument("--rules", default="rules/state_rules.json", help="Path to rules JSON")
    parser.add_argument("--urls", nargs="*", help="One or more URLs to scan")
    parser.add_argument("--url-file", help="Plain-text file with one URL per line")
    parser.add_argument("--sitemap", help="Sitemap URL to pull URLs from")
    parser.add_argument("--sitemap-limit", type=int, default=50)
    parser.add_argument("--out-pdf", default="compliance_report.pdf")
    parser.add_argument("--out-json", default="compliance_report.json")
    parser.add_argument("--drive-folder-id", default=None)
    parser.add_argument("--google-service-account", default=None)
    parser.add_argument(
        "--keyword",
        action="append",
        default=None,
        metavar="REGEX",
        help="Regex matched against normalized page text; repeat for multiple. Flags a finding when a match is found.",
    )
    parser.add_argument(
        "--require-keyword",
        action="append",
        default=None,
        metavar="REGEX",
        help="Regex that must appear on the page; repeat for multiple. Flags a finding when no match is found.",
    )
    args = parser.parse_args()

    keyword_risky = args.keyword or []
    keyword_required = args.require_keyword or []
    validate_regex_list(keyword_risky, "--keyword")
    validate_regex_list(keyword_required, "--require-keyword")

    rules_path = Path(args.rules)
    rules = load_rules(rules_path)

    state_keys = {s["key"] for s in rules.get("states", [])}
    if args.state not in state_keys:
        raise SystemExit(f"Unknown state profile '{args.state}'. Available: {', '.join(sorted(state_keys))}")

    urls = list(args.urls or [])
    if args.url_file:
        with open(args.url_file, "r", encoding="utf-8") as f:
            urls.extend(
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            )
    if args.sitemap:
        urls.extend(urls_from_sitemap(args.sitemap, limit=args.sitemap_limit))

    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    urls = deduped

    if not urls:
        raise SystemExit("No URLs supplied. Use --urls, --url-file, or --sitemap.")

    scanned_pages = []
    for url in urls:
        try:
            page = fetch_url(url)
            scanned_pages.append(
                scan_page(
                    page,
                    rules,
                    args.state,
                    args.product_type,
                    keyword_risky=keyword_risky,
                    keyword_required=keyword_required,
                )
            )
            print(f"Scanned: {url}", file=sys.stderr)
        except Exception as exc:
            scanned_pages.append({
                "url": url,
                "title": urlparse(url).netloc or url,
                "http_status": 0,
                "product_type": args.product_type,
                "verdict": "Scan failed",
                "risk_score": 0,
                "findings": [{
                    "rule_id": "scan_error",
                    "title": "Scan failure",
                    "severity": "info",
                    "status": "flag",
                    "summary": f"Could not scan this page: {exc}",
                    "recommendation": "Retry the URL manually and inspect any bot blocking, timeout, or redirect issue.",
                    "applies_to": ["all"],
                    "sources": [],
                }],
            })

    report = make_report(scanned_pages, args.state, rules.get("version", "unknown"))
    out_json = Path(args.out_json)
    out_pdf = Path(args.out_pdf)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    build_pdf(report, out_pdf)

    print(f"JSON report written to: {out_json}")
    print(f"PDF report written to: {out_pdf}")

    if args.drive_folder_id or args.google_service_account:
        link = upload_to_drive(out_pdf, args.drive_folder_id, args.google_service_account)
        print(f"Google Drive upload: {link}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
