"""Serializable result types used by the smart player session."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ProbeResult:
    """Lightweight playback simulation for one resolved host."""

    ok: bool = False
    manifest_type: str = "unknown"  # hls | dash | mp4 | unknown
    status: int = 0
    latency_ms: int = 0
    segment_status: int | None = None
    codecs: list[str] = field(default_factory=list)
    subtitle_status: dict[str, int] = field(default_factory=dict)
    score: float = -100.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    """One playable video candidate plus its native subtitle tracks."""

    host: str
    url: str
    manifest_type: str
    sources: list[dict] = field(default_factory=list)
    subtitles: list[dict] = field(default_factory=list)
    score: float = 0.0
    probe: ProbeResult = field(default_factory=ProbeResult)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["probe"] = self.probe.to_dict()
        return data


def session_to_dict(
    *,
    media_type: str,
    media_id: str,
    season: str | None,
    episode: str | None,
    candidates: list[Candidate],
    subtitles: list[dict],
    audit: dict,
    generated_at: float,
) -> dict:
    """Build the JSON object injected into the player page.

    Only signed, same-origin proxy URLs are included. Raw provider headers,
    CloudFront cookies and ``api_password`` never reach the browser.
    """
    return {
        "media": {
            "type": media_type,
            "id": media_id,
            "season": season,
            "episode": episode,
        },
        "candidates": [candidate.to_dict() for candidate in candidates],
        "subtitles": subtitles,
        "audit": audit,
        "generated_at": generated_at,
    }
