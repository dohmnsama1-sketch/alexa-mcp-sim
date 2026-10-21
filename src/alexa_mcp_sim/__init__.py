"""alexa-mcp-sim: an open-source, Echo Show-style voice simulator for any Streamable HTTP MCP server.

Amazon's Alexa+ add-on tools (MCP Toolkit, Web Simulator) are in a closed preview, so builders have no way to
*hear and see* their MCP server the way an Alexa+ customer would. This fills that gap: push-to-talk voice
(browser speech recognition + synthesis), speaker profiles, an MCP Apps (ui://) host for screen cards, and a
live JSON-RPC trace, with two planners: an offline interaction model (sample utterances, like classic Alexa
skills) and Amazon Bedrock (Converse API tool use) for free-form requests.
"""

__version__ = "0.1.0"
