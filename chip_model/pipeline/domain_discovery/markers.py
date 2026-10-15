"""Shared, dependency-free start markers for API and legacy dispatch guards."""
from collections.abc import Mapping


def is_domain_request(payload):
    """request_id alone is a generic key; explicit domain/contracts are V3."""
    return isinstance(payload, Mapping) and (
        payload.get('pipeline') == 'hermes-open-web-v3'
        or any(key in payload for key in ('domain', 'chip', 'resume_run_id', 'scope_type',
            'schema_version', 'contract_version', 'source_locator', 'source_db', 'data_root')))
