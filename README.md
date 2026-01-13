# Scopus MCP Server

<!-- mcp-name: io.github.qwe4559999/scopus-mcp -->

[中文](README_CN.md) | **English**

This is a Model Context Protocol (MCP) server that provides access to the Elsevier Scopus API. It allows AI assistants to search for academic papers, retrieve abstracts, and look up author profiles.

**Please note that requesting an Elsevier Scopus API key generally requires that your organization or institution has a subscription to Elsevier database services.**

## Configuration

### Setup Steps
1.  Go to [Elsevier Developer Portal](https://dev.elsevier.com/) to apply for an API key.
2.  Fill the key into `config.json` in the project folder.
3.  Edit `MCP_tool_config.json`, modifying the folder path (pay attention to the slash direction).
4.  Finally, import the configuration into your MCP client (e.g., Claude Desktop) by copying the content of `MCP_tool_config.json`.

## 🚀 Quick Start (Zero Setup)

If you use Claude Desktop, you can skip downloading the code and just configure it directly:

1.  **Get Key**: Get a free API Key from [Elsevier Developer Portal](https://dev.elsevier.com/).
2.  **Configure**: Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS).
3.  **Add**:

```json
{
  "mcpServers": {
    "scopus-assistant": {
      "command": "uvx",
      "args": [
        "scopus-mcp"
      ],
      "env": {
        "SCOPUS_API_KEY": "PUT_YOUR_KEY_HERE"
      }
    }
  }
}
```

*(Requires [uv](https://docs.astral.sh/uv/) installed)*

### Using with Trae

In Trae Settings -> MCP Servers -> Click **Add** -> Select **Manual Configuration (JSON)**, then paste:

```json
{
  "mcpServers": {
    "scopus-assistant": {
      "command": "uvx",
      "args": [
        "scopus-mcp"
      ],
      "env": {
        "SCOPUS_API_KEY": "PUT_YOUR_KEY_HERE"
      }
    }
  }
}
```

### Using with Cursor

1.  Open **Cursor Settings** -> **Features** -> **MCP Servers**.
2.  Click **+ Add New MCP Server**.
3.  Fill in the details:
    *   **Name**: `scopus-mcp`
    *   **Type**: `command` (stdio)
    *   **Command**: `uvx scopus-mcp`
4.  **Important**: You need to set `SCOPUS_API_KEY` in your system environment variables.

## Installation

1.  Ensure you have Python 3.10+ installed.
2.  Install dependencies:
    ```bash
    pip install .
    ```

## Usage

### Running the Server

You can run the server using `uvx` (recommended) or directly with python.

```bash
# Using uvx
uvx --from . scopus-mcp

# Or directly
python -m scopus_mcp.server
```

### Available Tools

1.  **`search_scopus`**
    -   Searches the Scopus database using the standard query syntax.
    -   Arguments:
        -   `query` (string): The search query (e.g., `TITLE("Artificial Intelligence")`).
        -   `count` (integer): Number of results to return (default: 5).
        -   `sort` (string): Sort order (e.g., `coverDate`).

2.  **`get_abstract_details`**
    -   Retrieves detailed information for a specific document.
    -   Arguments:
        -   `scopus_id` (string): The Scopus ID of the document.

3.  **`get_author_profile`**
    -   Retrieves an author's profile information.
    -   Arguments:
        -   `author_id` (string): The Scopus Author ID.

## Development

Run tests with:
```bash
pytest
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments & Contributors

<a href="https://github.com/qwe4559999/scopus-mcp/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=qwe4559999/scopus-mcp" />
</a>

*   **[thinktraveller](https://github.com/thinktraveller)** - *Initial Work & Core Development*
*   **[qwe4559999](https://github.com/qwe4559999)** - *Maintainer*
