#!/usr/bin/env python3
"""
Secret Santa Emailer (no CLI flags needed)

- Finds the most recent YYYY.txt in assignments_dir (default: this script's folder)
- Reads giver->receiver pairs
- Loads emails from emails_file (default: emails.txt next to this script)
- Loads SMTP + behavior from sendercredentials.txt (same folder by default)
- Defaults to DRY-RUN; set `send = true` in sendercredentials.txt to actually send

sendercredentials.txt keys (case-insensitive):
  smtp_server    = smtp.gmail.com
  smtp_port      = 587
  username       = youraddress@gmail.com
  password       = <16-char app password>
  from           = "Secret Santa <youraddress@gmail.com>"   # optional; defaults to username
  subject        = Secret Santa {year}: your assignment 🎁   # optional; {year} will be formatted
  use_starttls   = true                                      # optional; default true if port 587 else false
  send           = false                                     # optional; default false (dry-run)
  assignments_dir= /path/to/folder                           # optional; default this script's folder
  emails_file    = /path/to/emails.txt                       # optional; default emails.txt next to script
"""

from __future__ import annotations
import re
import sys
from pathlib import Path
from typing import Dict, Tuple
from email.message import EmailMessage
import smtplib
import ssl

# ----------------- Utilities & Parsing -----------------

ASSIGNMENT_LINE_RE = re.compile(r"\s*(?:->|:|,|-)\s*")

