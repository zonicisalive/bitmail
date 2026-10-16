"""
Free email list verifier: asks each recipient's mail server whether the mailbox
exists (SMTP RCPT probe), without sending anything.

Run it from a machine that can reach port 25 (a home PC usually can; the VPS cannot):

    venv/bin/python scripts/verify_emails.py contacts.csv

Writes <file>.verified.csv (every address with its result) and <file>.valid.txt
(only addresses safe to send to). Results:
    valid      the server confirmed the mailbox
    invalid    no such mailbox, or the domain cannot receive mail
    catch_all  the server accepts any address, so this one cannot be confirmed (risky)
    unknown    the server would not say (greylisting, blocks, timeouts); retry later
"""

import asyncio
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.deliverability import PreSendSafetyGuard  # noqa: E402

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
TIMEOUT = 12.0
PARALLEL = 4  # keep low: mail servers slow down or block hosts that probe hard


async def check(email: str, catch_all: dict, sem: asyncio.Semaphore) -> tuple:
    domain = email.split("@")[1]
    async with sem:
        probe = await PreSendSafetyGuard.probe_smtp_mailbox(email, timeout=TIMEOUT)
        if not probe.get("tested"):
            return email, "invalid", probe.get("details", "")
        if probe["status"] == "rejected":
            return email, "invalid", probe.get("details", "")
        if probe["status"] != "accepted":
            return email, "unknown", probe.get("details", "")
        if domain not in catch_all:
            catch_all[domain] = await PreSendSafetyGuard.detect_catch_all(domain, timeout=TIMEOUT)
        if catch_all[domain].get("is_catch_all"):
            return email, "catch_all", catch_all[domain].get("details", "")
        return email, "valid", probe.get("details", "")


async def main(path: Path) -> None:
    emails = list(dict.fromkeys(e.lower() for e in EMAIL_RE.findall(path.read_text(errors="ignore"))))
    if not emails:
        sys.exit(f"No email addresses found in {path}")
    print(f"Checking {len(emails)} addresses...")

    catch_all: dict = {}
    sem = asyncio.Semaphore(PARALLEL)
    results = []
    for done in asyncio.as_completed([check(e, catch_all, sem) for e in emails]):
        email, result, detail = await done
        results.append((email, result, detail))
        print(f"  [{len(results)}/{len(emails)}] {result:9} {email}")

    order = {e: i for i, e in enumerate(emails)}
    results.sort(key=lambda r: order[r[0]])
    out = path.with_suffix(".verified.csv")
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email", "result", "detail"])
        w.writerows(results)
    valid = path.with_suffix(".valid.txt")
    valid.write_text("".join(f"{e}\n" for e, r, _ in results if r == "valid"))

    counts = {k: sum(1 for _, r, _ in results if r == k) for k in ("valid", "invalid", "catch_all", "unknown")}
    print("\n" + "  ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"Full results: {out}\nSafe to send: {valid}")
    if counts["unknown"] > len(results) / 2:
        print("Most results are 'unknown': port 25 is probably blocked on this network.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(Path(sys.argv[1])))
