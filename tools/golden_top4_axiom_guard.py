"""Axiom admission interlock. No approved live transport is configured yet.

The observation below is OUR internal contract, not an invented Axiom API
schema. A reviewed transport must populate it from real first-party responses.
Fixture observations cannot open the live gate. This module never signs orders.
"""
from __future__ import annotations
import copy
import re
from urllib.parse import urlsplit
from golden_top4_engine import CampaignEngine as UngatedEngine, ARMS, VERSION, write_json

class AxiomAccessBlocked(RuntimeError):
    pass


def load_live_axiom_source():
    # Deliberately no cookie import, guessed endpoint or fallback to Solana.
    # Replace only when an approved transport and its live evidence are reviewed.
    raise AxiomAccessBlocked('AXIOM_DATA_ACCESS_NOT_ESTABLISHED: first-party anonymous runner requests returned HTTP 403; approved market-data transport required')


def validate_observation(observation, mint, now_ns, *, fixture_mode=False, max_age_ms=500):
    """Validate a normalized first-party observation; not sufficient alone to
    authenticate a transport. The production loader remains independently closed.
    """
    if not isinstance(observation, dict):
        raise AxiomAccessBlocked('AXIOM_OBSERVATION_MISSING')
    expected = 'SYNTHETIC_FIXTURE' if fixture_mode else 'FIRST_PARTY_LIVE_TRANSPORT'
    if observation.get('evidence_mode') != expected:
        raise AxiomAccessBlocked('AXIOM_EVIDENCE_MODE_MISMATCH')
    if observation.get('mint') != mint or not mint:
        raise AxiomAccessBlocked('AXIOM_MINT_MISMATCH')
    p = urlsplit(observation.get('origin', ''))
    host = p.hostname or ''
    try: port = p.port
    except ValueError: raise AxiomAccessBlocked('AXIOM_ORIGIN_INVALID')
    if p.scheme not in ('https', 'wss') or p.username or p.password or port not in (None, 443) or not (host == 'axiom.trade' or host.endswith('.axiom.trade')):
        raise AxiomAccessBlocked('AXIOM_ORIGIN_INVALID')
    if p.query or p.fragment:
        raise AxiomAccessBlocked('AXIOM_ORIGIN_MUST_NOT_CONTAIN_CREDENTIALS')
    stamps = [observation.get(k) for k in ('session_started_ns', 'source_event_ns', 'received_ns')]
    if not isinstance(now_ns, int) or isinstance(now_ns, bool) or not all(isinstance(x, int) and not isinstance(x, bool) for x in stamps):
        raise AxiomAccessBlocked('AXIOM_TIMESTAMPS_INVALID')
    started, event, received = stamps
    if not 0 < started <= event <= received <= now_ns:
        raise AxiomAccessBlocked('AXIOM_TIMESTAMPS_NONCAUSAL')
    if now_ns-event > max_age_ms*1_000_000 or now_ns-received > max_age_ms*1_000_000:
        raise AxiomAccessBlocked('AXIOM_DATA_STALE')
    if observation.get('market_state') != 'ACTIVE' or observation.get('tls_verified') is not True:
        raise AxiomAccessBlocked('AXIOM_MARKET_OR_TLS_UNVERIFIED')
    if not re.fullmatch(r'[0-9a-f]{64}', str(observation.get('raw_sha256', ''))):
        raise AxiomAccessBlocked('AXIOM_RAW_RECEIPT_MISSING')
    if not observation.get('transport_session_id') or not observation.get('market_id'):
        raise AxiomAccessBlocked('AXIOM_IDENTITY_MISSING')
    return copy.deepcopy(observation)


class CampaignEngine(UngatedEngine):
    """Entry-only interlock: inherited exit management is never disabled by it.

    The source cannot be restored from JSON. It must be freshly established by
    the reviewed live-loader; synthetic tests use an explicitly separate mode.
    """
    def __init__(self, config, state=None, *, fixture_mode=False):
        super().__init__(config, state, fixture_mode=fixture_mode)
        self.axiom_source = None
        self.axiom_entry_receipts = copy.deepcopy((state or {}).get('axiom_entry_receipts', {}))

    def submit(self, decision, state, now):
        if not self.fixture_mode:
            # There is no approved production source in this build. Loading it
            # raises before a model, its observer, or any transaction can enter.
            try:
                source = self.axiom_source or load_live_axiom_source()
                observation = validate_observation(source.observe(decision['mint'], now), decision['mint'], now)
            except AxiomAccessBlocked as exc:
                rejection = {'mint': decision['mint'], 'reason': str(exc), 'ns': now}
                self.controls.rejections.append(rejection)
                self._event({'kind': 'ENTRY_VETO', 'source': 'AXIOM_REQUIREMENT', **rejection})
                return False
            self.axiom_entry_receipts[decision['mint']] = observation
        return super().submit(decision, state, now)

    def forward_counts(self):
        counts = super().forward_counts()
        if self.fixture_mode:
            return counts
        # All eligible receipts must have been captured before that entry.
        for arm, account in self.accounts.items():
            accepted = 0
            for position in account['ledger']:
                receipt = self.axiom_entry_receipts.get(position['mint'])
                try:
                    validate_observation(receipt, position['mint'], self.decisions[position['mint']]['decision_ns'])
                except (AxiomAccessBlocked, KeyError, ValueError):
                    continue
                uncertain = self.runtime.candidate.uncertain_mints if arm == 'FULL_3S_V2' else self.control_uncertain
                post = position.get('post') or {}
                if position['mint'] not in uncertain and self.proofs.get(position['mint'], {}).get('verified') is True and post.get('complete') is True and post.get('coverage') == 'OBSERVED':
                    accepted += 1
            counts[arm] = accepted
        return counts

    def persistence(self):
        data = super().persistence()
        data['axiom_entry_receipts'] = copy.deepcopy(self.axiom_entry_receipts)
        data['axiom_requirement_enforced'] = True
        return data
