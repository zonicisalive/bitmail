"""
DNSBL / RBL Blacklist Monitoring Service
Queries 30+ major real-time IP and Domain blacklists in parallel to diagnose sender reputation.
"""

import asyncio
import ipaddress
import logging
import time
from typing import Any, Dict, List, Optional

import dns.asyncresolver
import dns.resolver

from app.db import utc_now_iso
from app.models import BlacklistReportResponse, BlacklistZoneResult

logger = logging.getLogger("bitmail.blacklist")

# Comprehensive catalog of 30+ DNSBL and Domain Blacklists
BLACKLIST_ZONES: List[Dict[str, Any]] = [
    # Top Tier IP Blacklists
    {
        "zone": "zen.spamhaus.org",
        "name": "Spamhaus ZEN",
        "type": "ip",
        "severity": "critical",
        "delist_url": "https://check.spamhaus.org",
        "description": "Composite blacklist including SBL (spam sources), XBL (exploits), and PBL (policy blocks)"
    },
    {
        "zone": "b.barracudacentral.org",
        "name": "Barracuda Reputation (BRBL)",
        "type": "ip",
        "severity": "critical",
        "delist_url": "https://www.barracudacentral.org/rbl/removal-request",
        "description": "Barracuda Networks dynamic list of verified spam sending IP addresses"
    },
    {
        "zone": "bl.spamcop.net",
        "name": "SpamCop Blocking List (SCBL)",
        "type": "ip",
        "severity": "critical",
        "delist_url": "https://www.spamcop.net/bl.shtml",
        "description": "Rapid response list tracking recent spam reports from user feedback"
    },
    {
        "zone": "dnsbl.sorbs.net",
        "name": "SORBS Aggregate",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.sorbs.net/delisting/overview.shtml",
        "description": "Spam and Open Relay Blocking System comprehensive aggregate"
    },
    {
        "zone": "spam.dnsbl.sorbs.net",
        "name": "SORBS Spam",
        "type": "ip",
        "severity": "critical",
        "delist_url": "http://www.sorbs.net/delisting/overview.shtml",
        "description": "Hosts delivering unsolicited bulk email to SORBS traps"
    },
    {
        "zone": "ips.backscatterer.org",
        "name": "Backscatterer (UCEPROTECT)",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.backscatterer.org/?target=test",
        "description": "Systems sending misdirected bounces and autoresponders"
    },
    {
        "zone": "bl.mailspike.net",
        "name": "Mailspike RBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://mailspike.org/iplookup.html",
        "description": "Real-time reputation monitoring IP email traffic patterns"
    },
    {
        "zone": "psbl.surriel.com",
        "name": "Passive Spam Block List (PSBL)",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://psbl.org/listing",
        "description": "Easy-to-delist passive honey-pot spam detection list"
    },
    {
        "zone": "dnsbl-1.uceprotect.net",
        "name": "UCEPROTECT Level 1",
        "type": "ip",
        "severity": "critical",
        "delist_url": "http://www.uceprotect.net/en/rblcheck.php",
        "description": "Direct spam senders caught within the last 7 days"
    },
    {
        "zone": "dnsbl-2.uceprotect.net",
        "name": "UCEPROTECT Level 2",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.uceprotect.net/en/rblcheck.php",
        "description": "Subnets and allocations with persistent spam problems"
    },
    {
        "zone": "dnsbl-3.uceprotect.net",
        "name": "UCEPROTECT Level 3",
        "type": "ip",
        "severity": "info",
        "delist_url": "http://www.uceprotect.net/en/rblcheck.php",
        "description": "Entire ASN / ISP ranges exhibiting widespread abuse"
    },
    {
        "zone": "cbl.abuseat.org",
        "name": "Composite Blocking List (CBL)",
        "type": "ip",
        "severity": "critical",
        "delist_url": "https://www.abuseat.org/lookup.cgi",
        "description": "Infected botnets, open proxies, and trojans"
    },
    {
        "zone": "ubl.unsubscore.com",
        "name": "LashBack Unsubscribe Blacklist (UBL)",
        "type": "ip",
        "severity": "critical",
        "delist_url": "https://www.lashback.com/blacklist-lookup",
        "description": "Senders harvesting and emailing harvested opt-out addresses"
    },
    {
        "zone": "truncate.gbudb.net",
        "name": "GBUdb Truncate",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.gbudb.com/truncate/",
        "description": "Collaborative real-time statistical IP spam probability list"
    },
    {
        "zone": "dnsbl.dronebl.org",
        "name": "DroneBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://dronebl.org/lookup",
        "description": "Compromised machines, worms, and IRC botnets"
    },
    {
        "zone": "rbl.interserver.net",
        "name": "InterServer RBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://rbl.interserver.net/",
        "description": "Direct spam sources monitored by InterServer mail network"
    },
    {
        "zone": "bl.nordspam.com",
        "name": "NordSpam IP RBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://www.nordspam.com/",
        "description": "Reputation tracking list protecting Nordic and EU domains"
    },
    {
        "zone": "all.s5h.net",
        "name": "S5h.net Blacklist",
        "type": "ip",
        "severity": "info",
        "delist_url": "http://all.s5h.net/",
        "description": "General open relay and spammer list"
    },
    {
        "zone": "ix.dnsbl.manitu.net",
        "name": "NiX Spam DNSBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://www.manitu.de/support/entstoeckung/",
        "description": "German high-accuracy honeypot spam detection system"
    },
    {
        "zone": "bl.blocklist.de",
        "name": "Blocklist.de Fail2ban",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://www.blocklist.de/en/search.html",
        "description": "Community fail2ban attacks against mail/ssh services"
    },
    {
        "zone": "korea.services.net",
        "name": "Korea Services Network RBL",
        "type": "ip",
        "severity": "info",
        "delist_url": "http://korea.services.net/",
        "description": "Monitors unauthenticated relay activity"
    },
    {
        "zone": "rbl.blockedservers.com",
        "name": "BlockedServers RBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://blockedservers.com/",
        "description": "Commercial blocklist targeting aggressive mass mailers"
    },
    {
        "zone": "dnsbl.inps.de",
        "name": "INPS DNSBL",
        "type": "ip",
        "severity": "info",
        "delist_url": "http://dnsbl.inps.de/",
        "description": "Independent network protection service blacklist"
    },
    {
        "zone": "db.wpbl.info",
        "name": "Weighted Private Block List (WPBL)",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.wpbl.info/",
        "description": "Automated statistical listing based on recipient complaints"
    },
    {
        "zone": "hostkarma.junkemailfilter.com",
        "name": "Hostkarma JunkEmailFilter",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://wiki.junkemailfilter.com/index.php/Spam_DNS_Lists",
        "description": "Automated IP reputation and spam classification"
    },
    {
        "zone": "dnsbl.zapbl.net",
        "name": "ZapBL DNSBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://zapbl.net/",
        "description": "Real-time blocklist for spam and abused services"
    },
    {
        "zone": "bl.suomispam.net",
        "name": "SuomiSpam Reputation",
        "type": "ip",
        "severity": "warning",
        "delist_url": "https://suomispam.net/",
        "description": "Community spam and abuse prevention list"
    },
    {
        "zone": "dnsrbl.swinog.ch",
        "name": "SwinOG DNSBL",
        "type": "ip",
        "severity": "info",
        "delist_url": "https://www.swinog.ch/",
        "description": "Swiss Network Operators Group spam blocklist"
    },
    {
        "zone": "bl.scientificspam.net",
        "name": "Scientific Spam RBL",
        "type": "ip",
        "severity": "warning",
        "delist_url": "http://www.scientificspam.net/",
        "description": "Heuristic spam trap and predatory sending detection"
    },
    {
        "zone": "rbl.rbldns.ru",
        "name": "RBLDNS Russia RBL",
        "type": "ip",
        "severity": "info",
        "delist_url": "https://rbldns.ru/",
        "description": "Eastern European spam trap and scanner blocklist"
    },

    # Domain Blacklists (DBL / URIBL)
    {
        "zone": "dbl.spamhaus.org",
        "name": "Spamhaus DBL",
        "type": "domain",
        "severity": "critical",
        "delist_url": "https://check.spamhaus.org",
        "description": "Real-time database of domains with poor or abusive reputation"
    },
    {
        "zone": "multi.surbl.org",
        "name": "SURBL Multi",
        "type": "domain",
        "severity": "critical",
        "delist_url": "http://www.surbl.org/surbl-analysis",
        "description": "Identifies domains appearing in unsolicited emails, phishing, or malware"
    },
    {
        "zone": "uribl.spameatingmonkey.net",
        "name": "SpamEatingMonkey URIBL",
        "type": "domain",
        "severity": "warning",
        "delist_url": "https://spameatingmonkey.com/lookup",
        "description": "Lists domains observed in spam messages"
    },
    {
        "zone": "fresh.spameatingmonkey.net",
        "name": "SEM Fresh Domains",
        "type": "domain",
        "severity": "info",
        "delist_url": "https://spameatingmonkey.com/lookup",
        "description": "Newly registered domains (less than 5 days old), considered elevated risk"
    },
    {
        "zone": "black.uribl.com",
        "name": "URIBL Black",
        "type": "domain",
        "severity": "critical",
        "delist_url": "https://admin.uribl.com/",
        "description": "Lists domains that appear in unsolicited bulk email bodies"
    },
    {
        "zone": "grey.uribl.com",
        "name": "URIBL Grey",
        "type": "domain",
        "severity": "warning",
        "delist_url": "https://admin.uribl.com/",
        "description": "Domains found on spam with lower listing threshold"
    },
    {
        "zone": "red.uribl.com",
        "name": "URIBL Red",
        "type": "domain",
        "severity": "warning",
        "delist_url": "https://admin.uribl.com/",
        "description": "Lists domains with suspicious high-volume activity"
    },
    {
        "zone": "bl.nordspam.com",
        "name": "NordSpam Domain RBL",
        "type": "domain",
        "severity": "warning",
        "delist_url": "https://www.nordspam.com/",
        "description": "Domain-level reputation filter monitored across EU regions"
    }
]


