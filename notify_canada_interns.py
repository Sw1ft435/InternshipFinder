#!/usr/bin/env python3
"""
notify_canada_interns.py (robust)
Fetch README (raw), parse the "Software Engineering Internship Roles" table
(supports Markdown pipe-tables and HTML <table>), filter Canada locations and Age=0d,
and send Discord webhook notifications.
"""
import os, re, json, sys
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

def parse_markdown_table(table_lines: List[str]) -> List[Dict[str,str]]:
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

def parse_html_table(html_fragment: str) -> Optional[List[Dict[str,str]]]:
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
        headers = [cell.get_text(strip=True) for cell in first_tr.find_all(["td","th"])]
    rows = []
    all_trs = table.find_all("tr")
    start_idx = 1 if all_trs and all_trs[0].find_all("th") else 0
    for tr in all_trs[start_idx:]:
        cells = tr.find_all(["td","th"])
        if not cells:
            continue
        cell_texts = [str(cell) for cell in cells]
        while len(cell_texts) < len(headers):
            cell_texts.append("")
        rows.append({headers[i]: cell_texts[i].strip() for i in range(len(headers))})
    return rows

def strip_html_tags(text: str) -> str:
    return BeautifulSoup(text, "lxml").get_text(strip=True)

def extract_link_from_cell(cell_text: str) -> Optional[str]:
    if not cell_text:
        return None
    m = re.search(r'\[.*?\]\((https?://[^\s)]+)\)', cell_text)
    if m:
        return m.group(1)
    m = re.search(r'href=["\'](https?://[^"\']+)["\']', cell_text)
    if m:
        return m.group(1)
    m = re.search(r"(https?://[^\s\)\]]+)", cell_text)
    if m:
        return m.group(1)
    return None

def location_is_canada(location_text: str) -> bool:
    if not location_text:
        return False
    txt = strip_html_tags(location_text).lower()
    return "canada" in txt

def load_notified(path=NOTIFIED_STORE) -> set:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
                return set(data if isinstance(data, list) else [])
        except Exception:
            return set()
    return set()

def save_notified(notified: set, path=NOTIFIED_STORE):
    with open(path, "w") as f:
        json.dump(sorted(list(notified)), f, indent=2)

def send_discord_webhook(webhook_url: str, title: str, description: str, url: Optional[str], fields: List[Dict[str,str]]):
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

def normalize_key_for_posting(item: Dict[str,str]) -> str:
    app = item.get("Application","") or item.get("Apply","") or ""
    link = extract_link_from_cell(app)
    if link:
        return link
    company = strip_html_tags(item.get("Company","")).strip()
    role = strip_html_tags(item.get("Role","")).strip()
    loc = strip_html_tags(item.get("Location","")).strip()
    return f"{company}|{role}|{loc}"

def main():
    webhook_url = os.getenv(DISCORD_WEBHOOK_ENV)
    if not webhook_url:
        print(f"ERROR: set the {DISCORD_WEBHOOK_ENV} environment variable.", file=sys.stderr)
        sys.exit(2)

    print("Fetching README...", RAW_README_URL)
    md = fetch_readme_raw(RAW_README_URL)

    section = find_section_markdown(md, ["Software Engineering Internship Roles"])
    if not section:
        print("Could not find Software Engineering section.", file=sys.stderr)
        sys.exit(1)

    table_lines = extract_first_markdown_table(section)
    rows = []
    if table_lines:
        rows = parse_markdown_table(table_lines)
    else:
        html_rows = parse_html_table(section)
        if html_rows:
            rows = html_rows
        else:
            m = re.search(r"(<table[\s\S]*?</table>)", md, re.IGNORECASE)
            if m:
                parsed_any = parse_html_table(m.group(1))
                if parsed_any:
                    rows = parsed_any

    if not rows:
        print("No table rows found.", file=sys.stderr)
        sys.exit(0)

    normalized_rows = []
    for r in rows:
        normalized_rows.append({k.strip(): strip_html_tags(v).strip() if isinstance(v,str) else str(v).strip() for k,v in r.items()})

    headers = list(normalized_rows[0].keys())
    application_header = next((h for h in headers if "apply" in h.lower() or "application" in h.lower()), None)
    location_header = next((h for h in headers if "location" in h.lower()), None)
    company_header = next((h for h in headers if "company" in h.lower()), headers[0] if headers else "Company")
    role_header = next((h for h in headers if "role" in h.lower() or "position" in h.lower()), headers[1] if len(headers) > 1 else "Role")
    age_header = next((h for h in headers if "age" in h.lower()), None)

    notified = load_notified()
    newly_notified = []

    # Track last valid link to handle sub-rows
    last_valid_link = None

    for item in normalized_rows:
        location = item.get(location_header, "")
        age = item.get(age_header, "")
        if not location_is_canada(location):
            continue
        if not age or not re.match(r"0\s*d", age.lower()):
            continue

        # Use current link or fallback to last valid main-row link
        current_link = extract_link_from_cell(item.get(application_header,""))
        if item.get(company_header,"").strip() != "↳" and current_link:
            last_valid_link = current_link
        link = current_link or last_valid_link

        key = normalize_key_for_posting(item)
        if key in notified:
            continue
        company = item.get(company_header,"")
        role = item.get(role_header,"")
        fields = [
            {"name": "Company", "value": company or "—", "inline": True},
            {"name": "Role", "value": role or "—", "inline": True},
            {"name": "Location", "value": location or "—", "inline": True},
            {"name": "Age", "value": age or "—", "inline": True},
        ]
        description = f"[Click to apply]({link})" if link else "Application link not found."
        title = f"New Canada Software Engineering Intern — {company}"
        try:
            send_discord_webhook(webhook_url, title=title, description=description, url=link, fields=fields)
            print(f"Notified: {company} — {role} — {location}")
            notified.add(key)
            newly_notified.append(key)
        except Exception as e:
            print("Failed sending webhook for", company, ":", e, file=sys.stderr)

    if newly_notified:
        save_notified(notified)
        print(f"Saved {len(newly_notified)} new notified items.")
    else:
        print("No new Canada postings to notify.")

if __name__ == "__main__":
    main()
