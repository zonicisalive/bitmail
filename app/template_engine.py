"""
Template Processing and HTML Transformation Engine.
Handles dynamic merge tag interpolation, open tracking pixel injection,
click tracking link rewriting, and CAN-SPAM / GDPR compliance footer generation.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import html
import logging
import re
from typing import Any, Dict, Optional
import urllib.parse
from bs4 import BeautifulSoup

from app.config import settings
from app.models import RenderedEmailContent, Subscriber

logger = logging.getLogger("mass_email.template_engine")


class TemplateEngine:
    """Enterprise Template Engine for Email Personalization, Tracking & Compliance."""

    # Regex matching {{ tag }}, {{tag | default="val"}}, {{ tag | val }}, {{tag:val}}
    MERGE_TAG_REGEX = re.compile(
        r"\{\{\s*(?P<tag>[a-zA-Z0-9_\.]+)(?:\s*(?:\||:)\s*(?:default\s*=\s*)?[\"']?(?P<default>[^}\"']*)[\"']?)?\s*\}\}",
        re.IGNORECASE
    )

    def __init__(
        self,
        base_url: Optional[str] = None,
        secret_key: Optional[str] = None,
        company_name: Optional[str] = None,
        company_address: Optional[str] = None,
        privacy_policy_url: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or settings.TRACKING_BASE_URL).rstrip("/")
        self.secret_key = secret_key or settings.SECRET_KEY
        self.company_name = company_name or getattr(settings, "COMPANY_NAME", "Enterprise Systems Corp.")
        self.company_address = company_address or getattr(settings, "COMPANY_ADDRESS", "100 Enterprise Way, Suite 400, San Francisco, CA 94105")
        self.privacy_policy_url = privacy_policy_url or getattr(settings, "PRIVACY_POLICY_URL", f"{self.base_url}/privacy")

    def generate_unsubscribe_token(self, email_storage_id: str, email: str) -> str:
        """Generate a secure cryptographic HMAC token for one-click and web unsubscription."""
        data = f"{email_storage_id}:{email.lower().strip()}:{settings.UNSUBSCRIBE_TOKEN_SALT}"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            data.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()[:32]

    def verify_unsubscribe_token(self, email_storage_id: str, email: str, token: str) -> bool:
        """Verify the validity of an unsubscribe token."""
        expected = self.generate_unsubscribe_token(email_storage_id, email)
        return hmac.compare_digest(expected, token)

    def build_unsubscribe_url(self, email_storage_id: str, email: str, campaign_id: Optional[str] = None) -> str:
        """Construct the full HTTPS unsubscribe URL with signed token."""
        token = self.generate_unsubscribe_token(email_storage_id, email)
        params = {
            "token": token,
            "email": email.strip(),
        }
        if campaign_id:
            params["campaign_id"] = campaign_id
        query = urllib.parse.urlencode(params)
        return f"{self.base_url}/unsubscribe/{email_storage_id}?{query}"

    def build_tracking_open_url(self, email_storage_id: str) -> str:
        """Construct open tracking pixel URL."""
        return f"{self.base_url}/track/open/{email_storage_id}"

    def build_tracking_click_url(self, email_storage_id: str, target_url: str) -> str:
        """Construct click tracking redirect URL with safe percent-encoding."""
        encoded_target = urllib.parse.quote(target_url, safe="")
        return f"{self.base_url}/track/click/{email_storage_id}?url={encoded_target}"

    def extract_context_dict(
        self,
        subscriber: Optional[Subscriber] = None,
        custom_context: Optional[Dict[str, Any]] = None,
        email_storage_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        sender_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Normalize all context variables into a flattened, case-insensitive accessible map."""
        now = datetime.now(timezone.utc)
        ctx: Dict[str, Any] = {
            "current_year": str(now.year),
            "year": str(now.year),
            "date": now.strftime("%Y-%m-%d"),
            "company_name": self.company_name,
            "company_address": self.company_address,
            "privacy_policy_url": self.privacy_policy_url,
            "base_url": self.base_url,
            "sender_name": sender_name or settings.DEFAULT_SENDER_NAME,
            "sender_email": sender_email or settings.DEFAULT_SENDER_EMAIL,
        }

        if email_storage_id:
            ctx["email_storage_id"] = email_storage_id
            ctx["storage_id"] = email_storage_id

        if campaign_id:
            ctx["campaign_id"] = campaign_id

        email_val = ""
        if subscriber:
            email_val = subscriber.email
            ctx["email"] = subscriber.email
            ctx["first_name"] = subscriber.first_name or ""
            ctx["last_name"] = subscriber.last_name or ""
            
            # Combine first and last name
            full_name = f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
            ctx["name"] = full_name or subscriber.email
            ctx["full_name"] = full_name

            # Unpack custom attributes
            if subscriber.custom_attributes:
                for k, v in subscriber.custom_attributes.items():
                    ctx[k] = v
                    ctx[f"custom.{k}"] = v
                    ctx[f"custom_attributes.{k}"] = v

        if custom_context:
            for k, v in custom_context.items():
                ctx[k] = v
                if isinstance(v, dict):
                    for sub_k, sub_v in v.items():
                        ctx[f"{k}.{sub_k}"] = sub_v
            if "email" in custom_context and not email_val:
                email_val = str(custom_context["email"])

        # Generate unsubscribe url if storage_id and email are available
        if email_storage_id and email_val:
            unsub_url = self.build_unsubscribe_url(email_storage_id, email_val, campaign_id)
            ctx["unsubscribe_url"] = unsub_url
            ctx["unsub_url"] = unsub_url
        elif "unsubscribe_url" not in ctx:
            ctx["unsubscribe_url"] = f"{self.base_url}/unsubscribe"
            ctx["unsub_url"] = f"{self.base_url}/unsubscribe"

        return ctx

    def interpolate_tags(self, template_str: Optional[str], context: Dict[str, Any]) -> str:
        """
        Interpolate {{ merge_tags }} in template string using provided context dictionary.
        Supports fallback default values: {{ first_name | default="Friend" }} or {{ first_name | Friend }}.
        Case-insensitive and whitespace tolerant.
        """
        if not template_str:
            return ""

        # Normalize context keys to lowercase without underscores/dots for fuzzy matching
        normalized_lookup: Dict[str, Any] = {}
        for k, v in context.items():
            normalized_lookup[k.lower()] = v
            clean_k = k.lower().replace("_", "").replace(".", "")
            normalized_lookup[clean_k] = v

        def _replace_match(match: re.Match) -> str:
            raw_tag = match.group("tag").strip()
            default_val = match.group("default")
            tag_lower = raw_tag.lower()
            clean_tag = tag_lower.replace("_", "").replace(".", "")

            if tag_lower in normalized_lookup:
                val = normalized_lookup[tag_lower]
                return str(val) if val is not None else (default_val or "")
            elif clean_tag in normalized_lookup:
                val = normalized_lookup[clean_tag]
                return str(val) if val is not None else (default_val or "")
            elif default_val is not None:
                return default_val.strip()
            return ""

        return self.MERGE_TAG_REGEX.sub(_replace_match, template_str)

    def inject_tracking_pixel(self, html_content: str, email_storage_id: str) -> str:
        """
        Inject 1x1 open tracking pixel before </body> or append to HTML.
        Uses hidden styling compliant with modern email clients.
        """
        if not html_content:
            return ""

        open_url = self.build_tracking_open_url(email_storage_id)
        pixel_tag = (
            f'<img src="{open_url}" width="1" height="1" alt="" '
            f'style="display:none !important;width:1px !important;height:1px !important;'
            f'max-height:0 !important;max-width:0 !important;opacity:0 !important;'
            f'overflow:hidden !important;mso-hide:all;" />'
        )

        # Case-insensitive replacement before </body>
        body_close_pattern = re.compile(r"</body>", re.IGNORECASE)
        if body_close_pattern.search(html_content):
            return body_close_pattern.sub(f"{pixel_tag}\n</body>", html_content, count=1)
        
        # If no body close tag, check </html>
        html_close_pattern = re.compile(r"</html>", re.IGNORECASE)
        if html_close_pattern.search(html_content):
            return html_close_pattern.sub(f"{pixel_tag}\n</html>", html_content, count=1)

        # Otherwise append at the end
        return f"{html_content}\n{pixel_tag}"

    def rewrite_links_for_tracking(
        self,
        html_content: str,
        email_storage_id: str,
        unsubscribe_url: Optional[str] = None,
    ) -> str:
        """
        Rewrite all <a href="..."> hyperlinks with click tracking redirects.
        Preserves mailto:, tel:, sms:, anchor links (#), and unsubscribe URLs.
        """
        if not html_content or "<a" not in html_content.lower():
            return html_content

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                if not href:
                    continue

                href_lower = href.lower()

                # Preserve non-HTTP schemes & anchors
                if href_lower.startswith(("mailto:", "tel:", "sms:", "javascript:", "#")):
                    continue

                # Preserve tags flagged with no-track
                if a_tag.get("data-no-track") or "no-track" in a_tag.get("class", []):
                    continue

                # Preserve unsubscribe URLs or existing tracking endpoints
                if unsubscribe_url and href == unsubscribe_url:
                    continue
                if "/track/click/" in href_lower or "/track/open/" in href_lower or "/unsubscribe" in href_lower:
                    continue

                # Rewrite to tracking URL
                tracking_link = self.build_tracking_click_url(email_storage_id, href)
                a_tag["href"] = tracking_link

            return str(soup)
        except Exception as e:
            logger.warning("Error parsing HTML for click tracking: %s. Falling back to regex.", e)
            return self._regex_rewrite_links(html_content, email_storage_id, unsubscribe_url)

    def _regex_rewrite_links(
        self,
        html_content: str,
        email_storage_id: str,
        unsubscribe_url: Optional[str] = None,
    ) -> str:
        """Fallback regex-based hyperlink rewriter if BeautifulSoup parsing encounters unexpected tokens."""
        def _replace_href(match: re.Match) -> str:
            prefix = match.group(1)
            quote = match.group(2)
            url = match.group(3).strip()
            suffix = match.group(4)

            url_lower = url.lower()
            if (
                url_lower.startswith(("mailto:", "tel:", "sms:", "javascript:", "#"))
                or (unsubscribe_url and url == unsubscribe_url)
                or "/track/" in url_lower
                or "/unsubscribe" in url_lower
            ):
                return match.group(0)

            tracking_url = self.build_tracking_click_url(email_storage_id, url)
            return f"{prefix}{quote}{tracking_url}{quote}{suffix}"

        pattern = re.compile(r'(<a\s+[^>]*?href=)(["\'])(.*?)\2([^>]*?>)', re.IGNORECASE)
        return pattern.sub(_replace_href, html_content)

    def inject_compliance_footer(
        self,
        html_content: str,
        unsubscribe_url: str,
        custom_company_name: Optional[str] = None,
        custom_company_address: Optional[str] = None,
    ) -> str:
        """
        Inject CAN-SPAM and GDPR compliant footer with unsubscribe link if not already present.
        """
        if not html_content:
            return ""

        # Check if already contains an unsubscribe link or compliance footer marker
        lower_html = html_content.lower()
        if "unsubscribe" in lower_html and (unsubscribe_url.lower() in lower_html or "manage preferences" in lower_html):
            return html_content
        if 'id="email-compliance-footer"' in lower_html or 'class="compliance-footer"' in lower_html:
            return html_content

        company = custom_company_name or self.company_name
        address = custom_company_address or self.company_address

        footer_html = f"""
<!-- CAN-SPAM & GDPR Compliant Footer -->
<div id="email-compliance-footer" class="compliance-footer" style="margin-top: 32px; padding: 24px 16px; border-top: 1px solid #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 12px; color: #64748b; line-height: 1.6; text-align: center;">
  <p style="margin: 0 0 8px 0;">You are receiving this email because you are registered with or subscribed to our updates.</p>
  <p style="margin: 0 0 10px 0; font-weight: 500; color: #475569;">{html.escape(company)} &bull; {html.escape(address)}</p>
  <p style="margin: 0;">
    <a href="{unsubscribe_url}" data-no-track="true" style="color: #2563eb; text-decoration: underline; font-weight: 500;">Unsubscribe</a> &bull;
    <a href="{unsubscribe_url}&action=preferences" data-no-track="true" style="color: #2563eb; text-decoration: underline;">Manage Preferences</a> &bull;
    <a href="{self.privacy_policy_url}" data-no-track="true" style="color: #2563eb; text-decoration: underline;">Privacy Policy</a>
  </p>
</div>
"""
        # Insert before </body> if present
        body_close_pattern = re.compile(r"</body>", re.IGNORECASE)
        if body_close_pattern.search(html_content):
            return body_close_pattern.sub(f"{footer_html}\n</body>", html_content, count=1)

        # Or before </html>
        html_close_pattern = re.compile(r"</html>", re.IGNORECASE)
        if html_close_pattern.search(html_content):
            return html_close_pattern.sub(f"{footer_html}\n</html>", html_content, count=1)

        return f"{html_content}\n{footer_html}"

    def html_to_plain_text(self, html_content: str) -> str:
        """Convert HTML to clean plain text format for multipart/alternative fallback."""
        if not html_content:
            return ""

        try:
            soup = BeautifulSoup(html_content, "html.parser")
            # Strip script and style elements
            for elem in soup(["script", "style", "head", "title", "meta", "noscript"]):
                elem.extract()

            # Replace <a> tags with Text (URL)
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                text = a.get_text(strip=True)
                if text and href and text != href and not href.startswith("#"):
                    a.replace_with(f"{text} ({href})")

            # Replace <br> and <p> with newlines
            for br in soup.find_all("br"):
                br.replace_with("\n")
            for p in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"]):
                p.insert_after("\n")

            text = soup.get_text()
            # Collapse multiple blank lines
            lines = [line.strip() for line in text.splitlines()]
            text = "\n".join(chunk for chunk in lines if chunk)
            return text
        except Exception:
            # Fallback regex strip
            clean = re.sub(r"<[^>]+>", " ", html_content)
            return "\n".join(line.strip() for line in clean.splitlines() if line.strip())

    def render_email(
        self,
        subject_template: str,
        html_template: str,
        text_template: Optional[str] = None,
        subscriber: Optional[Subscriber] = None,
        custom_context: Optional[Dict[str, Any]] = None,
        email_storage_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        sender_name: Optional[str] = None,
        sender_email: Optional[str] = None,
        track_opens: bool = True,
        track_clicks: bool = True,
        inject_footer: bool = True,
    ) -> RenderedEmailContent:
        """
        Complete end-to-end rendering pipeline:
        1. Build context & unsubscribe token.
        2. Interpolate merge tags in subject, HTML, and text.
        3. Inject CAN-SPAM / GDPR footer.
        4. Rewrite links for click tracking.
        5. Inject 1x1 open tracking pixel.
        """
        email_storage_id = email_storage_id or "preview_id"
        context = self.extract_context_dict(
            subscriber=subscriber,
            custom_context=custom_context,
            email_storage_id=email_storage_id,
            campaign_id=campaign_id,
            sender_name=sender_name,
            sender_email=sender_email,
        )

        unsubscribe_url = context.get("unsubscribe_url", f"{self.base_url}/unsubscribe")

        # Step 1: Interpolate variables
        interpolated_subject = self.interpolate_tags(subject_template, context)
        interpolated_html = self.interpolate_tags(html_template, context)
        
        if text_template:
            interpolated_text = self.interpolate_tags(text_template, context)
        else:
            interpolated_text = self.html_to_plain_text(interpolated_html)

        rendered_html = interpolated_html

        # Step 2: Inject Compliance Footer
        if inject_footer:
            rendered_html = self.inject_compliance_footer(
                html_content=rendered_html,
                unsubscribe_url=unsubscribe_url,
            )

        # Step 3: Rewrite links for click tracking
        if track_clicks and email_storage_id != "preview_id":
            rendered_html = self.rewrite_links_for_tracking(
                html_content=rendered_html,
                email_storage_id=email_storage_id,
                unsubscribe_url=unsubscribe_url,
            )

        # Step 4: Inject open tracking pixel
        if track_opens and email_storage_id != "preview_id":
            rendered_html = self.inject_tracking_pixel(
                html_content=rendered_html,
                email_storage_id=email_storage_id,
            )

        return RenderedEmailContent(
            subject=interpolated_subject,
            body_text=interpolated_text,
            body_html=interpolated_html,
            rendered_html=rendered_html,
            unsubscribe_url=unsubscribe_url,
        )


# Global template engine singleton
template_engine = TemplateEngine()
