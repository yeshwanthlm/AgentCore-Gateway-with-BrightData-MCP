"""
AgentCore Gateway with BrightData MCP — Competitive Price Intelligence

Sets up an Amazon Bedrock AgentCore Gateway that connects to the BrightData remote MCP server,
with long-term and short-term memory to persist user price-tracking preferences across sessions.

Prerequisites:
- AWS account with IAM user/role must have `bedrock-agentcore:*` permissions
- BrightData API token
"""

import boto3
import json
import time
import logging
import os
from datetime import datetime
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────
# CONFIGURATION — update these before running
# ─────────────────────────────────────────────

AWS_REGION = 'us-east-1'

BRIGHTDATA_API_TOKEN    = os.environ.get('BRIGHTDATA_API_TOKEN', '<YOUR_BRIGHTDATA_API_TOKEN>')
BRIGHTDATA_MCP_ENDPOINT = f'https://mcp.brightdata.com/mcp?token={BRIGHTDATA_API_TOKEN}'

GATEWAY_NAME = 'BrightDataGateway'
TARGET_NAME  = 'BrightDataMCPTarget'
ROLE_NAME    = 'brightdata-agentcore-role'

# Memory / session identifiers
USER_ID    = 'user-001'
SESSION_ID = f'price_intel_{datetime.now().strftime("%Y%m%d%H%M%S")}'

# ─────────────────────────────────────────────
# MEMORY ID PLACEHOLDER
# Set this to an existing memory ID to reuse it, or leave as None to create a new one.
# ─────────────────────────────────────────────
MEMORY_ID: str | None = None   # e.g. 'PriceIntelMemory-abc123'

# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('price-intel-agent')


def setup_iam_role(account_id: str) -> str:
    """Create the IAM role for AgentCore Gateway (idempotent)."""
    iam = boto3.client('iam')

    trust_policy = {
        'Version': '2012-10-17',
        'Statement': [{
            'Effect': 'Allow',
            'Principal': {'Service': 'bedrock-agentcore.amazonaws.com'},
            'Action': 'sts:AssumeRole'
        }]
    }

    inline_policy = {
        'Version': '2012-10-17',
        'Statement': [{
            'Effect': 'Allow',
            'Action': ['bedrock-agentcore:InvokeGateway'],
            'Resource': '*'
        }]
    }

    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description='Role for BrightData AgentCore Gateway'
        )
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName='AgentCoreGatewayInvokePolicy',
            PolicyDocument=json.dumps(inline_policy)
        )
        role_arn = role['Role']['Arn']
        logger.info(f'Created role: {role_arn}')
        time.sleep(10)  # allow IAM propagation
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE_NAME)['Role']['Arn']
        logger.info(f'Role already exists: {role_arn}')

    return role_arn


def setup_gateway(agentcore_client, role_arn: str) -> str:
    """Create the AgentCore Gateway (idempotent). Returns gateway_id."""
    try:
        resp = agentcore_client.create_gateway(
            name=GATEWAY_NAME,
            roleArn=role_arn,
            protocolType='MCP',
            authorizerType='NONE'
        )
        gateway_id = resp['gatewayId']
        logger.info(f'Gateway created — ID: {gateway_id}')
        logger.info(f'Gateway ARN: {resp["gatewayArn"]}')
        logger.info(f'Gateway URL: {resp.get("gatewayUrl", "N/A")}')
    except agentcore_client.exceptions.ConflictException:
        items = agentcore_client.list_gateways()['items']
        match = next((g for g in items if g['name'] == GATEWAY_NAME), None)
        if match:
            gateway_id = match['gatewayId']
            logger.info(f'Gateway already exists — ID: {gateway_id}')
        else:
            raise RuntimeError('ConflictException but gateway not found in list')

    return gateway_id


