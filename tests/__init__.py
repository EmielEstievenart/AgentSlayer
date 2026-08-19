# Exists so pytest imports these packages as tests.<dir> instead of putting
# tests/executor/ itself on sys.path - where its mcp/ directory would shadow the
# real `mcp` SDK for the whole test process (see docs/design/mcp.md section 7).
