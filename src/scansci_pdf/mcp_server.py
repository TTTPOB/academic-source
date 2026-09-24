"""Compatibility launcher for the single academic-source MCP implementation.

Modified by academic-source: the duplicate full-text MCP server is retired;
source retrieval and extraction implementations remain available as libraries.
"""


def main() -> None:
    from academic_source.interfaces.mcp import create_mcp
    from academic_source.services.application import Application
    from academic_source.settings import Settings

    application = Application(Settings.load())
    try:
        create_mcp(application, stdio=True).run(transport="stdio")
    finally:
        application.close()


if __name__ == "__main__":
    main()