def setup_target(agentcore_client, gateway_id: str) -> str:
    """Add BrightData as an MCP Server Target (idempotent). Returns target_id."""
    try:
        target_resp = agentcore_client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TARGET_NAME,
            description='BrightData Web Scraping & Ecommerce MCP tools',
            targetConfiguration={
                'mcp': {
                    'mcpServer': {
                        'endpoint': BRIGHTDATA_MCP_ENDPOINT
                    }
                }
            }
        )
        target_id = target_resp['targetId']
        logger.info(f'Target created — ID: {target_id}, Status: {target_resp["status"]}')
    except agentcore_client.exceptions.ConflictException:
        items = agentcore_client.list_gateway_targets(gatewayIdentifier=gateway_id)['items']
        match = next((t for t in items if t['name'] == TARGET_NAME), None)
        if match:
            target_id = match['targetId']
            logger.info(f'Target already exists — ID: {target_id}')
        else:
            raise RuntimeError('ConflictException but target not found in list')

    return target_id


def wait_for_target_ready(agentcore_client, gateway_id: str, target_id: str, retries: int = 20):
    """Poll until the gateway target reaches READY status."""
    logger.info('Waiting for target to reach READY status...')
    for i in range(retries):
        t = agentcore_client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id
        )
        status = t['status']
        logger.info(f'  [{i + 1}] Status: {status}')
        if status == 'READY':
            logger.info('Target is READY.')
            return
        elif status in ('FAILED', 'SYNCHRONIZE_UNSUCCESSFUL', 'UPDATE_UNSUCCESSFUL'):
            raise RuntimeError(f'Target failed: {t.get("statusReasons", [])}')
        time.sleep(15)
    raise TimeoutError('Timed out waiting for READY status.')


def setup_memory(memory_client, memory_id_override: str | None = None) -> str:
    """Create or reuse an AgentCore Memory resource. Returns memory_id."""
    from bedrock_agentcore.memory.constants import StrategyType

    if memory_id_override:
        logger.info(f'Using provided memory ID: {memory_id_override}')
        return memory_id_override

    MEMORY_NAME = 'PriceIntelMemory'

    strategies = [
        {
            StrategyType.USER_PREFERENCE.value: {
                'name': 'PriceIntelPreferences',
                'description': (
                    'Captures competitive price intelligence preferences including: '
                    'products and categories being tracked, preferred retailers and marketplaces, '
                    'price alert thresholds, competitor brands of interest, and monitoring frequency.'
                ),
                'namespaces': ['user/{actorId}/price_intel_preferences']
            }
        }
    ]

    try:
        memory = memory_client.create_memory_and_wait(
            name=MEMORY_NAME,
            strategies=strategies,
            description='Memory for competitive price intelligence agent — stores user tracking preferences',
            event_expiry_days=30,
            max_wait=300,
            poll_interval=10
        )
        mem_id = memory['id']
        logger.info(f'Created memory: {mem_id}')
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationException' and 'already exists' in str(e):
            memories = memory_client.list_memories()
            mem_id = next((m['id'] for m in memories if m['id'].startswith(MEMORY_NAME)), None)
            logger.info(f'Memory already exists. Using: {mem_id}')
        else:
            raise

    return mem_id


# ─────────────────────────────────────────────
# Memory Hook Provider
# ─────────────────────────────────────────────

from strands.hooks import (
    AgentInitializedEvent,
    AfterInvocationEvent,
    HookProvider,
    HookRegistry
)
from bedrock_agentcore.memory import MemoryClient


