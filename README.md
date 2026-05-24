# meme-mcp

Private meme retrieval and rendering service with a hosted MCP surface and a small web app.

This repository is initialized from `docs/plans/2026-05-24-001-feat-meme-mcp-v1-plan.md`.
The current implementation is test-first and covers the v1 foundations: envelope errors, config
validation, PAT hashing, upload validation, duplicate checks, content-addressed rendering storage,
retrieval ranking, and a FastAPI health/error surface.

