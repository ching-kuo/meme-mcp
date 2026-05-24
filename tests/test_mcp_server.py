import json

from meme_mcp.mcp.server import EXPECTED_TOOLS, tool_schemas


def test_mcp_exposes_exactly_find_and_generate_with_small_schemas() -> None:
    schemas = tool_schemas()
    assert set(schemas) == EXPECTED_TOOLS == {"find", "generate"}
    budget = sum(len(json.dumps(schema)) for schema in schemas.values())
    assert budget < 4096

