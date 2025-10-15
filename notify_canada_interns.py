#!/usr/bin/env python3
"""
notify_canada_interns.py (robust, fixed)
Fetch README (raw), parse the "Software Engineering Internship Roles" table
(supports Markdown pipe-tables and HTML <table>), filter Canada locations and Age=0d,
and send Discord webhook notifications.

Fixes:
 - Extract links using BeautifulSoup anchors (returns normalized hrefs, no &amp; vs & mismatch)
 - Prefer non-simplify.jobs apply link when multiple anchors exist
 - Sub-rows (company == '↳') inherit last main-row company and application link
 - Normalize/unescape URLs when saving/loading notified.json to dedupe correctly
 - Persist notified.json immediately after each successful notification
"""
import os
import re
import json
import sys
import html as html_module
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup

RAW_README_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
NOTIFIED_STORE = "notified.json"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"


def fetch_readme_raw(url: str = RAW_README_URL, timeout: int = 15) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def find_section_markdown(md: str, section_heading_keywords: List[str]) -> Optional[str]:
    lines = md.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        for kw in section_heading_keywords:
            if kw.lower() in line.lower():
                start_idx = i
                break
        if start_idx is not None:
            break
    if start_idx is None:
        return None
    for j in range(start_idx + 1, len(lines)):
        if lines[j].startswith("#"):
            end_idx = j
            break
    else:
        end_idx = len(lines)
    return "\n".join(lines[start_idx:end_idx])


def extract_first_markdown_table(section_md: str) -> Optional[List[str]]:
    lines = section_md.splitlines()
    table_lines = []
    in_table = False
    for line in lines:
        if line.strip().startswith("|"):
            in_table = True
            table_lines.append(line.rstrip())
        elif in_table:
            break
    return table_lines or None


def parse_markdown_table(table_lines: List[str]) -> List[Dict[str, str]]:
    cleaned = [ln.strip().strip("|").strip() for ln in table_lines if ln.strip()]
    if len(cleaned) < 2:
        return []
    header_row = cleaned[0]
    if re.match(r"^\s*-+\s*(\|\s*-+\s*)*$", cleaned[1]):
        data_rows = cleaned[2:]
    else:
        data_rows = cleaned[1:]
    headers = [h.strip() for h in header_row.split("|")]
    out = []
    for row in data_rows:
        cells = [c.strip() for c in row.split("|")]
        while len(cells) < len(headers):
            cells.append("")
        out.append({headers[i]: cells[i] for i in range(len(headers))})
    return out


