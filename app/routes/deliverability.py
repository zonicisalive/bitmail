"""
Deliverability & DNS Authentication Router.
Exposes endpoints for pre-send email & MX verification, batch cleaning,
and live DNS authentication diagnostics (SPF, DKIM, DMARC, MX).
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.deliverability import (
    DISPOSABLE_DOMAINS,
    DnsAuthenticatorService,
    EmailValidatorService,
    PreSendSafetyGuard,
)
from app.models import (
    SafetyBatchLookupRequest,
    SafetyBatchLookupResponse,
    SafetyLookupRequest,
    SafetyLookupResponse,
)

router = APIRouter(prefix="/api/deliverability", tags=["Deliverability & DNS Authentication"])


# ==============================================================================
# Pydantic Schemas
# ==============================================================================
class SingleEmailValidationRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320, description="Target email to validate")


class BatchEmailValidationRequest(BaseModel):
    emails: List[str] = Field(..., min_length=1, max_length=1000, description="List of emails to validate (max 1000)")


class DnsCheckRequest(BaseModel):
    domain: str = Field(..., min_length=2, max_length=253, description="Target sending domain (e.g. mail.bitnade.com)")
    dkim_selector: Optional[str] = Field(default=None, max_length=64, description="Optional DKIM selector (e.g. default, k1, google)")


# ==============================================================================
# Route Endpoints
# ==============================================================================
@router.post("/validate-email")
async def validate_single_email(payload: SingleEmailValidationRequest) -> Dict[str, Any]:
    """
    Validate a single email address:
    - RFC 5322 syntax validation
    - Disposable / temporary burner domain check
    - Asynchronous DNS MX record lookup
    """
    email = payload.email.strip()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Email address cannot be empty."
        )
    res = await EmailValidatorService.validate_email(email)
    return {"status": "success", "result": res}


@router.post("/validate-batch")
async def validate_batch_emails(payload: BatchEmailValidationRequest) -> Dict[str, Any]:
    """
    Validate a batch of emails asynchronously:
    - Bounded concurrency execution
    - Categorization: valid, risky (disposable), invalid (bad syntax / missing MX)
    - Returns summary statistics and breakdown
    """
    clean_emails = [e.strip() for e in payload.emails if e and e.strip()]
    if not clean_emails:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No valid email addresses provided in batch list."
        )
    res = await EmailValidatorService.validate_batch(clean_emails)
    return {"status": "success", "result": res}


@router.post("/dns-check")
async def check_domain_dns(payload: DnsCheckRequest) -> Dict[str, Any]:
    """
    Perform live DNS authentication probe for a sending domain:
    - SPF: Checks TXT records, ~all/-all policy, and lookup limits
    - DMARC: Checks _dmarc TXT record, p=none/quarantine/reject, and Google/Yahoo 2024 compliance
    - DKIM: Checks selector TXT record, public key format, and key length
    - MX: Checks mail exchange servers and identifies routing provider
    - Calculates Deliverability Health Score (0-100) and Letter Grade (A+ to F)
    - Generates recommended DNS records to fix missing/weak configurations
    """
    domain = payload.domain.strip()
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Domain cannot be empty."
        )
    res = await DnsAuthenticatorService.check_domain_deliverability(
        domain=domain,
        dkim_selector=payload.dkim_selector
    )
    return {"status": "success", "result": res}


@router.get("/info")
async def get_deliverability_info() -> Dict[str, Any]:
    """
    Metadata about the deliverability scanner engine.
    """
    features_dict = {
        "disposable_domains_tracked": len(DISPOSABLE_DOMAINS),
        "supported_checks": ["syntax_rfc5322", "disposable_detection", "mx_dns_lookup", "spf_audit", "dmarc_audit", "dkim_probe"],
        "compliance_standards": ["RFC 5321", "RFC 5322", "RFC 7208", "RFC 7489", "Google/Yahoo 2024 Bulk Guidelines"],
    }
    return {
        "status": "healthy",
        "engine": "Bitmail Deliverability Probe v2.4",
        "features": features_dict,
        "disposable_domains_tracked": len(DISPOSABLE_DOMAINS),
        "supported_checks": features_dict["supported_checks"],
        "compliance_standards": features_dict["compliance_standards"],
    }


@router.post("/safety-lookup", response_model=SafetyLookupResponse)
async def inspect_email_safety(payload: SafetyLookupRequest) -> Any:
    """
    Perform pre-send availability & safety lookup on an email address:
    - RFC 5322 syntax validation
    - Suppression & bounce blacklist verification
    - Live DNS MX resolution & host reachability
    - Disposable / temporary burner domain detection
    - Role-based address / spam trap detection
    - Optional active SMTP port 25 handshake probe (RCPT TO)
    Returns: RECOMMENDED, NOT_RECOMMENDED, or DO_NOT_SEND.
    """
    email = payload.email.strip()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Email address cannot be empty."
        )
    res = await PreSendSafetyGuard.evaluate_sendability(
        email=email,
        probe_smtp=payload.probe_smtp,
        strict_mode=payload.strict_mode
    )
    return res


@router.post("/safety-batch-lookup", response_model=SafetyBatchLookupResponse)
async def inspect_batch_safety(payload: SafetyBatchLookupRequest) -> Any:
    """
    Batch evaluate a list of emails with categorized buckets:
    - Recommended (safe)
    - Not Recommended (risky role or burner)
    - Do Not Send (invalid, no MX, or suppressed)
    """
    clean_emails = [e.strip() for e in payload.emails if e and e.strip()]
    if not clean_emails:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No valid email addresses provided in batch list."
        )
    res = await PreSendSafetyGuard.evaluate_batch(
        emails=clean_emails,
        strict_mode=payload.strict_mode
    )
    return res


