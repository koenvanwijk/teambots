"""A tiny MCP server (streamable HTTP) with one tool, for tests."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("calc", host="127.0.0.1", port=18792)


@mcp.tool()
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
