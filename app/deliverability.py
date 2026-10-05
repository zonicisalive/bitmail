"""
Deliverability and DNS Authentication Engine for Bitmail.
Provides:
1. Pre-send Email Validation (RFC 5322 syntax, disposable/burner detection, async MX lookup).
2. Live DNS Authenticator Diagnostics (SPF, DKIM, DMARC, MX, health scoring, DNS record generation).
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import dns.asyncresolver
import dns.exception
import dns.resolver

logger = logging.getLogger("bitmail.deliverability")

# RFC 5322 simplified but strict regex for production web apps
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)

# Curated list of 250+ top disposable / burner / temporary email domains
DISPOSABLE_DOMAINS: Set[str] = {
    "0-mail.com", "0815.ru", "0clickemail.com", "10mail.org", "10minutemail.be",
    "10minutemail.cf", "10minutemail.co.uk", "10minutemail.co.za", "10minutemail.com",
    "10minutemail.de", "10minutemail.ga", "10minutemail.gq", "10minutemail.net",
    "10minutemail.nl", "10minutemail.org", "10minutemail.pl", "10minutemailbox.com",
    "10minutemails.in", "10minutemail.us", "20minutemail.com", "20minutemail.it",
    "33mail.com", "anonbox.net", "anonymbox.com", "antichef.com", "antichef.net",
    "armyspy.com", "binkmail.com", "bio-munch.com", "bobmail.info", "brefmail.com",
    "bspamfree.org", "bugmenot.com", "burnermail.io", "burntmail.com", "cachedot.net",
    "cardstore.cf", "casualdx.com", "chacuo.net", "chacuo.org", "cloudmail.biz",
    "cloudtemp.net", "cool.fr.nf", "courriel.fr.nf", "crazymailing.com", "cuvox.de",
    "dayrep.com", "deadaddress.com", "deadfake.com", "dispostable.com", "divismail.ru",
    "dodgit.com", "dontreg.com", "dontsendmespam.de", "drdrb.net", "dropmail.me",
    "dumpmail.de", "e4ward.com", "einrot.com", "email-temp.com", "emailage.net",
    "emailondeck.com", "emailproxsy.com", "emailsensei.com", "emailtemporario.com.br",
    "emailtemporal.org", "emailtemporaneo.com", "emailthe.net", "emailwarden.com",
    "emeil.in", "emeil.ir", "emkei.cz", "evopc.com", "fackme.gq", "fakeinbox.com",
    "fakemailgenerator.com", "fast-email.org", "fastmail.cf", "filzmail.com",
    "flemail.ru", "fmail.pl", "frapmail.com", "front14.org", "fudgerub.com",
    "generator.email", "getairmail.com", "getnada.com", "ghostmail.com", "gishpuppy.com",
    "glocksoft.com", "goemailgo.com", "gorillamail.com", "gotmail.com", "greenkarmamail.com",
    "grr.la", "guerrillamail.biz", "guerrillamail.com", "guerrillamail.de", "guerrillamail.net",
    "guerrillamail.org", "guerrillamailblock.com", "gustr.com", "harakirimail.com",
    "hidemail.de", "hightempmail.com", "hmamail.com", "hovia.com", "hulapla.de",
    "imgof.com", "inboxalias.com", "inboxbear.com", "inboxclean.com", "inboxdesign.me",
    "inboxkitten.com", "incognitomail.org", "instant-mail.de", "instantemailaddress.com",
    "instapass.net", "ipoo.org", "ispyforme.com", "itunescardcodes.info", "jetable.net",
    "jetable.org", "jourrapide.com", "junk1e.com", "kasmail.com", "klzlk.com",
    "koszmail.pl", "kurzepost.de", "letthemeatspam.com", "lifebyfood.com", "link2mail.net",
    "litedrop.com", "loopermail.com", "maildrop.cc", "mailcatch.com", "maileater.com",
    "mailexpire.com", "mailfa.tk", "mailforspam.com", "mailhazard.com", "mailhazard.us",
    "mailimate.com", "mailin8r.com", "mailinator.com", "mailinator.net", "mailinator2.com",
    "mailincubator.com", "mailkeep.com", "mailmoat.com", "mailnesia.com", "mailnull.com",
    "mailpoof.com", "mailsac.com", "mailscrap.com", "mailslap.com", "mailtemp.net",
    "mailtothis.com", "mailtrash.net", "meltmail.com", "mintemail.com", "mohmal.com",
    "mohmal.im", "mohmal.in", "mohmal.it", "mohmal.pw", "mohmal.tech", "moncourrier.fr.nf",
    "monemail.fr.nf", "monmail.fr.nf", "msgsafe.io", "mt2015.com", "mytemp.email",
    "nada.ltd", "nada.pn", "netmails.net", "nobulk.com", "noclickemail.com",
    "nomail.xl.cx", "nomail2me.com", "nospam.ze.cx", "nospam4.us", "nospamday.com",
    "nospamfor.us", "notsharingmy.info", "nowmymail.com", "objectmail.com", "oneoffmail.com",
    "onetimeusemail.com", "owlymail.com", "pookmail.com", "postacin.com", "privymail.de",
    "proxymail.eu", "quickinbox.com", "rcpt.at", "recode.me", "redchan.it",
    "rhyta.com", "rmqkr.net", "safetymail.info", "safe-mail.net", "sandelf.com",
    "saynotospams.com", "scatmail.com", "schmuckmail.de", "sharklasers.com", "shieldemail.com",
    "shiftmail.com", "shortmail.net", "sibmail.com", "sinn-frei.at", "slopsbox.com",
    "sofort-mail.de", "sogetthis.com", "spam4.me", "spamavert.com", "spambob.com",
    "spambob.net", "spambob.org", "spambog.com", "spambog.de", "spambog.ru",
    "spameater.org", "spamex.com", "spamfree24.org", "spamgourmet.com", "spamhole.com",
    "spaminator.de", "spaml.com", "spaml.de", "spammotel.com", "spamobox.com",
    "spamsour.com", "spamspot.com", "superrito.com", "suremail.info", "tafmail.com",
    "teewars.org", "teleworm.us", "temp-mail.de", "temp-mail.info", "temp-mail.org",
    "temp-mail.ru", "tempail.com", "tempamail.com", "tempemail.biz", "tempemail.co",
    "tempemail.net", "tempemailaddress.com", "tempinbox.com", "tempmail.co", "tempmail.com",
    "tempmail.de", "tempmail.eu", "tempmail.it", "tempmail.net", "tempmail.us",
    "tempmailaddress.com", "tempmailer.com", "tempmailer.net", "temporaryforwarding.com",
    "temporaryinbox.com", "temporarymail.com", "temporarymailaddress.com", "temppost.com",
    "throwawayemailaddress.com", "throwawaymail.com", "trash-mail.at", "trash-mail.com",
    "trash-mail.de", "trashmail.at", "trashmail.com", "trashmail.de", "trashmail.me",
    "trashmail.net", "trashmail.org", "trashymail.com", "urhen.com", "wegwerfadresse.de",
    "wegwerfemail.de", "wegwerfmail.de", "wegwerfmail.net", "wegwerfmail.org", "whyspam.me",
    "willhackforfood.biz", "yopmail.com", "yopmail.fr", "yopmail.net", "ypmail.webcam",
    "zippymail.info", "zoemail.com", "zoemail.net", "zoemail.org"
}

# Known Email Providers recognition
KNOWN_PROVIDERS = {
    "google": "Google Workspace / Gmail",
    "googlemail": "Google Workspace / Gmail",
    "outlook": "Microsoft 365 / Outlook",
    "microsoft": "Microsoft 365 / Exchange",
    "protection.outlook": "Microsoft 365 / Exchange",
    "brevo": "Brevo (Sendinblue)",
    "sendinblue": "Brevo (Sendinblue)",
    "sendgrid": "Twilio SendGrid",
    "amazonses": "Amazon Simple Email Service (SES)",
    "mailgun": "Mailgun",
    "postmark": "Postmark",
    "zoho": "Zoho Mail",
    "proton": "ProtonMail",
    "icloud": "Apple iCloud Mail",
    "fastmail": "FastMail",
}


# ==============================================================================
# In-Memory DNS Cache (TTL Caching for ultra-fast checks)
# ==============================================================================
class DnsCache:
    """Thread-safe TTL memory cache for DNS resolutions."""
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._ttl:
                return val
            del self._cache[key]
        return None

    def set(self, key: str, val: Any) -> None:
        self._cache[key] = (time.time(), val)

    def clear(self) -> None:
        self._cache.clear()


_dns_cache = DnsCache(ttl_seconds=300)


def get_async_resolver(timeout: float = 3.0) -> dns.asyncresolver.Resolver:
    """Instantiate a configured async DNS resolver."""
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    return resolver


# ==============================================================================
# 1. Pre-send Email & MX Validator Service
# ==============================================================================
class EmailValidatorService:
    """
    Validates RFC 5322 syntax, detects disposable/burner email providers,
    and conducts asynchronous DNS MX verification.
    """

    @staticmethod
    def validate_syntax(email: str) -> Tuple[bool, Optional[str]]:
        """
        Validate RFC 5322 structural constraints.
        Returns (is_valid, failure_reason).
        """
        if not email or not isinstance(email, str):
            return False, "Email address is empty."

        clean = email.strip()
        if "<" in clean and clean.endswith(">"):
            inner = clean.split("<")[-1].rstrip(">").strip()
            if inner:
                clean = inner

        if len(clean) > 254:
            return False, f"Email address exceeds 254 characters (length: {len(clean)})."

        if "@" not in clean:
            return False, "Missing '@' symbol."

        parts = clean.split("@")
        if len(parts) != 2:
            return False, "Email address contains multiple '@' symbols."

        local, domain = parts[0], parts[1]
        if not local:
            return False, "Local part before '@' is empty."
        if len(local) > 64:
            return False, f"Local part exceeds 64 characters (length: {len(local)})."

        if not domain:
            return False, "Domain part after '@' is empty."
        if len(domain) > 253:
            return False, f"Domain exceeds 253 characters (length: {len(domain)})."

        if ".." in clean:
            return False, "Email contains consecutive dots ('..')."

        if local.startswith(".") or local.endswith("."):
            return False, "Local part cannot start or end with a dot."

        if domain.startswith(".") or domain.endswith("."):
            return False, "Domain cannot start or end with a dot."

        if not EMAIL_REGEX.match(clean):
            return False, "Email contains invalid characters or malformed domain structure."

        # Domain must have at least one dot and non-numeric TLD
        domain_parts = domain.split(".")
        if len(domain_parts) < 2:
            return False, "Domain must contain a valid top-level domain (TLD)."

        tld = domain_parts[-1]
        if len(tld) < 2 or tld.isdigit():
            return False, f"Invalid top-level domain '.{tld}'."

        return True, None

    @staticmethod
    def is_disposable(domain: str) -> bool:
        """Check if domain or parent domain matches known burner providers."""
        if not domain:
            return False
        clean_domain = domain.strip().lower()
        if clean_domain in DISPOSABLE_DOMAINS:
            return True

        # Check subdomains (e.g. user@abc.mailinator.com)
        parts = clean_domain.split(".")
        if len(parts) > 2:
            apex = ".".join(parts[-2:])
            if apex in DISPOSABLE_DOMAINS:
                return True
        return False

    @staticmethod
    async def resolve_mx(domain: str, timeout: float = 3.0) -> Tuple[bool, List[Dict[str, Any]], str]:
        """
        Query MX records asynchronously for domain.
        Falls back to A/AAAA record check if no MX found (per RFC 5321 §5.1).
        Returns (has_mx, list_of_records, reason).
        """
        clean_domain = domain.strip().lower()
        cache_key = f"mx:{clean_domain}"
        cached = _dns_cache.get(cache_key)
        if cached is not None:
            return cached

        resolver = get_async_resolver(timeout)
        records: List[Dict[str, Any]] = []

        try:
            answers = await resolver.resolve(clean_domain, "MX")
            for r in answers:
                records.append({
                    "priority": int(r.preference),
                    "host": str(r.exchange).rstrip(".").lower()
                })
            records.sort(key=lambda x: x["priority"])
            result = (True, records, f"Found {len(records)} active MX record(s).")
            _dns_cache.set(cache_key, result)
            return result
        except dns.resolver.NoAnswer:
            # Fallback to direct A record per RFC 5321 section 5.1
            try:
                a_answers = await resolver.resolve(clean_domain, "A")
                if a_answers:
                    records.append({
                        "priority": 0,
                        "host": clean_domain,
                        "is_direct_a": True
                    })
                    result = (True, records, "No MX records; domain accepts mail via direct A record fallback (RFC 5321).")
                    _dns_cache.set(cache_key, result)
                    return result
            except Exception:
                pass
            result = (False, [], "Domain exists but publishes zero MX or direct A mail exchange records.")
            _dns_cache.set(cache_key, result)
            return result
        except dns.resolver.NXDOMAIN:
            result = (False, [], f"Domain '{clean_domain}' does not exist (NXDOMAIN).")
            _dns_cache.set(cache_key, result)
            return result
        except dns.exception.Timeout:
            result = (False, [], f"DNS lookup timed out for domain '{clean_domain}'.")
            return result
        except Exception as exc:
            result = (False, [], f"DNS resolution failed: {str(exc)}")
            return result

    @classmethod
    async def validate_email(cls, email: str, timeout: float = 3.0) -> Dict[str, Any]:
        """
        Perform complete email validation:
        1. Syntax
        2. Disposable domain
        3. MX host resolution
        Returns standardized verdict: 'valid' | 'risky' | 'invalid'.
        """
        clean_email = email.strip()
        if "<" in clean_email and clean_email.endswith(">"):
            inner = clean_email.split("<")[-1].rstrip(">").strip()
            if inner:
                clean_email = inner
        syntax_ok, syntax_err = cls.validate_syntax(clean_email)
        if not syntax_ok:
            return {
                "email": clean_email,
                "status": "invalid",
                "is_valid": False,
                "syntax_valid": False,
                "is_disposable": False,
                "has_mx": False,
                "domain": clean_email.split("@")[-1] if "@" in clean_email else "",
                "mx_records": [],
                "reasons": [syntax_err or "Invalid syntax"],
            }

        domain = clean_email.split("@")[1].lower()
        is_burner = cls.is_disposable(domain)
        has_mx, mx_records, mx_reason = await cls.resolve_mx(domain, timeout=timeout)

        reasons = []
        if is_burner:
            reasons.append("Identified as a temporary / disposable burner domain.")
        if not has_mx:
            reasons.append(mx_reason)

        if not has_mx:
            verdict = "invalid"
            is_valid = False
        elif is_burner:
            verdict = "risky"
            is_valid = True  # Can still technically be sent to, but risky
        else:
            verdict = "valid"
            is_valid = True

        return {
            "email": clean_email,
            "status": verdict,
            "is_valid": is_valid,
            "syntax_valid": True,
            "is_disposable": is_burner,
            "has_mx": has_mx,
            "domain": domain,
            "mx_records": mx_records,
            "reasons": reasons if reasons else ["Syntax, domain, and MX records fully verified."],
        }

    @classmethod
    async def validate_batch(cls, emails: List[str], max_concurrency: int = 25) -> Dict[str, Any]:
        """
        Verify a list of emails with bounded concurrency.
        Returns summary statistics and individual item results.
        """
        sem = asyncio.Semaphore(max_concurrency)

        async def worker(e: str):
            async with sem:
                return await cls.validate_email(e)

        tasks = [worker(e) for e in emails]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        valid_count = sum(1 for r in results if r["status"] == "valid")
        risky_count = sum(1 for r in results if r["status"] == "risky")
        invalid_count = sum(1 for r in results if r["status"] == "invalid")
        disposable_count = sum(1 for r in results if r.get("is_disposable"))
        syntax_error_count = sum(1 for r in results if not r.get("syntax_valid"))
        no_mx_count = sum(1 for r in results if not r.get("has_mx") and r.get("syntax_valid"))
        clean_rate = round((valid_count / len(results) * 100), 1) if results else 0.0

        return {
            "total": len(results),
            "valid_count": valid_count,
            "deliverable_count": valid_count,
            "deliverable_percent": clean_rate,
            "risky_count": risky_count,
            "invalid_count": invalid_count,
            "disposable_count": disposable_count,
            "syntax_error_count": syntax_error_count,
            "no_mx_count": no_mx_count,
            "clean_rate": clean_rate,
            "results": results
        }


# ==============================================================================
# 2. Live DNS Authenticator Diagnostic Service (SPF, DKIM, DMARC, MX)
# ==============================================================================
class DnsAuthenticatorService:
    """
    Executes live DNS audits for sending domains:
    - SPF: Checks mechanisms, ~all/-all strictness, and RFC multiple-record errors.
    - DMARC: Checks policy (none/quarantine/reject), rua reporting, and Google/Yahoo bulk rules.
    - DKIM: Queries selector public keys, identifies algorithm and key length.
    - MX: Queries mail exchange servers and identifies provider.
    - Calculates Deliverability Health Score (0-100) and Letter Grade (A+ to F).
    - Generates copy-paste ready DNS records for Cloudflare, Route53, Namecheap, etc.
    """

    @staticmethod
    async def _query_txt(host: str, timeout: float = 3.5) -> List[str]:
        """Query TXT records for a specific host and normalize string values."""
        clean_host = host.strip().lower()
        resolver = get_async_resolver(timeout)
        try:
            answers = await resolver.resolve(clean_host, "TXT")
            records = []
            for r in answers:
                if hasattr(r, "strings"):
                    val = "".join(part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part) for part in r.strings)
                else:
                    val = str(r).strip('"').replace('" "', '')
                records.append(val)
            return records
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return []
        except Exception:
            return []

    @classmethod
    async def check_spf(cls, domain: str, timeout: float = 3.5) -> Dict[str, Any]:
        """Query and evaluate SPF record."""
        clean_domain = domain.strip().lower()

        try:
            txt_records = await cls._query_txt(clean_domain, timeout)
            spf_records = [
                r for r in txt_records
                if r.startswith("v=spf1")
            ]
        except Exception as exc:
            return {
                "status": "error",
                "score": 0,
                "record": None,
                "reasons": [f"DNS TXT resolution failed: {str(exc)}"],
                "recommendation": f"Add a valid TXT record for '{clean_domain}' with 'v=spf1 ... ~all'."
            }

        if not spf_records:
            return {
                "status": "fail",
                "score": 0,
                "record": None,
                "reasons": ["No SPF (Sender Policy Framework) TXT record found."],
                "policy": None,
                "recommendation": f"Create a TXT record for '@' (or '{clean_domain}') with value: 'v=spf1 include:_spf.google.com ~all' (or your SMTP IP/relay)."
            }

        if len(spf_records) > 1:
            return {
                "status": "error",
                "score": 5,
                "record": spf_records[0],
                "all_records": spf_records,
                "reasons": [
                    f"CRITICAL RFC 7208 VIOLATION: Multiple SPF records found ({len(spf_records)}). Receiving mail servers will evaluate this as PermError and reject or spam your emails!"
                ],
                "details": f"Multiple SPF records found ({len(spf_records)}). RFC 7208 prohibits publishing more than one SPF record.",
                "recommendation": "Merge all SPF rules into a single 'v=spf1 ...' TXT record and delete the duplicate."
            }

        record = spf_records[0]
        reasons = []
        policy = "unknown"
        score = 20

        # Evaluate all-mechanisms
        if "-all" in record:
            policy = "-all"
            score += 10
            reasons.append("Strict HardFail ('-all') policy enabled. Highest authentication security.")
        elif "~all" in record:
            policy = "~all"
            score += 8
            reasons.append("Standard SoftFail ('~all') policy enabled. Recommended for SaaS relays.")
        elif "?all" in record:
            policy = "?all"
            score += 2
            reasons.append("Neutral ('?all') policy detected. Offers weak protection against spoofing.")
        elif "+all" in record:
            policy = "+all"
            score = 0
            reasons.append("DANGEROUS: '+all' allows ANY server on the internet to send on your behalf!")

        # Count DNS lookup mechanisms
        terms = record.split()
        lookup_mechanisms = sum(1 for t in terms if any(t.startswith(prefix) for prefix in ("include:", "a", "mx", "ptr", "exists:", "redirect=")))
        if lookup_mechanisms > 10:
            reasons.append(f"WARNING: SPF contains {lookup_mechanisms} lookup mechanisms, exceeding the RFC 10-lookup limit!")
            score = max(5, score - 10)

        status = "pass" if score >= 25 else ("warn" if score >= 15 else "fail")

        return {
            "status": status,
            "score": score,
            "record": record,
            "policy": policy,
            "lookup_count": lookup_mechanisms,
            "details": f"SPF record active with policy '{policy}'.",
            "reasons": reasons,
            "recommendation": "SPF record is well configured." if status == "pass" else "Review SPF mechanisms to ensure safe delivery."
        }

    @classmethod
    async def check_dmarc(cls, domain: str, timeout: float = 3.5) -> Dict[str, Any]:
        """
        Query and evaluate DMARC record at _dmarc.<domain>,
        falling back to organizational apex domain per RFC 7489 §6.6.3.
        """
        clean_domain = domain.strip().lower()

        dmarc_host = f"_dmarc.{clean_domain}"
        queried_host = dmarc_host

        txt_records = await cls._query_txt(dmarc_host, timeout)
        dmarc_records = [
            r for r in txt_records
            if r.startswith("v=DMARC1")
        ]

        # Fallback to apex domain if this was a subdomain (e.g. mail.bitnade.com -> bitnade.com)
        if not dmarc_records and clean_domain.count(".") >= 2:
            parts = clean_domain.split(".")
            apex_domain = ".".join(parts[-2:])
            apex_dmarc = f"_dmarc.{apex_domain}"
            apex_records = await cls._query_txt(apex_dmarc, timeout)
            dmarc_records = [
                r for r in apex_records
                if r.startswith("v=DMARC1")
            ]
            if dmarc_records:
                queried_host = apex_dmarc

        if not dmarc_records:
            return {
                "status": "fail",
                "score": 0,
                "record": None,
                "policy": None,
                "reasons": [
                    "No DMARC record found at _dmarc." + clean_domain + ".",
                    "MANDATORY REQUIREMENT: As of February 2024, Gmail and Yahoo automatically reject or mark as spam mass emails sent without DMARC!"
                ],
                "recommendation": f"Add a TXT record for '_dmarc.{clean_domain}' with value: 'v=DMARC1; p=quarantine; rua=mailto:dmarc-reports@{clean_domain};'"
            }

        record = dmarc_records[0]
        reasons = []
        score = 20

        # Parse tags
        tags = {}
        for token in record.split(";"):
            token = token.strip()
            if "=" in token:
                k, v = token.split("=", 1)
                tags[k.strip().lower()] = v.strip().lower()

        policy = tags.get("p", "none")
        rua = tags.get("rua", None)
        pct = tags.get("pct", "100")

        if policy == "reject":
            score += 15
            reasons.append("Strict policy 'p=reject' active. Unauthorized spoofed emails are immediately dropped by inboxes.")
        elif policy == "quarantine":
            score += 12
            reasons.append("Protective policy 'p=quarantine' active. Unauthorized emails are sent to spam/junk folders.")
        elif policy == "none":
            score += 5
            reasons.append("Monitoring-only policy 'p=none' active. Complies with Gmail/Yahoo minimum, but does not block spoofing.")
        else:
            reasons.append(f"Unrecognized policy 'p={policy}'.")

        if rua:
            score += 5
            reasons.append(f"Aggregate reporting enabled ({rua}).")
        else:
            reasons.append("No aggregate report URI ('rua=') defined. You will not receive deliverability audit reports.")

        meets_bulk = policy in ("quarantine", "reject")
        status = "pass" if policy in ("quarantine", "reject") else ("warning" if policy == "none" else "fail")

        return {
            "status": status,
            "score": min(score, 35),
            "record": record,
            "queried_host": queried_host,
            "policy": policy,
            "rua": rua,
            "pct": pct,
            "meets_2024_bulk_requirements": meets_bulk,
            "details": f"DMARC policy 'p={policy}' is active." + (" Meets Google/Yahoo 2024 bulk sender rules." if meets_bulk else ""),
            "reasons": reasons,
            "recommendation": "DMARC record is well configured." if status == "pass" else "Consider advancing DMARC policy from 'none' to 'quarantine' or 'reject'."
        }

    @classmethod
    async def check_dkim(cls, domain: str, selector: Optional[str] = None, timeout: float = 3.5) -> Dict[str, Any]:
        """
        Query DKIM public key for domain.
        If selector is not provided, probes standard common selectors:
        'default', 'k1', 'mail', 'google', 'smtp', 's1', 's2017', 's2020'.
        """
        clean_domain = domain.strip().lower()

        selectors_to_try = [selector.strip()] if selector and selector.strip() else [
            "default", "k1", "mail", "google", "smtp", "s1", "s2017", "s2020"
        ]

        found_record = None
        found_selector = None

        for sel in selectors_to_try:
            dkim_host = f"{sel}._domainkey.{clean_domain}"
            txt_records = await cls._query_txt(dkim_host, timeout)
            for val in txt_records:
                if "v=DKIM1" in val or "p=" in val:
                    found_record = val
                    found_selector = sel
                    break
            if found_record:
                break

        if not found_record:
            return {
                "status": "warn",
                "score": 0,
                "selector": selector or "default (scanned common selectors)",
                "record": None,
                "reasons": [
                    f"No active DKIM key found for domain '{clean_domain}' using selector(s): {', '.join(selectors_to_try[:4])}."
                ],
                "recommendation": "Generate a 2048-bit DKIM key in your mail relay (e.g. Brevo/SendGrid/Google) and add the TXT record to your DNS."
            }

        # Parse key
        key_valid = "p=" in found_record
        reasons = [f"DKIM public key record found using selector '{found_selector}'."]

        if "v=DKIM1" in found_record:
            reasons.append("Standard DKIM version 1 header verified.")

        score = 25 if key_valid else 10
        status = "pass" if key_valid else "warn"

        return {
            "status": status,
            "score": score,
            "selector": found_selector,
            "record": found_record,
            "is_valid": key_valid,
            "reasons": reasons,
            "recommendation": f"DKIM authentication is active on selector '{found_selector}'."
        }

    @staticmethod
    async def check_mx(domain: str, timeout: float = 3.5) -> Dict[str, Any]:
        """Query and evaluate MX records for domain."""
        clean_domain = domain.strip().lower()
        resolver = get_async_resolver(timeout)

        try:
            answers = await resolver.resolve(clean_domain, "MX")
            records = [
                {"priority": int(r.preference), "host": str(r.exchange).rstrip(".").lower()}
                for r in answers
            ]
            records.sort(key=lambda x: x["priority"])
        except Exception as exc:
            return {
                "status": "fail",
                "score": 0,
                "records": [],
                "provider": "None / Unresolved",
                "reasons": [f"No MX records found or resolution failed: {str(exc)}"],
                "recommendation": f"Configure MX records for '{clean_domain}' so inboxes can receive bounces and replies."
            }

        # Detect provider
        detected_provider = "Custom / Private Mail Server"
        for r in records:
            host = r["host"]
            for key, name in KNOWN_PROVIDERS.items():
                if key in host:
                    detected_provider = name
                    break
            if detected_provider != "Custom / Private Mail Server":
                break

        return {
            "status": "pass",
            "score": 10,
            "records": records,
            "provider": detected_provider,
            "reasons": [f"Found {len(records)} MX record(s). Mail routing handled by: {detected_provider}."],
            "recommendation": "MX records are active and operational."
        }

    @classmethod
    def calculate_health_score(
        cls,
        spf: Dict[str, Any],
        dmarc: Dict[str, Any],
        dkim: Dict[str, Any],
        mx: Dict[str, Any]
    ) -> Tuple[int, str]:
        """
        Compute total deliverability health score (0 to 100) and assign letter grade.
        Weights:
        - DMARC: 35 pts
        - SPF: 30 pts
        - DKIM: 25 pts
        - MX: 10 pts
        """
        score = spf.get("score", 0) + dmarc.get("score", 0) + dkim.get("score", 0) + mx.get("score", 0)
        score = max(0, min(100, int(score)))

        if score >= 90:
            grade = "A+"
        elif score >= 80:
            grade = "A"
        elif score >= 65:
            grade = "B"
        elif score >= 50:
            grade = "C"
        else:
            grade = "F"

        return score, grade

    @classmethod
    def generate_recommended_records(cls, domain: str, results: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate copyable DNS records to fix missing or weak authentication."""
        clean_domain = domain.strip().lower()
        records: List[Dict[str, str]] = []

        # SPF Recommendation
        spf = results.get("spf", {})
        if spf.get("status") in ("fail", "error") or not spf.get("record"):
            records.append({
                "type": "TXT",
                "name": "@",
                "full_name": clean_domain,
                "value": "v=spf1 include:_spf.google.com ~all",
                "purpose": "Authorizes your sending relay to transmit mail for this domain."
            })

        # DMARC Recommendation
        dmarc = results.get("dmarc", {})
        if dmarc.get("status") in ("fail", "error") or not dmarc.get("record") or dmarc.get("policy") == "none":
            policy_val = "p=quarantine" if dmarc.get("policy") == "none" else "p=quarantine"
            records.append({
                "type": "TXT",
                "name": "_dmarc",
                "full_name": f"_dmarc.{clean_domain}",
                "value": f"v=DMARC1; {policy_val}; rua=mailto:dmarc-reports@{clean_domain}; pct=100;",
                "purpose": "Protects against domain spoofing and satisfies Google & Yahoo 2024 bulk requirements."
            })

        # DKIM Recommendation (template if not found)
        dkim = results.get("dkim", {})
        if dkim.get("status") != "pass":
            records.append({
                "type": "TXT",
                "name": "default._domainkey",
                "full_name": f"default._domainkey.{clean_domain}",
                "value": "v=DKIM1; k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC3...",
                "purpose": "Cryptographically signs outbound emails to verify sender authenticity."
            })

        return records

    @classmethod
    async def check_domain_deliverability(cls, domain: str, dkim_selector: Optional[str] = None) -> Dict[str, Any]:
        """
        Comprehensive asynchronous domain deliverability check.
        Runs SPF, DMARC, DKIM, and MX probes in parallel.
        """
        clean_domain = domain.strip().lower()
        if "@" in clean_domain:
            clean_domain = clean_domain.split("@")[-1]

        spf_task = cls.check_spf(clean_domain)
        dmarc_task = cls.check_dmarc(clean_domain)
        dkim_task = cls.check_dkim(clean_domain, selector=dkim_selector)
        mx_task = cls.check_mx(clean_domain)

        spf, dmarc, dkim, mx = await asyncio.gather(spf_task, dmarc_task, dkim_task, mx_task)

        score, grade = cls.calculate_health_score(spf, dmarc, dkim, mx)
        diagnostics = {
            "spf": spf,
            "dmarc": dmarc,
            "dkim": dkim,
            "mx": mx
        }
        recommended_records = cls.generate_recommended_records(clean_domain, diagnostics)

        return {
            "domain": clean_domain,
            "score": score,
            "grade": grade,
            "spf": spf,
            "dmarc": dmarc,
            "dkim": dkim,
            "mx": mx,
            "recommended_records": recommended_records,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }


# ==============================================================================
# 3. Pre-Send Mailbox Availability & Safety Guard
# ==============================================================================

ROLE_BASED_PREFIXES: Set[str] = {
    "abuse", "admin", "administrator", "billing", "compliance", "contact",
    "careers", "daemon", "devnull", "dns", "ftp", "help", "helpdesk",
    "hostmaster", "hr", "info", "inoc", "ispfeedback", "ispsupport", "jobs",
    "legal", "list", "list-request", "mail", "mailer-daemon", "marketing",
    "media", "news", "noc", "no-reply", "noreply", "null", "office", "postmaster",
    "press", "privacy", "root", "sales", "security", "spam", "spamtrap",
    "support", "sysadmin", "tech", "undisclosed-recipients", "unsubscribe",
    "usenet", "uucp", "webmaster", "www"
}


class PreSendSafetyGuard:
    """
    Evaluates whether an email address is available to send to, risky / not recommended,
    or unsafe (do not send) before attempting SMTP delivery.
    """

    @classmethod
    def is_role_account(cls, email: str) -> Tuple[bool, Optional[str]]:
        """Detect generic departmental or role-based mailbox addresses."""
        if not email or "@" not in email:
            return False, None
        local = email.split("@")[0].strip().lower()
        if "+" in local:
            local = local.split("+")[0]
        if local in ROLE_BASED_PREFIXES:
            return True, f"'{local}@' is a generic role-based address. Role mailboxes suffer higher complaint rates and are often monitored by ISP spam-traps."
        return False, None

    @classmethod
    async def check_suppression(cls, email: str) -> Tuple[bool, Optional[str]]:
        """Check if recipient is in the local suppression table or marked bounced/unsubscribed."""
        clean = email.strip().lower()
        try:
            from app.db import get_db
            async with get_db() as db:
                async with db.execute(
                    "SELECT reason FROM suppressions WHERE email = ? COLLATE NOCASE LIMIT 1", (clean,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        return True, f"Address is blacklisted in global suppression table ({row['reason'] or 'suppressed'})."

                async with db.execute(
                    "SELECT reason FROM suppression_list WHERE email = ? COLLATE NOCASE LIMIT 1", (clean,)
                ) as cur_sl:
                    row_sl = await cur_sl.fetchone()
                    if row_sl:
                        return True, f"Address is in suppression list ({row_sl['reason'] or 'suppressed'})."

                async with db.execute(
                    "SELECT status FROM subscribers WHERE email = ? COLLATE NOCASE LIMIT 1", (clean,)
                ) as cur2:
                    row2 = await cur2.fetchone()
                    if row2 and row2["status"] in ("bounced", "unsubscribed", "complained"):
                        return True, f"Recipient is marked as '{row2['status']}' in subscriber database."
        except Exception as e:
            logger.debug("Suppression check exception: %s", e)
        return False, None

    @classmethod
    async def probe_smtp_mailbox(cls, email: str, timeout: float = 3.0) -> Dict[str, Any]:
        """
        Lightweight async SMTP handshake test on MX port 25.
        Issues EHLO, MAIL FROM, RCPT TO, QUIT to test mailbox existence without sending.
        Gracefully handles ISP firewall port 25 blocks and timeouts.
        """
        clean = email.strip().lower()
        domain = clean.split("@")[-1] if "@" in clean else ""
        if not domain:
            return {"tested": False, "passed": False, "status": "failed", "details": "No domain found"}

        has_mx, mx_records, _ = await EmailValidatorService.resolve_mx(domain, timeout=timeout)
        if not has_mx or not mx_records:
            return {"tested": False, "passed": False, "status": "failed", "details": "No MX mail exchanger records found"}

        primary_mx = mx_records[0]["host"]
        if primary_mx in ("localhost", "127.0.0.1", "sandbox"):
            return {
                "tested": True,
                "passed": True,
                "status": "accepted",
                "code": 250,
                "details": f"Localhost/sandbox relay accepts recipient '{clean}'.",
                "mx_host": primary_mx
            }

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(primary_mx, 25),
                timeout=timeout
            )
        except Exception as conn_err:
            return {
                "tested": True,
                "passed": True,  # Inconclusive (firewall), do not block
                "status": "inconclusive",
                "code": None,
                "details": f"Port 25 connection to MX {primary_mx} firewalled or unreachable: {conn_err}",
                "mx_host": primary_mx
            }

        try:
            # Banner
            await asyncio.wait_for(reader.readline(), timeout=timeout)

            # EHLO
            writer.write(b"EHLO bitmail.security.probe\r\n")
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not line or line[3:4] == b" ":
                    break

            # MAIL FROM
            writer.write(b"MAIL FROM:<probe@bitmail.security>\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=timeout)

            # RCPT TO
            writer.write(f"RCPT TO:<{clean}>\r\n".encode("latin-1"))
            await writer.drain()
            rcpt_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            rcpt_str = rcpt_line.decode("latin-1", errors="ignore").strip()

            try:
                writer.write(b"QUIT\r\n")
                await writer.drain()
            except Exception:
                pass
            writer.close()
            await writer.wait_closed()

            code = None
            try:
                code = int(rcpt_str[:3])
            except Exception:
                pass

            if code and 200 <= code < 300:
                return {
                    "tested": True,
                    "passed": True,
                    "status": "accepted",
                    "code": code,
                    "details": f"Mailbox verified by MX {primary_mx} ({rcpt_str}).",
                    "mx_host": primary_mx
                }
            elif code and code in (550, 551, 552, 553, 554):
                return {
                    "tested": True,
                    "passed": False,
                    "status": "rejected",
                    "code": code,
                    "details": f"Mailbox rejected by MX {primary_mx}: user unknown ({rcpt_str}).",
                    "mx_host": primary_mx
                }
            else:
                return {
                    "tested": True,
                    "passed": True,
                    "status": "deferred_or_greylisted",
                    "code": code,
                    "details": f"Server response from MX {primary_mx}: {rcpt_str}",
                    "mx_host": primary_mx
                }
        except Exception as probe_err:
            try:
                writer.close()
            except Exception:
                pass
            return {
                "tested": True,
                "passed": True,
                "status": "inconclusive",
                "code": None,
                "details": f"SMTP probe interrupted: {probe_err}",
                "mx_host": primary_mx
            }

    @classmethod
    async def evaluate_sendability(
        cls,
        email: str,
        probe_smtp: bool = False,
        strict_mode: bool = False,
        timeout: float = 3.0
    ) -> Dict[str, Any]:
        """
        Orchestrates all safety & availability checks for an email address:
        1. RFC 5322 syntax
        2. Local suppression & bounce blacklist
        3. Domain MX record & NXDOMAIN existence
        4. Disposable / temporary burner domain check
        5. Role-based / spam trap account check
        6. Optional active SMTP port 25 mailbox probe
        """
        clean = email.strip()
        if "<" in clean and clean.endswith(">"):
            inner = clean.split("<")[-1].rstrip(">").strip()
            if inner:
                clean = inner

        domain = clean.split("@")[-1].lower() if "@" in clean else ""
        reasons: List[str] = []
        checks: Dict[str, Any] = {}
        score = 100
        hard_block = False
        warning_flags = False

        # 1. Syntax Check
        syntax_ok, syntax_err = EmailValidatorService.validate_syntax(clean)
        checks["syntax"] = {
            "passed": syntax_ok,
            "status": "ok" if syntax_ok else "failed",
            "details": "RFC 5322 compliant syntax" if syntax_ok else (syntax_err or "Invalid syntax"),
            "metadata": {"length": len(clean)}
        }
        if not syntax_ok:
            reasons.append(syntax_err or "Invalid email syntax")
            hard_block = True
            score = 0

        # 2. Suppression Check
        is_suppressed, supp_reason = await cls.check_suppression(clean)
        checks["suppression"] = {
            "passed": not is_suppressed,
            "status": "ok" if not is_suppressed else "failed",
            "details": "Not listed on any suppression blacklist" if not is_suppressed else supp_reason,
            "metadata": {"is_suppressed": is_suppressed}
        }
        if is_suppressed:
            reasons.append(supp_reason or "Email is suppressed")
            hard_block = True
            score = 0

        # 3. Domain & MX Check
        if not hard_block and domain:
            has_mx, mx_recs, mx_reason = await EmailValidatorService.resolve_mx(domain, timeout=timeout)
            checks["domain_mx"] = {
                "passed": has_mx,
                "status": "ok" if has_mx else "failed",
                "details": mx_reason,
                "metadata": {"records": mx_recs, "record_count": len(mx_recs)}
            }
            if not has_mx:
                reasons.append(f"Domain '{domain}' has no valid MX records to receive emails.")
                hard_block = True
                score = 0
        else:
            checks["domain_mx"] = {
                "passed": False,
                "status": "failed",
                "details": "Skipped due to syntax or suppression failure",
                "metadata": {"records": [], "record_count": 0}
            }

        # 4. Disposable Domain Check
        is_burner = EmailValidatorService.is_disposable(domain) if domain else False
        checks["disposable"] = {
            "passed": not is_burner,
            "status": "ok" if not is_burner else "warning",
            "details": "Clean corporate or public provider domain" if not is_burner else "Identified as temporary burner domain",
            "metadata": {"is_disposable": is_burner}
        }
        if is_burner:
            reasons.append("Temporary burner domain detected. High risk of immediate bounce and zero engagement.")
            warning_flags = True
            score = max(0, score - 35)

        # 5. Role Account Check
        is_role, role_reason = cls.is_role_account(clean)
        checks["role_account"] = {
            "passed": not is_role,
            "status": "ok" if not is_role else "warning",
            "details": "Individual personal mailbox" if not is_role else role_reason,
            "metadata": {"is_role": is_role}
        }
        if is_role:
            reasons.append(role_reason or "Role-based mailbox detected")
            warning_flags = True
            score = max(0, score - 20)

        # 6. Active SMTP Mailbox Probe (Optional)
        if probe_smtp and not hard_block and domain:
            probe_res = await cls.probe_smtp_mailbox(clean, timeout=timeout)
            checks["smtp_probe"] = {
                "passed": probe_res["passed"],
                "status": "ok" if probe_res["passed"] and probe_res["status"] == "accepted" else ("failed" if not probe_res["passed"] else "warning"),
                "details": probe_res["details"],
                "metadata": probe_res
            }
            if not probe_res["passed"]:
                reasons.append(probe_res["details"])
                hard_block = True
                score = 0
        else:
            checks["smtp_probe"] = {
                "passed": True,
                "status": "untested",
                "details": "Active SMTP port 25 probe skipped (DNS MX verification only)",
                "metadata": {"tested": False}
            }

        # Verdict calculation
        if hard_block:
            verdict = "do_not_send"
            is_safe = False
            rec_text = "DO NOT SEND: Mailbox is unavailable or invalid. Attempting to send will result in a hard bounce or compliance violation."
        elif warning_flags:
            if strict_mode:
                verdict = "do_not_send"
                is_safe = False
                rec_text = "BLOCKED (STRICT MODE): Address flagged as risky (role-based or disposable). Sending suppressed per safety policy."
            else:
                verdict = "not_recommended"
                is_safe = True
                rec_text = "NOT RECOMMENDED: Address carries deliverability risk (burner or role account). Sending is possible, but caution is advised."
        else:
            verdict = "recommended"
            is_safe = True
            rec_text = "RECOMMENDED: Verified address with active MX servers. Safe to send."

        primary_reason = reasons[0] if reasons else "All deliverability and availability checks passed."

        return {
            "email": clean,
            "verdict": verdict,
            "is_safe_to_send": is_safe,
            "safety_score": score,
            "primary_reason": primary_reason,
            "reasons": reasons,
            "checks": checks,
            "recommendation": rec_text
        }

    @classmethod
    async def evaluate_batch(
        cls,
        emails: List[str],
        strict_mode: bool = False,
        concurrency: int = 25
    ) -> Dict[str, Any]:
        """Evaluate a batch of email addresses concurrently for broadcast pre-flight."""
        sem = asyncio.Semaphore(concurrency)

        async def worker(em: str) -> Dict[str, Any]:
            async with sem:
                return await cls.evaluate_sendability(em, probe_smtp=False, strict_mode=strict_mode)

        tasks = [worker(e) for e in emails if e and e.strip()]
        results = await asyncio.gather(*tasks)

        recommended = []
        not_recommended = []
        do_not_send = []
        clean_emails = []

        for r in results:
            v = r["verdict"]
            if v == "recommended":
                recommended.append(r)
                clean_emails.append(r["email"])
            elif v == "not_recommended":
                not_recommended.append(r)
                if not strict_mode:
                    clean_emails.append(r["email"])
            else:
                do_not_send.append(r)

        total = len(results)
        safe_percent = round((len(clean_emails) / max(1, total)) * 100, 1)

        return {
            "summary": {
                "total": total,
                "recommended_count": len(recommended),
                "not_recommended_count": len(not_recommended),
                "do_not_send_count": len(do_not_send),
                "safe_percent": safe_percent
            },
            "results": results,
            "clean_emails": clean_emails
        }