def interpret_listing_code(zone: str, return_code: str) -> str:
    """Provides human-readable description for DNSBL 127.0.0.X return codes."""
    if not return_code:
        return "Listed as malicious/spam source"

    if "spamhaus.org" in zone:
        mapping = {
            "127.0.0.2": "Direct UBE/Spam source (SBL)",
            "127.0.0.3": "Spamhaus CSS (snowshoe/reputation spam)",
            "127.0.0.4": "Exploit/Trojan/Compromised Machine (XBL)",
            "127.0.0.9": "Drop list (SBL DROP)",
            "127.0.0.10": "ISP dynamic/end-user IP address (PBL)",
            "127.0.0.11": "ISP dynamic/end-user IP address (PBL)",
            "127.0.1.2": "Spam domain (DBL)",
            "127.0.1.4": "Phishing domain (DBL)",
            "127.0.1.5": "Malware domain (DBL)",
            "127.0.1.6": "Botnet C&C domain (DBL)",
        }
        return mapping.get(return_code, f"Spamhaus listing ({return_code})")

    if "surbl.org" in zone:
        mapping = {
            "127.0.0.2": "Scumware / Malware payload",
            "127.0.0.4": "Phishing site",
            "127.0.0.8": "Malware site",
            "127.0.0.16": "Spam URL (abuse)",
            "127.0.0.32": "Cracked / Compromised host",
            "127.0.0.64": "Abused redirector / URL shortener",
        }
        return mapping.get(return_code, f"SURBL listing ({return_code})")

    if "sorbs.net" in zone:
        mapping = {
            "127.0.0.2": "HTTP Open Relay",
            "127.0.0.3": "SOCKS Open Proxy",
            "127.0.0.4": "Misc Open Proxy",
            "127.0.0.5": "SMTP Open Relay",
            "127.0.0.6": "Spam Trap Hit (Active)",
            "127.0.0.7": "Spam Trap Hit (Historical)",
            "127.0.0.8": "Spam Trap Hit (Web/HTML)",
            "127.0.0.9": "Compromised Dynamic Host",
            "127.0.0.10": "Dynamic IP address range",
        }
        return mapping.get(return_code, f"SORBS listing ({return_code})")

    if "uceprotect.net" in zone:
        return f"UCEPROTECT listing code {return_code}"

    if "barracuda" in zone:
        return "Listed on Barracuda Reputation Network"

    if "spamcop" in zone:
        return "Listed on SpamCop due to recent spam reports"

    return f"Blacklisted (code {return_code})"


