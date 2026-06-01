"""Reverse-image enrichment: recover a meme's web identity before VLM analysis.

The single provider is Google Cloud Vision Web Detection (see ``client.py``).
The client only parses the Vision wire format into a typed, never-raising
``WebDetectionResult``; sanitization of all web-recovered text is the upload
service's job (KTD6), not the client's.
"""

from meme_mcp.reverse_image.client import (
    GoogleVisionClient,
    OriginCandidate,
    WebDetectionResult,
    WebGrounding,
)

__all__ = [
    "GoogleVisionClient",
    "OriginCandidate",
    "WebDetectionResult",
    "WebGrounding",
]
