# AgentCore Gateway with BrightData MCP — Competitive Price Intelligence

A competitive price intelligence agent built on **Amazon Bedrock AgentCore Gateway** and **BrightData's remote MCP server**. The agent scrapes live product pricing across retailers, tracks user-defined price thresholds, and persists preferences across sessions using AgentCore's long-term and short-term memory.

## Architecture

```
User
 │
 ▼
Strands Agent (Claude via Bedrock)
 │
 ├── AgentCore Gateway (MCP Protocol)
 │    └── BrightData MCP Target (web scraping tools)
 │
 └── AgentCore Memory
      ├── Short-term: raw conversation events per session
      └── Long-term: extracted user preferences (products, retailers, thresholds)
```

## What It Does

- Sets up an **AgentCore Gateway** that proxies requests to BrightData's MCP server over Streamable HTTP
- Uses **BrightData tools** to scrape real-time product prices from Amazon, Best Buy, and other retailers
- Maintains **short-term memory** (raw conversation events) and **long-term memory** (extracted preferences like tracked products, price alert thresholds, and competitor brands)
- Demonstrates **cross-session memory persistence** — a new agent session automatically reloads the user's preferences without being told again

## Prerequisites

- Python 3.11+
- AWS account with an IAM user/role that has `bedrock-agentcore:*` permissions
- [BrightData](https://brightdata.com) account and API token
- AWS credentials configured locally (`~/.aws/credentials` or environment variables)

## Setup

1. Clone the repo and install dependencies:

```bash
git clone https://github.com/yeshwanthlm/AgentCore-Gateway-with-BrightData-MCP.git
cd AgentCore-Gateway-with-BrightData-MCP
pip install -r requirements.txt
```

2. Set your BrightData API token:

```bash
export BRIGHTDATA_API_TOKEN=your_token_here
```

Or edit `agent.py` directly and replace `<YOUR_BRIGHTDATA_API_TOKEN>`.

## Running

### As a Python script

```bash
python agent.py
```

### As a Jupyter notebook

```bash
jupyter notebook agent.ipynb
```

The notebook walks through each step interactively — useful for understanding the setup and inspecting memory state between steps.

## How It Works

### Step-by-step flow

1. Creates an IAM role (`brightdata-agentcore-role`) that trusts `bedrock-agentcore.amazonaws.com`
2. Creates an **AgentCore Gateway** with MCP protocol and no authorizer
3. Registers BrightData's `/mcp` endpoint as a **Gateway Target**
4. Waits for the target to reach `READY` status
5. Creates an **AgentCore Memory** resource with a `USER_PREFERENCE` strategy
6. Instantiates a Strands `Agent` with BrightData tools and memory hooks
7. Runs two sessions to demonstrate memory persistence across sessions

### Memory hooks

`PriceIntelMemoryHookProvider` implements two hooks:

- `on_agent_initialized` — retrieves long-term preferences from previous sessions and injects them into the system prompt before the first turn
- `on_after_invocation` — saves each conversation turn (user + assistant messages) to short-term memory so AgentCore can extract structured preferences in the background

### Memory strategy

The `USER_PREFERENCE` strategy extracts structured signals from raw conversation events and stores them in the namespace `user/{actorId}/price_intel_preferences`. This includes:

- Products and categories being tracked
- Preferred retailers and marketplaces
- Price alert thresholds
- Competitor brands of interest

## Project Structure

```
.
├── agent.py          # Standalone script — full setup + agent loop
├── agent.ipynb       # Jupyter notebook — step-by-step interactive version
└── requirements.txt  # Python dependencies
```

## Configuration

Key constants at the top of `agent.py` (or the config cell in the notebook):

| Variable | Description |
|---|---|
| `AWS_REGION` | AWS region to deploy resources (default: `us-east-1`) |
| `BRIGHTDATA_API_TOKEN` | Your BrightData API token |
| `GATEWAY_NAME` | Name for the AgentCore Gateway |
| `ROLE_NAME` | IAM role name to create/reuse |
| `USER_ID` | User identifier for memory namespacing |
| `MEMORY_ID` | Set to an existing memory ID to reuse it across runs |

## IAM Permissions Required

Your AWS credentials need at minimum:

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock-agentcore:*",
    "iam:CreateRole",
    "iam:PutRolePolicy",
    "iam:GetRole",
    "sts:GetCallerIdentity"
  ],
  "Resource": "*"
}
```

## Notes

- AgentCore Gateway requires **Streamable HTTP** transport — SSE is not supported
- Memory extraction (short-term → long-term) runs as a background process and may take 30–60 seconds after a session ends
- All setup calls are idempotent — re-running the script will reuse existing resources
- Set `MEMORY_ID` in `agent.py` to persist memory across multiple script runs
