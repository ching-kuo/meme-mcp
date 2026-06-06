"""Native MCP OAuth 2.1 authorization-server package.

Houses the server-side OAuth authorization server that lets Claude's native
custom-connector UI connect directly (no ``mcp-remote`` bridge): the table DDL
(:mod:`schema`), the SQLite persistence layer (:mod:`store`), the SDK provider
implementation (:mod:`provider`), and the parent-app authorize/consent bridge
(:mod:`consent`).

The package is inert unless ``OAUTH_AS_ENABLED`` is set; existing PAT-only
deployments import nothing from here.
"""