class PriceIntelMemoryHookProvider(HookProvider):
    """Manages long-term and short-term memory for the price intelligence agent."""

    def __init__(self, mem_client: MemoryClient, mem_id: str):
        self.mem_client = mem_client
        self.mem_id = mem_id

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """Load stored price-tracking preferences and inject into system prompt."""
        try:
            actor_id = event.agent.state.get('actor_id')
            if not actor_id:
                logger.warning('Missing actor_id in agent state')
                return

            namespace = f'user/{actor_id}/price_intel_preferences'
            preferences = self.mem_client.retrieve_memories(
                memory_id=self.mem_id,
                namespace=namespace,
                query='products tracked retailers price thresholds competitors monitoring',
                top_k=5
            )

            if preferences:
                pref_lines = []
                for pref in preferences:
                    if isinstance(pref, dict):
                        text = pref.get('content', {}).get('text', '').strip()
                        if text:
                            pref_lines.append(f'- {text}')

                if pref_lines:
                    context = '\n'.join(pref_lines)
                    event.agent.system_prompt += (
                        f'\n\n## User Price Intelligence Preferences (from previous sessions):\n{context}'
                    )
                    logger.info(f'Loaded {len(pref_lines)} price intel preferences')
            else:
                logger.info('No previous price intel preferences found — starting fresh')

        except Exception as e:
            logger.error(f'Error loading preferences: {e}')

    def on_after_invocation(self, event: AfterInvocationEvent):
        """Save conversation turn to short-term memory after each agent response."""
        try:
            messages = event.agent.messages
            if len(messages) < 2:
                return

            actor_id = event.agent.state.get('actor_id')
            session_id = event.agent.state.get('session_id')
            if not actor_id or not session_id:
                logger.warning('Missing actor_id or session_id')
                return

            user_msg = assistant_msg = None
            for msg in reversed(messages):
                content = msg.get('content', [])
                if msg['role'] == 'assistant' and not assistant_msg:
                    if content and isinstance(content[0], dict) and 'text' in content[0]:
                        assistant_msg = content[0]['text']
                elif msg['role'] == 'user' and not user_msg:
                    if content and isinstance(content[0], dict) and 'text' in content[0]:
                        if 'toolResult' not in content[0]:
                            user_msg = content[0]['text']
                            break

            if user_msg and assistant_msg:
                self.mem_client.create_event(
                    memory_id=self.mem_id,
                    actor_id=actor_id,
                    session_id=session_id,
                    messages=[(user_msg, 'USER'), (assistant_msg, 'ASSISTANT')]
                )
                logger.info('Saved conversation turn to short-term memory')

        except Exception as e:
            logger.error(f'Error saving conversation: {e}')

    def register_hooks(self, registry: HookRegistry):
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)
        logger.info('Price intel memory hooks registered')


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

def create_agent(gateway_url: str, memory_client: MemoryClient, mem_id: str,
                 actor_id: str, session_id: str):
    """Instantiate a price intelligence agent backed by the AgentCore Gateway."""
    from strands import Agent
    from strands.tools.mcp import MCPClient
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    system_prompt = (
        f'You are a competitive price intelligence agent. Today is {datetime.today().strftime("%Y-%m-%d")}. '
        'Use BrightData tools to scrape and analyze product pricing data across retailers. '
        'Track price trends, identify deals, and surface competitive insights. '
        'Always remember the user\'s tracked products, preferred retailers, and price thresholds.'
    )

    memory_hooks = PriceIntelMemoryHookProvider(memory_client, mem_id)

    mcp_client = MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=gateway_url,
            aws_region=AWS_REGION,
            aws_service='bedrock-agentcore'
        )
    )
    mcp_client.__enter__()

    agent = Agent(
        tools=mcp_client.list_tools_sync(),
        system_prompt=system_prompt,
        hooks=[memory_hooks],
        state={'actor_id': actor_id, 'session_id': session_id}
    )

    return agent, mcp_client


# ─────────────────────────────────────────────
# Memory inspection helpers
# ─────────────────────────────────────────────

def inspect_short_term_memory(memory_client: MemoryClient, mem_id: str,
                               actor_id: str, session_id: str):
    print('SHORT-TERM MEMORY (Raw Conversation Events)')
    print('=' * 60)
    events = memory_client.list_events(
        memory_id=mem_id,
        actor_id=actor_id,
        session_id=session_id
    )
    if events:
        for i, event in enumerate(events, 1):
            print(f'\n--- Event {i} ---')
            for turn in event.get('payload', []):
                conv = turn.get('conversational', {})
                role = conv.get('role', '')
                text = conv.get('content', {}).get('text', '')[:300]
                print(f'  [{role}] {text}')
    else:
        print('No events found yet.')


