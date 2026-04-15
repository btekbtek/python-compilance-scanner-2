# Compliance keyword auditor

A Python CLI that scans ecommerce product pages, runs configurable compliance rules (JSON), optional **custom regex keyword** checks, and generates JSON + PDF reports.

## What it does

- scans one or many product URLs
- can pull URLs from a sitemap (including nested sitemap indexes)
- checks page text and visible links against `rules/state_rules.json`
- optional CLI patterns:
  - `--keyword REGEX` — flag pages where the pattern **matches** (e.g. risky/forbidden terms)
  - `--require-keyword REGEX` — flag pages where the pattern **does not** appear (e.g. required disclosures)
- generates `compliance_report.json` and `compliance_report.pdf` (or paths you pass)
- can upload the PDF to Google Drive using a service account

## Important scope note

This project is a **framework + seeded ruleset**, not a complete 50-state legal oracle.

It ships with example rules (federal-style claim checks and California-oriented hemp / THC patterns). Extend `rules/state_rules.json` with counsel-reviewed rules for production use.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

When examples use `\` at the end of a line, that backslash must be the **last character on that line** (line continuation). If you paste the command as **one long line**, delete every `\` or you can get `error: unrecognized arguments` from stray blank arguments.

### Scan a few URLs (replace with your storefront)

```bash
python3 cbd_thc_compliance_auditor.py \
  --state california \
  --urls \
  "https://example.com/products/sample-tincture" \
  "https://example.com/products/sample-gummies" \
  --out-pdf report.pdf \
  --out-json report.json
```

### Custom keyword (regex) checks

Flag pages that contain a phrase (here: “cures” as a word):

```bash
python3 cbd_thc_compliance_auditor.py --state california \
  --urls "https://example.com/products/sample-tincture" \
  --keyword "\\bcures?\\b"
```

Require an FDA-style disclaimer line to appear:

```bash
python3 cbd_thc_compliance_auditor.py --state california \
  --urls "https://example.com/products/sample-tincture" \
  --require-keyword "not been evaluated by the fda"
```

Patterns are matched against **normalized** page text (whitespace collapsed, case-insensitive regex flags).

### Scan from sitemap

```bash
python3 cbd_thc_compliance_auditor.py \
  --state california \
  --sitemap https://example.com/sitemap.xml \
  --sitemap-limit 25
```

### Scan from a text file

Create `urls.txt` with one URL per line, then:

```bash
python3 cbd_thc_compliance_auditor.py \
  --state california \
  --url-file urls.txt
```

## Google Drive upload

1. Create a Google Cloud service account
2. Enable Google Drive API
3. Download the JSON key
4. Share the destination Drive folder with the service account email
5. Run:

```bash
python3 cbd_thc_compliance_auditor.py \
  --state california \
  --url-file urls.txt \
  --google-service-account /absolute/path/service-account.json \
  --drive-folder-id YOUR_DRIVE_FOLDER_ID
```

The script prints the Drive file link after upload.

## Rule format

Rules live in `rules/state_rules.json`.

Each rule has:

- `id`
- `title`
- `severity`
- `states`
- `products`
- `check`
- `fail_summary`
- `recommendation`
- `sources`

Supported check types:

- `missing_any_text`
- `missing_any_link`
- `risky_text_present`
- `risky_html_present`

## Good next improvements

- add separate rule profiles for hemp ingestibles vs topicals vs inhalables
- add more states with official source links
- parse JSON-LD product data
- scan collection pages and cart pages
- screenshot capture for evidence packs
- CSV export for merchant remediation queue
