import json
from pathlib import Path

from app_utils.esi_testing import EsiClientStub, EsiEndpoint


def load_test_data():
    file_path = Path(__file__).parent / "esi.json"
    with file_path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


_endpoints = [
    EsiEndpoint(
        "Character",
        "get_characters_character_id_notifications",
        "character_id",
    ),
    EsiEndpoint(
        "Corporation",
        "get_corporations_corporation_id_structures",
        "corporation_id",
        needs_token=True,
    ),
    EsiEndpoint(
        "Universe",
        "get_universe_structures_structure_id",
        "structure_id",
        needs_token=True,
    ),
]

esi_client_stub = EsiClientStub(load_test_data(), endpoints=_endpoints)
esi_client_error_stub = EsiClientStub(
    load_test_data(), endpoints=_endpoints, http_error=True
)