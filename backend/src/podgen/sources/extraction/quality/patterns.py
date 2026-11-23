"""Legacy ingestion recognition patterns; technical availability only."""

import re

_NBSP_CHARS_RE = re.compile(r"[\u00a0\u2007\u202f\u2060\ufeff]")
_INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)]\((?:https?://|/)[^)]+\)")
_EMPTY_INLINE_LINK_RE = re.compile(r"\[\s*]\((?:https?://|/)[^)]+\)", re.IGNORECASE)
_LINK_REF_DEF_RE = re.compile(r"^\[[^\]]+]:\s+\S+\s*$", re.IGNORECASE)
_DIVIDER_RE = re.compile(r"^[-=*_]{3,}$")
_SETEXT_UNDERLINE_RE = re.compile(r"^[-=]{3,}$")
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$")
_TITLE_LINE_RE = re.compile(r"^\s*title:\s*(.+)$", re.IGNORECASE)
_HAS_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)
_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r"[^\S\n]+")
_TRAILING_SPACE_RE = re.compile(r" +\n")
_TOO_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_JS_LINK_ONLY_RE = re.compile(r"^\[[^\]]*]\(\s*javascript:.*\)$", re.IGNORECASE)
_EMPTY_LINK_ONLY_RE = re.compile(r"^\[[^\]]*]\((?:https?://|/)[^)]+\)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)*(?:\[[ xX]\]\s*)*")
_NAV_LINE_RE = re.compile(
    r"^(skip to(?::|\b)|home|menu|search|sign in|login|subscribe|cookie(s)?|privacy|terms|contact)\b",
    re.IGNORECASE,
)
_SERVICE_LINE_RE = re.compile(
    r"^(published time:\s*\d{4}-\d{2}-\d{2}|loading\s+\[[^\]]+].*|dismiss this warning)$",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"^(topics|countries|series|resources|about|search|menu|home|open external link|external links?)$",
    re.IGNORECASE,
)
_TITLE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_HTTP_403_ERROR_RE = re.compile(
    r"(target url returned error\s*403|returned error\s*403|status(?: code)?\s*[:=]?\s*403|error\s*[:=]?\s*403|^403\s+forbidden\b|403:\s*forbidden)",
    re.IGNORECASE,
)
_HTTP_ERROR_BANNER_RE = re.compile(
    r"(target url returned error\s*[45]\d\d|returned error\s*[45]\d\d|warning:\s*target url returned error\s*[45]\d\d)",
    re.IGNORECASE,
)
_HTTP_ERROR_MAX_CHARS = 1200
_SERVICE_PAGE_MAX_CHARS = 2000
_PUBLISHER_SHELL_MAX_CHARS = 8000
_INVALID_URL_ERROR_RE = re.compile(r"error:\s+this is an invalid url", re.IGNORECASE)
_BOT_CHECK_RE = re.compile(r"verify (?:that )?you are not a bot", re.IGNORECASE)
_JAVASCRIPT_CHALLENGE_RE = re.compile(
    r"(?:enable|turn on) javascript(?: and cookies)?(?: to continue)?|cookies to continue"
    r"|javascript is required to continue",
    re.IGNORECASE,
)
_SECURITY_SERVICE_RE = re.compile(
    r"security check required|complete the security check|unusual activity from your network"
    r"|using a security service to protect itself",
    re.IGNORECASE,
)
_MAINTENANCE_RE = re.compile(r"temporarily unavailable while we perform maintenance", re.IGNORECASE)
_CLOUDFRONT_REQUEST_ID_RE = re.compile(r"\brequest id:\s*[A-Za-z0-9_+=/-]{12,}", re.IGNORECASE)
_MISSING_DOCUMENT_RE = re.compile(
    r"document name you requested.{0,240}could not be found on this server",
    re.IGNORECASE | re.DOTALL,
)
_NOT_FOUND_REQUEST_RE = re.compile(
    r"(?:requested (?:document|page|resource)|page you (?:requested|are looking for)).{0,180}(?:not|could not) be found",
    re.IGNORECASE | re.DOTALL,
)
_COOKIE_CONTROL_PHRASES = (
    "cookie settings",
    "accept only necessary cookies",
    "accept all cookies",
    "manage your cookie preferences",
)
_COOKIE_UI_TOKENS = {
    "accept",
    "consent",
    "cookie",
    "cookies",
    "manage",
    "necessary",
    "preferences",
    "privacy",
    "reject",
    "settings",
}
_ACCESS_FORM_MARKERS = (
    "create an account",
    "email address",
    "enter your email",
    "forgot your password",
    "member portal",
    "sign in to continue",
)
_TARGET_ACCESS_DENIAL_RE = re.compile(
    r"(?:unfortunately\s+)?(?:you|your institution)\s+(?:do|does)\s+not\s+have\s+access"
    r"|full(?:\s+text)?\s+access\s+(?:is\s+)?(?:not\s+available|restricted)"
    r"|this\s+(?:content|article|chapter|document)\s+is\s+not\s+available\s+to\s+you",
    re.IGNORECASE,
)
_ACCESS_ROUTE_MARKERS = (
    "get access",
    "institutional login",
    "institutional access",
    "purchase access",
    "subscribe to access",
    "sign in to access",
)
_PUBLISHER_DOCUMENT_CONTROL_MARKERS = (
    "article contents",
    "book contents",
    "chapter information",
    "download pdf",
    "save pdf",
)
_PREVIEW_ONLY_RE = re.compile(
    r"(?:only\s+a\s+preview|preview\s+only|this\s+is\s+a\s+preview|limited\s+preview)",
    re.IGNORECASE,
)