class BlacklistMonitorService:
    """
    High-performance async DNSBL diagnostic service.
    Queries 30+ blacklist zones in parallel with strict query timeouts.
    """

    def __init__(self, query_timeout: float = 2.5):
        self.query_timeout = query_timeout

    def is_ip(self, target: str) -> bool:
        """Determines if the target string is a valid IPv4 or IPv6 address."""
        try:
            ipaddress.ip_address(target.strip())
            return True
        except ValueError:
            return False

    def reverse_ip(self, ip_str: str) -> str:
        """Reverses IPv4 octets (e.g. 1.2.3.4 -> 4.3.2.1)."""
        octets = ip_str.strip().split(".")
        if len(octets) == 4:
            return ".".join(reversed(octets))
        return ip_str.strip()

    async def resolve_domain_ip(self, domain: str) -> Optional[str]:
        """Resolves the primary IPv4 address of a domain."""
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = self.query_timeout
        try:
            answers = await resolver.resolve(domain.strip(), "A")
            for rdata in answers:
                return rdata.to_text()
        except Exception:
            pass
        return None

    async def _query_single_zone(
        self,
        query_hostname: str,
        zone_info: Dict[str, Any]
    ) -> BlacklistZoneResult:
        """
        Queries a single DNSBL zone asynchronously.
        Handles NXDOMAIN (clean), timeouts, and query refusers gracefully.
        """
        zone = zone_info["zone"]
        name = zone_info["name"]
        target_type = zone_info["type"]
        delist_url = zone_info.get("delist_url")
        default_severity = zone_info.get("severity", "warning")

        start_time = time.monotonic()
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = self.query_timeout

        try:
            answers = await resolver.resolve(query_hostname, "A")
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)

            codes = [rdata.to_text() for rdata in answers]
            primary_code = codes[0] if codes else "127.0.0.2"

            # Check for known DNSBL public-resolver refuser codes (e.g. 127.255.255.254/255)
            # These mean the DNSBL blocked the DNS resolver from querying, not that the target is listed.
            if primary_code in ("127.255.255.254", "127.255.255.255"):
                return BlacklistZoneResult(
                    zone=zone,
                    name=name,
                    target_type=target_type,
                    listed=False,
                    return_code=primary_code,
                    category="Resolver rate-limited by DNSBL",
                    delist_url=delist_url,
                    severity="info",
                    response_time_ms=elapsed_ms,
                    error="Public DNS query refuser"
                )

            # In Hostkarma, 127.0.0.1 signifies whitelist/trusted, not listed on blacklist
            if "junkemailfilter.com" in zone and primary_code == "127.0.0.1":
                return BlacklistZoneResult(
                    zone=zone,
                    name=name,
                    target_type=target_type,
                    listed=False,
                    return_code=primary_code,
                    category="Whitelisted (Trusted Sender)",
                    delist_url=delist_url,
                    severity="clean",
                    response_time_ms=elapsed_ms
                )

            category = interpret_listing_code(zone, primary_code)

            return BlacklistZoneResult(
                zone=zone,
                name=name,
                target_type=target_type,
                listed=True,
                return_code=primary_code,
                category=category,
                delist_url=delist_url,
                severity=default_severity,
                response_time_ms=elapsed_ms
            )

        except dns.resolver.NXDOMAIN:
            # Clean: The target is not listed in this zone
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
            return BlacklistZoneResult(
                zone=zone,
                name=name,
                target_type=target_type,
                listed=False,
                severity="clean",
                response_time_ms=elapsed_ms
            )

        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
            return BlacklistZoneResult(
                zone=zone,
                name=name,
                target_type=target_type,
                listed=False,
                severity="clean",
                response_time_ms=elapsed_ms
            )

        except dns.resolver.LifetimeTimeout:
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
            return BlacklistZoneResult(
                zone=zone,
                name=name,
                target_type=target_type,
                listed=False,
                severity="info",
                response_time_ms=elapsed_ms,
                error="Query timed out"
            )

        except Exception as e:
            elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
            return BlacklistZoneResult(
                zone=zone,
                name=name,
                target_type=target_type,
                listed=False,
                severity="info",
                response_time_ms=elapsed_ms,
                error=str(e)
            )

    async def check_target(self, target: str) -> BlacklistReportResponse:
        """
        Executes parallel checks across all appropriate blacklist zones.
        If target is an IP, queries all IP zones.
        If target is a domain, queries domain zones (DBL) AND resolves IP to query IP zones.
        """
        clean_target = target.strip().lower()
        # Strip protocol if user pasted URL
        if clean_target.startswith("http://"):
            clean_target = clean_target[7:].split("/")[0]
        elif clean_target.startswith("https://"):
            clean_target = clean_target[8:].split("/")[0]

        is_ip_address = self.is_ip(clean_target)
        target_type = "ip" if is_ip_address else "domain"

        resolved_ip: Optional[str] = None
        tasks = []

        if is_ip_address:
            reversed_ip = self.reverse_ip(clean_target)
            ip_zones = [z for z in BLACKLIST_ZONES if z["type"] == "ip"]
            for z in ip_zones:
                query_host = f"{reversed_ip}.{z['zone']}"
                tasks.append(self._query_single_zone(query_host, z))
        else:
            # Domain check:
            # 1. Domain blocklists (DBL)
            domain_zones = [z for z in BLACKLIST_ZONES if z["type"] == "domain"]
            for z in domain_zones:
                query_host = f"{clean_target}.{z['zone']}"
                tasks.append(self._query_single_zone(query_host, z))

            # 2. Resolve domain's IP and check IP blocklists
            resolved_ip = await self.resolve_domain_ip(clean_target)
            if resolved_ip and self.is_ip(resolved_ip):
                reversed_ip = self.reverse_ip(resolved_ip)
                ip_zones = [z for z in BLACKLIST_ZONES if z["type"] == "ip"]
                for z in ip_zones:
                    query_host = f"{reversed_ip}.{z['zone']}"
                    tasks.append(self._query_single_zone(query_host, z))

        results: List[BlacklistZoneResult] = await asyncio.gather(*tasks)

        listed_count = sum(1 for r in results if r.listed)
        clean_count = len(results) - listed_count

        if listed_count == 0:
            status = "clean"
        elif any(r.listed and r.severity == "critical" for r in results):
            status = "blacklisted"
        else:
            status = "warning"

        return BlacklistReportResponse(
            target=clean_target,
            target_type=target_type,
            resolved_ip=resolved_ip,
            total_zones_checked=len(results),
            listed_count=listed_count,
            clean_count=clean_count,
            is_blacklisted=(listed_count > 0),
            status=status,
            results=results,
            checked_at=utc_now_iso()
        )


# Global singleton
blacklist_service = BlacklistMonitorService()