def inspect_long_term_memory(memory_client: MemoryClient, mem_id: str, actor_id: str):
    print('LONG-TERM MEMORY (Extracted Price Intel Preferences)')
    print('=' * 60)
    try:
        preferences = memory_client.retrieve_memories(
            memory_id=mem_id,
            namespace=f'user/{actor_id}/price_intel_preferences',
            query='products tracked retailers price thresholds competitors monitoring',
            top_k=5
        )
        if preferences:
            for i, pref in enumerate(preferences, 1):
                if isinstance(pref, dict):
                    text = pref.get('content', {}).get('text', '')
                    if text:
                        print(f'{i}. {text}')
        else:
            print('No preferences extracted yet. Wait 30-60s and retry.')
    except Exception as e:
        print(f'Could not retrieve long-term memory: {e}')


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    account_id = boto3.client('sts').get_caller_identity()['Account']
    logger.info(f'Account ID : {account_id}')
    logger.info(f'Region     : {AWS_REGION}')

    # Step 1 — IAM role
    role_arn = setup_iam_role(account_id)

    # Step 2 — Gateway
    agentcore = boto3.client('bedrock-agentcore-control', region_name=AWS_REGION)
    gateway_id = setup_gateway(agentcore, role_arn)

    # Step 3 — Target
    target_id = setup_target(agentcore, gateway_id)

    # Step 4 — Wait for READY
    wait_for_target_ready(agentcore, gateway_id, target_id)

    # Step 5 — Gateway URL
    gateway_details = agentcore.get_gateway(gatewayIdentifier=gateway_id)
    gateway_url = gateway_details.get('gatewayUrl', '')
    logger.info(f'Gateway URL: {gateway_url}')

    # Step 6 — Memory
    from bedrock_agentcore.memory import MemoryClient
    memory_client = MemoryClient(region_name=AWS_REGION)
    mem_id = setup_memory(memory_client, memory_id_override=MEMORY_ID)
    logger.info(f'Memory ID  : {mem_id}')

    # Step 7 — Session 1: set up tracking preferences
    agent, mcp_client = create_agent(gateway_url, memory_client, mem_id, USER_ID, SESSION_ID)

    print('You: I want to track iPhone 16 Pro prices. Alert me if it drops below $999 on Amazon or Best Buy.')
    print('\nAgent: ', end='')
    agent(
        'I want to track iPhone 16 Pro prices. Alert me if it drops below $999 on Amazon or Best Buy. '
        'Also keep an eye on Samsung Galaxy S25 as a competitor.'
    )

    print('\n')
    print('You: What are the current iPhone 16 Pro prices right now?')
    print('\nAgent: ', end='')
    agent('What are the current iPhone 16 Pro prices right now?')

    # Step 8 — Inspect memory
    inspect_short_term_memory(memory_client, mem_id, USER_ID, SESSION_ID)
    inspect_long_term_memory(memory_client, mem_id, USER_ID)

    # Step 9 — Session 2: demonstrate memory persistence
    print('\nWaiting for AgentCore to process memory in the background...')
    time.sleep(15)

    session_id_2 = f'price_intel_{datetime.now().strftime("%Y%m%d%H%M%S")}_2'
    logger.info(f'New Session ID: {session_id_2}')

    agent_2, mcp_client_2 = create_agent(gateway_url, memory_client, mem_id, USER_ID, session_id_2)

    print('You: Any price updates I should know about?')
    print('\nAgent: ', end='')
    agent_2('Any price updates I should know about? Do you remember what products and retailers I care about?')

    mcp_client.__exit__(None, None, None)
    mcp_client_2.__exit__(None, None, None)


if __name__ == '__main__':
    main()
