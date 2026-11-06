"""Absolute-path entry point; independent of the MCP client's working directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant_service.study_agent.mcp import main

if __name__ == "__main__":
    main()
