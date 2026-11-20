"""Which episodes and leads a digest covers. Reads artifacts only."""

import hashlib
import json
import logging
from dataclasses import dataclass

from earmark.artifacts import ArtifactStore
from earmark.digest.model import DigestEpisode, DigestLead
from earmark.digest.prompts import DIGEST_SCHEMA_VERSION
from earmark.digest.window import DigestWindow
from earmark.errors import PermanentError, TransientError
from earmark.models import Analysis, Episode, ShareRecord, entity_key

logger = logging.getLogger(__name__)

UNREADABLE_ROOT = "the episode files under EARMARK_DATA_DIR cannot be read"


@dataclass(frozen=True, slots=True)
class Collected:
    episodes: tuple[DigestEpisode, ...]
    warnings: tuple[str, ...]

    @property
    def leads(self) -> tuple[DigestLead, ...]:
        return tuple(lead for e in self.episodes for lead in e.leads)


def _read_analyses(artifacts: ArtifactStore, key: str, warnings: list[str]) -> list[Analysis]:
    found: list[Analysis] = []
    for prompt_hash in artifacts.list_analysis_hashes(key):
        try:
            analysis = artifacts.read_analysis(key, prompt_hash)
        except (PermanentError, OSError):
            logger.warning("episode %s: analysis %s is unreadable", key, prompt_hash)
            warnings.append(
                f"episode {key}: analysis {prompt_hash} is unreadable; its leads are left out"
            )
            continue
        if analysis is not None:
            found.append(analysis)
    found.sort(key=lambda a: (a.created_at, a.prompt_hash))
    return found


def collect(artifacts: ArtifactStore, window: DigestWindow) -> Collected:
    try:
        keys = artifacts.list_episode_keys()
    except OSError:
        raise TransientError(UNREADABLE_ROOT) from None
    warnings: list[str] = []
    picked: list[tuple[Episode, ShareRecord]] = []
    for key in keys:
        try:
            episode = artifacts.read_episode(key)
            share = artifacts.read_share(key)
        except (PermanentError, OSError):
            logger.warning("episode %s: unreadable files under EARMARK_DATA_DIR", key)
            warnings.append(
                f"episode {key}: unreadable files under EARMARK_DATA_DIR; left out of the digest"
            )
            continue
        if episode is None:
            continue
        if share is None:
            logger.debug("episode %s has no share record; not in any digest", key)
            continue
        if window.contains(share.first_shared_at):
            picked.append((episode, share))
    picked.sort(key=lambda p: (p[1].first_shared_at, p[0].key))

    episodes: list[DigestEpisode] = []
    counter = 0
    for episode, share in picked:
        analyses = _read_analyses(artifacts, episode.key, warnings)
        seen: set[str] = set()
        leads: list[DigestLead] = []
        for analysis in analyses:
            for lead in analysis.research_leads:
                dedupe = entity_key(lead.text) or " ".join(lead.text.split())
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                counter += 1
                leads.append(
                    DigestLead(
                        lead_id=f"L{counter}",
                        episode_key=episode.key,
                        text=lead.text,
                        rationale=lead.rationale,
                        timestamp_s=lead.timestamp_s,
                    )
                )
        episodes.append(DigestEpisode(episode, share.first_shared_at, len(analyses), tuple(leads)))
    return Collected(tuple(episodes), tuple(dict.fromkeys(warnings)))


def digest_input_hash(model: str, collected: Collected) -> str:
    shows = {e.episode.key: e.episode for e in collected.episodes}
    payload = {
        "v": DIGEST_SCHEMA_VERSION,
        "model": model,
        "leads": [
            [
                lead.lead_id,
                lead.episode_key,
                lead.text,
                lead.rationale,
                shows[lead.episode_key].show_title,
                shows[lead.episode_key].title,
            ]
            for lead in collected.leads
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]
