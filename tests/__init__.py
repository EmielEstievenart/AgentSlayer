# Exists so pytest imports these packages as tests.<dir> instead of putting
# tests/ itself on sys.path - where tests/mcp/ would shadow the real `mcp`
# SDK for the whole test process (see docs/design/mcp.md section 7).
