import asyncio
import subprocess

import boto3
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.bedrock import BedrockConverseModel
from pydantic_ai.providers.bedrock import BedrockProvider
from botocore.config import Config as BotocoreConfig

bedrock_config = BotocoreConfig(read_timeout=300,
                                connect_timeout=60,
                                retries={"max_attempts": 3},
                                )
bedrock_client = boto3.client('bedrock_runtime', region_name='us-west-1')
model = BedrockConverseModel(
    ""
)