def canon(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

def script_dir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path(".").resolve()

def _parse_bool(s: str | None, default: bool) -> bool:
    if s is None:
        return default
    return s.strip().lower() in {"1", "true", "yes", "on"}

def load_credentials(path: Path) -> dict:
    """
    Load key=value pairs from sendercredentials.txt.
    Required: smtp_server, smtp_port, username
    Optional: password, from, subject, use_starttls, send, assignments_dir, emails_file
    """
    if not path.exists():
        sys.exit(f"[ERROR] Credentials file not found: {path}")

    cfg: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip().lower()
            v = v.strip().strip('"').strip("'")
            cfg[k] = v

    server = cfg.get("smtp_server")
    port_s = cfg.get("smtp_port")
    username = cfg.get("username")

    if not server or not port_s or not username:
        sys.exit("[ERROR] sendercredentials.txt must include smtp_server, smtp_port, and username.")

    try:
        port = int(port_s)
    except ValueError:
        sys.exit("[ERROR] smtp_port must be an integer.")

    use_starttls = _parse_bool(cfg.get("use_starttls"), default=(port == 587))
    from_addr = cfg.get("from") or username
    subject_tmpl = cfg.get("subject") or "Secret Santa {year}: your assignment 🎁"
    send_flag = _parse_bool(cfg.get("send"), default=False)

    # Optional paths
    sdir = cfg.get("assignments_dir")
    emails_file = cfg.get("emails_file")

    return {
        "server": server,
        "port": port,
        "username": username,
        "password": cfg.get("password"),  # may be None in dry-run
        "use_starttls": use_starttls,
        "from_addr": from_addr,
        "subject_tmpl": subject_tmpl,
        "send": send_flag,
        "assignments_dir": sdir,
        "emails_file": emails_file,
    }

def find_latest_year_file(directory: Path) -> Tuple[int, Path]:
    candidates: list[Tuple[int, Path]] = []
    for p in directory.glob("*.txt"):
        m = re.fullmatch(r"(\d{4})\.txt", p.name)
        if m:
            candidates.append((int(m.group(1)), p))
    if not candidates:
        sys.exit(f"[ERROR] No year files like '2025.txt' found in {directory}")
    year, path = max(candidates, key=lambda t: t[0])
    return year, path

def load_assignments(path: Path) -> Dict[str, str]:
    """Returns mapping giver(display)->receiver(display)"""
    mapping: Dict[str, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = ASSIGNMENT_LINE_RE.split(s)
            if len(parts) != 2:
                continue
            giver, receiver = parts[0].strip(), parts[1].strip()
            mapping[giver] = receiver
    if not mapping:
        sys.exit(f"[ERROR] No assignments parsed from {path}")
    return mapping

def load_emails(path: Path) -> Dict[str, Tuple[str, str]]:
    """
    Returns dict: canonical_name -> (display_name, email)
    Accepts: 'Name, email' or pipe/tab; ignores comments/blank lines.
    """
    if not path.exists():
        sys.exit(f"[ERROR] Emails file not found: {path}")
    book: Dict[str, Tuple[str, str]] = {}
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = re.split(r"[,\|\t]", s)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) < 2:
                print(f"[WARN] Skipping line (need 'Name, Email'): {s}")
                continue
            name, email = parts[0], parts[1]
            book[canon(name)] = (name, email)
    if not book:
        sys.exit(f"[ERROR] No emails parsed from {path}")
    return book

def build_giver_email_map(assignments: Dict[str, str], email_book: Dict[str, Tuple[str, str]]) -> Dict[str, Tuple[str, str, str]]:
    """
    Returns: giver_display -> (giver_email, giver_display, receiver_display)
    Warns for missing emails.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    missing = []
    for giver_display, receiver_display in assignments.items():
        key = canon(giver_display)
        rec = email_book.get(key)
        if not rec:
            missing.append(giver_display)
            continue
        _, giver_email = rec
        out[giver_display] = (giver_email, giver_display, receiver_display)
    if missing:
        print("[WARN] No email for the following givers (will be skipped):")
        for m in missing:
            print(f"  - {m}")
    return out

def make_message(from_addr: str, to_addr: str, giver_name: str, receiver_name: str, year: int, subject: str) -> EmailMessage:
    body = (
        f"Hey {giver_name},\n\n"
        f"Welcome to the Secret Santa gift exchange for {year}! "
        f"This year you have been assigned to gift to {receiver_name}.\n\n"
        f"If you want help coming up with ideas here is the link to everyones wish lists, https://docs.google.com/document/d/1vflkXkzUwnWFdUOJLYx-AUdgH5nmC55qJmJercyIJHY/edit?usp=sharing\n\n"
        f"Remember our price cap is $30 but many spouses are willing to subsidize more expensive gifts\n"
        f"(Please keep this a secret 🤫)\n\n"
    )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    return msg

def send_all(messages: list[EmailMessage], server: str, port: int, username: str, password: str, use_starttls: bool = True) -> None:
    if use_starttls:
        with smtplib.SMTP(server, port) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            for m in messages:
                smtp.send_message(m)
    else:
        with smtplib.SMTP_SSL(server, port, context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            for m in messages:
                smtp.send_message(m)

# ----------------- Main (no CLI flags) -----------------

def main():
    here = script_dir()

    creds_path = here / "sendercredentials.txt"
    creds = load_credentials(creds_path)

    # Resolve paths
    assignments_dir = Path(creds["assignments_dir"]).expanduser().resolve() if creds["assignments_dir"] else here
    emails_path = Path(creds["emails_file"]).expanduser().resolve() if creds["emails_file"] else (here / "emails.txt")

    year, ypath = find_latest_year_file(assignments_dir)
    assignments = load_assignments(ypath)
    email_book = load_emails(emails_path)
    roster = build_giver_email_map(assignments, email_book)
    if not roster:
        sys.exit("[ERROR] No mailable givers found. Check emails.txt names vs assignment names.")

    subject = creds["subject_tmpl"].format(year=year)
    from_addr = creds["from_addr"]

    # Build messages
    messages: list[EmailMessage] = []
    for giver_display, (giver_email, giver_name, receiver_name) in roster.items():
        messages.append(
            make_message(
                from_addr=from_addr,
                to_addr=giver_email,
                giver_name=giver_name,
                receiver_name=receiver_name,
                year=year,
                subject=subject,
            )
        )

    if not creds["send"]:
        print(f"[DRY-RUN] Would send {len(messages)} emails for year {year} from '{from_addr}':")
        print(f"Assignments file: {ypath}")
        print(f"Emails file:      {emails_path}")
        print("-" * 60)
        for m in messages:
            print(f"TO:   {m['To']}")
            print(f"SUBJ: {m['Subject']}")
            print()
            print(m.get_content())
            print("-" * 60)
        print("Set `send = true` in sendercredentials.txt to actually send.")
        return

    if not creds["password"]:
        sys.exit("[ERROR] `send = true` but no password found in sendercredentials.txt.")

    print(f"[INFO] Sending {len(messages)} emails for year {year} via {creds['server']}:{creds['port']} ...")
    send_all(
        messages,
        server=creds["server"],
        port=creds["port"],
        username=creds["username"],
        password=creds["password"],
        use_starttls=creds["use_starttls"],
    )
    print("[OK] All emails sent.")

if __name__ == "__main__":
    main()
