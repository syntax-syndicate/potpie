import asyncio
import logging
from functools import lru_cache
from typing import AsyncGenerator, Dict, List, Optional

from langchain.schema import HumanMessage, SystemMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_core.runnables import RunnableSequence
from langchain_openai import ChatOpenAI
from sqlalchemy.orm import Session

from app.modules.conversations.message.message_model import MessageType
from app.modules.intelligence.memory.chat_history_service import ChatHistoryService
from app.modules.intelligence.prompts.prompt_schema import PromptResponse, PromptType
from app.modules.intelligence.prompts.prompt_service import PromptService
from app.modules.intelligence.tools.code_tools import CodeTools
from app.modules.parsing.knowledge_graph.inference_service import InferenceService
from app.modules.conversations.message.message_schema import ContextNode

logger = logging.getLogger(__name__)


class TestAgent:
    def __init__(self, openai_key: str, db: Session):
        self.llm = ChatOpenAI(
            api_key=openai_key,
            temperature=0.7,
            model="gpt-4o-mini",
            model_kwargs={"stream": True},
        )
        self.history_manager = ChatHistoryService(db)
        self.tools = CodeTools.get_tools()
        self.prompt_service = PromptService(db)
        self.chain = None
        self.inference_service = InferenceService()

    @lru_cache(maxsize=2)
    async def _get_prompts(self) -> Dict[PromptType, PromptResponse]:
        prompts = await self.prompt_service.get_prompts_by_agent_id_and_types(
            "TEST_AGENT", [PromptType.SYSTEM, PromptType.HUMAN]
        )
        return {prompt.type: prompt for prompt in prompts}

    async def _create_chain(self) -> RunnableSequence:
        prompts = await self._get_prompts()
        system_prompt = prompts.get(PromptType.SYSTEM)
        human_prompt = prompts.get(PromptType.HUMAN)

        if not system_prompt or not human_prompt:
            raise ValueError("Required prompts not found for QNA_AGENT")

        prompt_template = ChatPromptTemplate(
            messages=[
                SystemMessagePromptTemplate.from_template(system_prompt.text),
                MessagesPlaceholder(variable_name="history"),
                MessagesPlaceholder(variable_name="tool_results"),
                HumanMessagePromptTemplate.from_template(human_prompt.text),
            ]
        )
        return prompt_template | self.llm

    async def _run_tools(
        self, query: str, project_id: str, node_ids: Optional[List[ContextNode]] = None
    ) -> List[SystemMessage]:
        tool_results = []
        for tool in self.tools:
            try:
                tool_input = {
                    "query": query,
                    "project_id": project_id,
                    "node_ids": [node.node_id for node in node_ids] if node_ids else [],
                }
                logger.debug(f"Running tool {tool.name} with input: {tool_input}")

                tool_result = (
                    await tool.arun(tool_input)
                    if hasattr(tool, "arun")
                    else await asyncio.to_thread(tool.run, tool_input)
                )

                if tool_result:
                    tool_results.append(
                        SystemMessage(content=f"Tool {tool.name} result: {tool_result}")
                    )
            except Exception as e:
                logger.error(f"Error running tool {tool.name}: {str(e)}", exc_info=True)

        return tool_results

    async def run(
        self,
        query: str,
        project_id: str,
        user_id: str,
        conversation_id: str,
        node_ids: Optional[List[ContextNode]] = None,
    ) -> AsyncGenerator[Dict, None]:
        try:
            if not self.chain:
                self.chain = await self._create_chain()

            history = self.history_manager.get_session_history(user_id, conversation_id)
            validated_history = [
                (
                    HumanMessage(content=str(msg))
                    if isinstance(msg, (str, int, float))
                    else msg
                )
                for msg in history
            ]

            tool_results = await self._run_tools(query, project_id, node_ids)

            # Extract unique filenames for citations
            citations = list(
                set(
                    result.content.split("file=", 1)[1]
                    .split(",")[0]
                    .strip()
                    .strip('"')
                    .strip("'")
                    for result in tool_results
                    if "file=" in result.content
                )
            )

            # Yield the citations first
            yield {"citations": citations, "message": ""}

            inputs = {
                "history": validated_history,
                "tool_results": tool_results,
                "input": query,
            }

            logger.debug(f"Inputs to LLM: {inputs}")

            full_response = ""
            async for chunk in self.chain.astream(inputs):
                content = chunk.content if hasattr(chunk, "content") else str(chunk)
                full_response += content
                self.history_manager.add_message_chunk(
                    conversation_id, content, MessageType.AI_GENERATED
                )
                yield {"citations": citations, "message": full_response}

            logger.debug(f"Full LLM response: {full_response}")

            self.history_manager.flush_message_buffer(
                conversation_id, MessageType.AI_GENERATED
            )

        except Exception as e:
            logger.error(f"Error during QNAAgent run: {str(e)}", exc_info=True)
            yield {"citations": [], "message": f"An error occurred: {str(e)}"}