def parse_html_table(html_fragment: str) -> Optional[List[Dict[str, str]]]:
    soup = BeautifulSoup(html_fragment, "lxml")
    table = soup.find("table")
    if not table:
        return None
    ths = table.find_all("th")
    if ths:
        headers = [th.get_text(strip=True) for th in ths]
    else:
        first_tr = table.find("tr")
        if not first_tr:
            return None
        headers = [cell.get_text(strip=True) for cell in first_tr.find_all(["td", "th"])]
    rows = []
    all_trs = table.find_all("tr")
    start_idx = 1 if all_trs and all_trs[0].find_all("th") else 0
    for tr in all_trs[start_idx:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        cell_raw = [str(cell) for cell in cells]
        cell_text = [cell.get_text(strip=True) for cell in cells]
        while len(cell_raw) < len(headers):
            cell_raw.append("")
            cell_text.append("")
        # store raw HTML string for each header (keeps anchors), plain text will be derived later
        rows.append({headers[i]: cell_raw[i].strip() for i in range(len(headers))})
    return rows


def strip_html_tags(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return BeautifulSoup(text, "lxml").get_text(strip=True)


def normalize_url(url: str) -> str:
    """
    Basic normalization: unescape HTML entities and strip whitespace.
    (We intentionally avoid heavy URL canonicalization to keep behavior predictable.)
    """
    if not isinstance(url, str):
        return ""
    return html_module.unescape(url).strip()


def extract_link_from_cell(cell_text_or_html: str) -> Optional[str]:
    """
    Prefer extracting href from anchor tags (BeautifulSoup), picking a sensible anchor:
      - Prefer anchors not pointing to simplify.jobs/p (the repo's internal simplify link)
      - Otherwise return the first absolute http(s) href found
    Fallbacks to regex if no <a> tags exist.
    Returned URL is normalized (HTML-unescaped).
    """
    if not cell_text_or_html:
        return None

    try:
        bs = BeautifulSoup(cell_text_or_html, "lxml")
        anchors = bs.find_all("a", href=True)
        if anchors:
            # prefer anchors that don't point to simplify.jobs/p/ (these are "simplify" shortlinks)
            for a in anchors:
                href = a.get("href", "").strip()
                if href and href.lower().startswith(("http://", "https://")) and "simplify.jobs/p/" not in href.lower():
                    return normalize_url(href)
            # otherwise return first absolute href available
            for a in anchors:
                href = a.get("href", "").strip()
                if href and href.lower().startswith(("http://", "https://")):
                    return normalize_url(href)
    except Exception:
        pass

    # Fallback: markdown-style [text](url)
    m = re.search(r'\[.*?\]\((https?://[^\s)]+)\)', cell_text_or_html)
    if m:
        return normalize_url(m.group(1))

    # regex href capture (may contain &amp; etc.)
    m = re.search(r'href=["\'](https?://[^"\']+)["\']', cell_text_or_html)
    if m:
        return normalize_url(html_module.unescape(m.group(1)))

    # fallback: any bare http(s) URL
    m = re.search(r"(https?://[^\s\)\]]+)", cell_text_or_html)
    if m:
        return normalize_url(m.group(1))

    return None


def location_is_canada(location_text: str) -> bool:
    if not location_text:
        return False
    txt = strip_html_tags(location_text).lower()
    return "canada" in txt  # strict: must contain 'canada'


def load_notified(path=NOTIFIED_STORE) -> set:
    """
    Load notified.json if present. Normalize (unescape) stored strings so dedupe matches current normalized URLs.
    """
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(normalize_url(x) for x in data if isinstance(x, str))
        except Exception:
            return set()
    return set()


def save_notified(notified: set, path=NOTIFIED_STORE):
    """
    Save sorted normalized list to disk for deterministic artifacts.
    """
    with open(path, "w") as f:
        json.dump(sorted(list(normalize_url(x) for x in notified)), f, indent=2)


def build_normalized_rows(raw_rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Build rows with both text-only and raw variants:
      - 'Header' => text-only string
      - 'Header_raw' => raw HTML (if provided by HTML parsing) or original string
    """
    normalized = []
    for r in raw_rows:
        row = {}
        for k, v in r.items():
            if isinstance(v, str) and ("<" in v and ">" in v and "href" in v):
                # raw HTML from parse_html_table
                row[f"{k}_raw"] = v
                row[k] = strip_html_tags(v)
            else:
                row[k] = strip_html_tags(v)
                row[f"{k}_raw"] = v
        normalized.append(row)
    return normalized


def send_discord_webhook(webhook_url: str, title: str, description: str, url: Optional[str], fields: List[Dict[str, str]]):
    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "url": url or None,
                "fields": fields,
            }
        ]
    }
    headers = {"Content-Type": "application/json"}
    r = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
    r.raise_for_status()


def main():
    webhook_url = os.getenv(DISCORD_WEBHOOK_ENV)
    if not webhook_url:
        print(f"ERROR: set the {DISCORD_WEBHOOK_ENV} environment variable.", file=sys.stderr)
        sys.exit(2)

    print("Fetching README...", RAW_README_URL)
    md = fetch_readme_raw(RAW_README_URL)

    section = find_section_markdown(md, ["Software Engineering Internship Roles", "Software Engineering"])
    if not section:
        print("Could not find Software Engineering section.", file=sys.stderr)
        sys.exit(1)

    table_lines = extract_first_markdown_table(section)
    rows = []
    if table_lines:
        print("DEBUG: Found markdown table.")
        rows = parse_markdown_table(table_lines)
    else:
        print("DEBUG: Trying HTML parsing of the section.")
        html_rows = parse_html_table(section)
        if html_rows:
            print(f"DEBUG: Found HTML table in section ({len(html_rows)} rows).")
            rows = html_rows
        else:
            m = re.search(r"(<table[\s\S]*?</table>)", md, re.IGNORECASE)
            if m:
                parsed_any = parse_html_table(m.group(1))
                if parsed_any:
                    print(f"DEBUG: Found HTML table anywhere in README ({len(parsed_any)} rows).")
                    rows = parsed_any

    if not rows:
        print("No table rows found.", file=sys.stderr)
        sys.exit(0)

    normalized_rows = build_normalized_rows(rows)

    # derive human headers (exclude *_raw keys)
    headers = list(normalized_rows[0].keys())
    human_headers = [h for h in headers if not h.endswith("_raw")]

    application_header = next((h for h in human_headers if "apply" in h.lower() or "application" in h.lower()), None)
    location_header = next((h for h in human_headers if "location" in h.lower()), None)
    company_header = next((h for h in human_headers if "company" in h.lower()), human_headers[0] if human_headers else "Company")
    role_header = next((h for h in human_headers if "role" in h.lower() or "position" in h.lower()), human_headers[1] if len(human_headers) > 1 else "Role")
    age_header = next((h for h in human_headers if "age" in h.lower()), None)

    notified = load_notified()
    newly_notified = []

    last_valid_link = None
    previous_company = None

    for item in normalized_rows:
        location = item.get(location_header, "")
        age = item.get(age_header, "")

        # Only rows that explicitly say "Canada" in the location text
        if not location_is_canada(location):
            continue

        # Strict age match: accept "0d", "0 d", "0 days" (case-insensitive)
        if not age or not re.search(r"\b0\s*d\b|\b0\s*days?\b", age.lower()):
            continue

        # Prefer raw application cell (preserves anchors)
        app_raw_key = (application_header + "_raw") if application_header else None
        app_raw_val = item.get(app_raw_key or "", "") if app_raw_key else ""
        current_link = extract_link_from_cell(app_raw_val) or extract_link_from_cell(item.get(application_header or "", ""))

        company_text_raw = item.get(company_header, "")
        company_text_stripped = strip_html_tags(company_text_raw).strip() if company_text_raw else ""

        # Track previous main-row company (for sub-rows that display '↳')
        if company_text_stripped and company_text_stripped != "↳":
            previous_company = company_text_stripped

        # If main row and has a link, update last_valid_link (so sub-rows inherit it)
        if company_text_stripped and company_text_stripped != "↳" and current_link:
            last_valid_link = current_link

        # For sub-rows, fall back to last_valid_link if current is missing
        link = current_link or last_valid_link

        # Build dedupe key:
        if link:
            key = normalize_url(link)
        else:
            # If this is a sub-row '↳' and previous_company is available, use it, otherwise company_text_stripped
            comp_for_key = previous_company if company_text_stripped == "↳" and previous_company else company_text_stripped
            role_text = (item.get(role_header, "") or "").strip()
            loc_text = strip_html_tags(location).strip()
            key = normalize_url(f"{comp_for_key}|{role_text}|{loc_text}")

        # Skip if already notified
        if key in notified:
            continue

        # Compose message fields (use previous_company for subrows)
        company_display = previous_company if company_text_stripped == "↳" and previous_company else company_text_stripped
        role_display = strip_html_tags(item.get(role_header, ""))
        loc_display = strip_html_tags(location)
        age_display = age or ""

        fields = [
            {"name": "Company", "value": company_display or "—", "inline": True},
            {"name": "Role", "value": role_display or "—", "inline": True},
            {"name": "Location", "value": loc_display or "—", "inline": True},
            {"name": "Age", "value": age_display or "—", "inline": True},
        ]
        description = f"[Click to apply]({link})" if link else "Application link not found."
        title = f"New Canada Software Engineering Intern — {company_display or 'Unknown'}"

        try:
            send_discord_webhook(webhook_url, title=title, description=description, url=link, fields=fields)
            print(f"Notified: {company_display} — {role_display} — {loc_display}")
            # persist immediately — ensure saved values are normalized
            notified.add(key)
            save_notified(notified)
            newly_notified.append(key)
        except Exception as e:
            print("Failed sending webhook for", company_display, ":", e, file=sys.stderr)

    if newly_notified:
        print(f"Saved {len(newly_notified)} new notified items.")
    else:
        print("No new Canada postings to notify.")


if __name__ == "__main__":
    main()
