"""Headers for serving generated previews. Pure functions so the security-relevant parts are unit-testable."""
import posixpath

# Explicit map: mimetypes consults the Windows registry, where ".js" can be "text/plain", which `nosniff` then makes
# the browser refuse as a module script.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".map": "application/json", ".txt": "text/plain; charset=utf-8",
}


def content_type(filename: str) -> str:
    return CONTENT_TYPES.get(posixpath.splitext(filename)[1].lower(), "application/octet-stream")


def preview_csp(frame_ancestors: list[str]) -> str:
    """`sandbox` (without allow-same-origin) gives the page an opaque origin even if opened directly; no network of any kind."""
    return (
        "sandbox allow-scripts; default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors " + (" ".join(frame_ancestors) or "'none'")
    )


def preview_headers(frame_ancestors: list[str], filename: str) -> dict[str, str]:
    """Every response header of a preview file (shared by the API route and offline browser probes)."""
    return {
        "Content-Type": content_type(filename),
        "Content-Security-Policy": preview_csp(frame_ancestors),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        # Public static output; lets the sandboxed (opaque-origin) page load its own module script.
        "Access-Control-Allow-Origin": "*",
    }
